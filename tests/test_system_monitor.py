"""Tests for phc/devices/system_monitor: local host performance metrics via
psutil, plus a platform-specific CPU temperature path and per-process
`match` mode. All psutil/platform calls are monkeypatched so these tests are
deterministic and independent of the actual host's hardware.
"""

import contextlib
import sys
import types

import psutil
import pytest

from phc.core.endpoint import Endpoint
from phc.core.errors import ConfigError
from phc.devices.system_monitor import device as system_monitor_device
from phc.devices.system_monitor.device import SystemMonitorDevice
from tests.conftest import fetch_sync

_ENDPOINT_KEYS = (
    "cpu_percent", "memory_percent", "memory_used", "process_count", "running",
    "network_rx_rate", "network_tx_rate",
    "disk_usage_percent", "disk_read_rate", "disk_write_rate",
    "cpu_temperature", "uptime_seconds",
)

_MB = 1_000_000   # byte-valued endpoints report MB


def _endpoints():
    """Fresh Endpoint objects for one device -- must not be shared across
    device instances (Endpoint holds per-device mutable state)."""
    return [Endpoint(key) for key in _ENDPOINT_KEYS]


def _io(bytes_recv=0, bytes_sent=0, read_bytes=0, write_bytes=0):
    return types.SimpleNamespace(bytes_recv=bytes_recv, bytes_sent=bytes_sent,
                                  read_bytes=read_bytes, write_bytes=write_bytes)


def _sequence(values):
    """Return a callable yielding each of `values` in turn, then repeating
    the last one forever -- for monkeypatching a psutil/time function that
    setup() and receive() each call once (giving them different,
    controllable return values). Repeating past the end (rather than
    raising StopIteration) matters for time.monotonic() specifically: on
    Windows, asyncio's own event-loop teardown (triggered by fetch_sync's
    asyncio.run()) calls the real time.monotonic() a few more times after
    receive() has already read what the test cares about."""
    it = iter(values)
    last = None

    def _next(*a, **kw):
        nonlocal last
        try:
            last = next(it)
        except StopIteration:
            pass
        return last
    return _next


def _patch_psutil_defaults(monkeypatch, *, net_counters=None, disk_counters=None,
                            disk_usage_percent=42.0, sensors=None, plat="Linux"):
    """Patch every psutil/platform call setup()/receive() makes with fixed,
    deterministic values. net_counters/disk_counters may be a single
    SimpleNamespace (same value on every call) or a list (one value per
    call, via _sequence) for tests that need setup()'s baseline call and
    receive()'s later call to differ."""
    monkeypatch.setattr(system_monitor_device.psutil, "cpu_percent", lambda interval=None: 12.5)
    monkeypatch.setattr(system_monitor_device.psutil, "virtual_memory",
                         lambda: types.SimpleNamespace(percent=55.0, used=4 * _MB))
    monkeypatch.setattr(system_monitor_device.psutil, "pids", lambda: [1, 2, 3])

    net_counters = net_counters if net_counters is not None else _io()
    if isinstance(net_counters, list):
        monkeypatch.setattr(system_monitor_device.psutil, "net_io_counters", _sequence(net_counters))
    else:
        monkeypatch.setattr(system_monitor_device.psutil, "net_io_counters", lambda: net_counters)

    if isinstance(disk_counters, list):
        monkeypatch.setattr(system_monitor_device.psutil, "disk_io_counters", _sequence(disk_counters))
    else:
        monkeypatch.setattr(system_monitor_device.psutil, "disk_io_counters", lambda: disk_counters)

    monkeypatch.setattr(system_monitor_device.psutil, "disk_usage",
                         lambda path: types.SimpleNamespace(percent=disk_usage_percent))
    monkeypatch.setattr(system_monitor_device.psutil, "boot_time", lambda: 1000.0)
    # raising=False: psutil.sensors_temperatures doesn't exist at all on
    # Windows (only defined on platforms that support it), but every test
    # here patches it unconditionally for a consistent, deterministic
    # _patch_psutil_defaults regardless of which `plat` is under test.
    monkeypatch.setattr(system_monitor_device.psutil, "sensors_temperatures",
                         lambda: sensors if sensors is not None else {}, raising=False)
    monkeypatch.setattr(system_monitor_device.platform, "system", lambda: plat)
    monkeypatch.setattr(system_monitor_device.time, "time", lambda: 2000.0)


def _device(disk_path="", match=None):
    return SystemMonitorDevice("host", params={"disk_path": disk_path, "match": match},
                               endpoints=_endpoints())


def test_reads_all_endpoints_from_psutil(monkeypatch):
    _patch_psutil_defaults(monkeypatch, disk_counters=_io(read_bytes=0, write_bytes=0))
    dev = _device()
    fetch_sync(dev)
    dev.update_state()

    assert dev.get("cpu_percent") == 12.5
    assert dev.get("memory_percent") == 55.0
    assert dev.get("memory_used") == 4.0
    assert dev.get("process_count") == 3
    assert dev.get("running") is True
    assert dev.get("disk_usage_percent") == 42.0
    assert dev.get("uptime_seconds") == 2000.0 - 1000.0
    assert dev.get("network_rx_rate") == 0.0
    assert dev.get("network_tx_rate") == 0.0
    assert dev.get("disk_read_rate") == 0.0
    assert dev.get("disk_write_rate") == 0.0
    assert dev.get("cpu_temperature") is None


def test_network_and_disk_rates_are_computed_from_counter_deltas(monkeypatch):
    baseline_net = _io(bytes_recv=1000 * _MB, bytes_sent=500 * _MB)
    later_net = _io(bytes_recv=2000 * _MB, bytes_sent=1500 * _MB)
    baseline_disk = _io(read_bytes=200 * _MB, write_bytes=100 * _MB)
    later_disk = _io(read_bytes=700 * _MB, write_bytes=600 * _MB)

    _patch_psutil_defaults(monkeypatch, net_counters=[baseline_net, later_net],
                            disk_counters=[baseline_disk, later_disk])
    monotonic = _sequence([0.0, 10.0])
    monkeypatch.setattr(system_monitor_device.time, "monotonic", monotonic)

    dev = _device()  # setup(): baseline counters + monotonic() == 0.0
    fetch_sync(dev)  # receive(): later counters + monotonic() == 10.0 -> elapsed 10s
    dev.update_state()

    assert dev.get("network_rx_rate") == 100.0   # (2000-1000)/10
    assert dev.get("network_tx_rate") == 100.0   # (1500-500)/10
    assert dev.get("disk_read_rate") == 50.0     # (700-200)/10
    assert dev.get("disk_write_rate") == 50.0    # (600-100)/10


def test_disk_io_counters_none_reports_none_rates_without_affecting_others(monkeypatch):
    _patch_psutil_defaults(monkeypatch, disk_counters=None)
    dev = _device()
    fetch_sync(dev)
    dev.update_state()

    assert dev.get("disk_read_rate") is None
    assert dev.get("disk_write_rate") is None
    assert dev.get("cpu_percent") == 12.5
    assert dev.get("memory_percent") == 55.0


def test_linux_cpu_temperature_from_known_sensor_label(monkeypatch):
    sensors = {"cpu_thermal": [types.SimpleNamespace(current=45.2)]}
    _patch_psutil_defaults(monkeypatch, sensors=sensors, plat="Linux")
    dev = _device()
    fetch_sync(dev)
    dev.update_state()

    assert dev.get("cpu_temperature") == 45.2


def test_linux_cpu_temperature_none_when_no_sensors(monkeypatch):
    _patch_psutil_defaults(monkeypatch, sensors={}, plat="Linux")
    dev = _device()
    fetch_sync(dev)
    dev.update_state()

    assert dev.get("cpu_temperature") is None


def test_windows_cpu_temperature_none_when_wmi_package_unavailable(monkeypatch):
    # The dev/CI environment for this repo has no `wmi` package installed,
    # so the real ImportError path is exercised without needing to fake it.
    _patch_psutil_defaults(monkeypatch, plat="Windows")
    dev = _device()
    fetch_sync(dev)
    dev.update_state()

    assert dev.get("cpu_temperature") is None


def test_windows_cpu_temperature_picks_first_matching_cpu_sensor(monkeypatch):
    class _FakeSensor:
        def __init__(self, sensor_type, name, value):
            self.SensorType = sensor_type
            self.Name = name
            self.Value = value

    class _FakeWmiClient:
        def Sensor(self):
            return [
                _FakeSensor("Temperature", "GPU Core", 55.0),
                _FakeSensor("Load", "CPU Total", 20.0),
                _FakeSensor("Temperature", "CPU Package", 62.5),
            ]

    fake_wmi_module = types.ModuleType("wmi")
    fake_wmi_module.WMI = lambda namespace=None: _FakeWmiClient()
    monkeypatch.setitem(sys.modules, "wmi", fake_wmi_module)

    _patch_psutil_defaults(monkeypatch, plat="Windows")
    dev = _device()
    fetch_sync(dev)
    dev.update_state()

    assert dev.get("cpu_temperature") == 62.5


def test_windows_cpu_temperature_none_when_wmi_query_raises(monkeypatch):
    class _FakeWmiClient:
        def Sensor(self):
            raise RuntimeError("OpenHardwareMonitor not running")

    fake_wmi_module = types.ModuleType("wmi")
    fake_wmi_module.WMI = lambda namespace=None: _FakeWmiClient()
    monkeypatch.setitem(sys.modules, "wmi", fake_wmi_module)

    _patch_psutil_defaults(monkeypatch, plat="Windows")
    dev = _device()
    fetch_sync(dev)
    dev.update_state()

    assert dev.get("cpu_temperature") is None


def test_disk_path_defaults_per_platform(monkeypatch):
    _patch_psutil_defaults(monkeypatch)
    monkeypatch.setattr(system_monitor_device.os, "name", "posix")
    dev = _device(disk_path="")
    assert dev._disk_path == "/"

    monkeypatch.setattr(system_monitor_device.os, "name", "nt")
    monkeypatch.delenv("SystemDrive", raising=False)
    dev = _device(disk_path="")
    assert dev._disk_path == "C:\\"


def test_disk_path_explicit_override_is_used_as_is(monkeypatch):
    _patch_psutil_defaults(monkeypatch)
    dev = _device(disk_path="D:\\")
    assert dev._disk_path == "D:\\"


class _FakeProcess:
    """Stand-in for a process_iter() entry; `io=None` denies io_counters()."""

    def __init__(self, pid, name, cmdline=(), cpu=0.0, mem_percent=0.0, rss=0,
                 created=0.0, io=(0, 0), gone=False):
        self.pid = pid
        self.info = {"name": name, "cmdline": list(cmdline)}
        self._cpu, self._mem_percent, self._rss = cpu, mem_percent, rss
        self._created, self._io, self._gone = created, io, gone

    def oneshot(self):
        """No-op stand-in for psutil's caching context manager."""
        return contextlib.nullcontext()

    def io_counters(self):
        """Fixed counters, or AccessDenied when constructed with io=None."""
        if self._io is None:
            raise psutil.AccessDenied(self.pid)
        return _io(read_bytes=self._io[0], write_bytes=self._io[1])

    def cpu_percent(self, interval=None):
        """Fixed per-core CPU percent; NoSuchProcess when `gone`."""
        if self._gone:
            raise psutil.NoSuchProcess(self.pid)
        return self._cpu

    def memory_percent(self):
        """Fixed memory percent."""
        return self._mem_percent

    def memory_info(self):
        """Fixed resident set size."""
        return types.SimpleNamespace(rss=self._rss)

    def create_time(self):
        """Fixed start time."""
        return self._created


def _patch_processes(monkeypatch, *polls, cpu_count=4):
    """Patch process_iter() to yield one list of _FakeProcess per call."""
    monkeypatch.setattr(system_monitor_device.psutil, "process_iter",
                         _sequence([list(procs) for procs in polls]))
    monkeypatch.setattr(system_monitor_device.psutil, "cpu_count", lambda: cpu_count)


def _poll(dev):
    """One receive() plus state commit."""
    fetch_sync(dev)
    dev.update_state()


def test_match_name_sums_matching_processes(monkeypatch):
    _patch_psutil_defaults(monkeypatch)
    _patch_processes(monkeypatch, [
        _FakeProcess(10, "z-way-server", cpu=6.0, mem_percent=2.0, rss=100 * _MB, created=1500.0),
        _FakeProcess(11, "z-way-server", cpu=2.0, mem_percent=1.0, rss=50 * _MB, created=1200.0),
        _FakeProcess(12, "python", cpu=50.0, mem_percent=9.0, rss=999, created=1100.0),
    ])
    dev = _device(match={"name": "z-way-server"})
    _poll(dev)

    assert dev.get("running") is True
    assert dev.get("process_count") == 2
    assert dev.get("cpu_percent") == 2.0               # (6+2) / 4 cores
    assert dev.get("memory_percent") == 3.0
    assert dev.get("memory_used") == 150.0
    assert dev.get("uptime_seconds") == 2000.0 - 1200.0   # oldest match
    for key in ("network_rx_rate", "network_tx_rate", "disk_usage_percent", "cpu_temperature"):
        assert dev.get(key) is None


def test_match_cmdline_is_a_regex_search(monkeypatch):
    _patch_psutil_defaults(monkeypatch)
    _patch_processes(monkeypatch, [
        _FakeProcess(20, "python", ["/opt/phc/.venv/bin/phc", "--config", "system.yaml"], rss=7 * _MB),
        _FakeProcess(21, "python", ["python", "other.py"], rss=99),
    ])
    dev = _device(match={"cmdline": r"phc\s+--config"})
    _poll(dev)

    assert dev.get("process_count") == 1
    assert dev.get("memory_used") == 7.0


def test_match_self_selects_own_pid(monkeypatch):
    _patch_psutil_defaults(monkeypatch)
    monkeypatch.setattr(system_monitor_device.os, "getpid", lambda: 31)
    _patch_processes(monkeypatch, [_FakeProcess(30, "python", rss=1 * _MB),
                                   _FakeProcess(31, "python", rss=2 * _MB)])
    dev = _device(match={"self": True})
    _poll(dev)

    assert dev.get("memory_used") == 2.0


def test_no_matching_process_reports_not_running(monkeypatch):
    _patch_psutil_defaults(monkeypatch)
    _patch_processes(monkeypatch, [_FakeProcess(40, "python")])
    dev = _device(match={"name": "z-way-server"})
    _poll(dev)

    assert dev.get("running") is False
    assert dev.get("process_count") == 0
    assert dev.get("cpu_percent") is None
    assert dev.get("memory_used") is None


def test_process_exiting_mid_poll_is_skipped(monkeypatch):
    _patch_psutil_defaults(monkeypatch)
    _patch_processes(monkeypatch, [_FakeProcess(50, "app", rss=5 * _MB),
                                   _FakeProcess(51, "app", rss=6 * _MB, gone=True)])
    dev = _device(match={"name": "app"})
    _poll(dev)

    assert dev.get("process_count") == 1
    assert dev.get("memory_used") == 5.0


def test_process_disk_rates_diff_per_pid(monkeypatch):
    _patch_psutil_defaults(monkeypatch)
    # A settable clock, not _sequence: asyncio's own loop also calls
    # monotonic() between the two polls.
    clock = [0.0]
    monkeypatch.setattr(system_monitor_device.time, "monotonic", lambda: clock[0])
    _patch_processes(
        monkeypatch,
        [_FakeProcess(60, "app", io=(100 * _MB, 200 * _MB))],
        # 60 grows by 500/1000 MB; 61 is new, so it has no baseline yet.
        [_FakeProcess(60, "app", io=(600 * _MB, 1200 * _MB)),
         _FakeProcess(61, "app", io=(9000 * _MB, 9000 * _MB))],
    )
    dev = _device(match={"name": "app"})
    _poll(dev)
    assert dev.get("disk_read_rate") == 0.0   # first poll: no baseline

    clock[0] = 10.0
    _poll(dev)
    assert dev.get("disk_read_rate") == 50.0   # 500 MB / 10s
    assert dev.get("disk_write_rate") == 100.0


def test_process_disk_rates_none_when_io_counters_denied(monkeypatch):
    _patch_psutil_defaults(monkeypatch)
    _patch_processes(monkeypatch, [_FakeProcess(70, "app", io=(0, 0)),
                                   _FakeProcess(71, "app", io=None)])
    dev = _device(match={"name": "app"})
    _poll(dev)

    assert dev.get("disk_read_rate") is None
    assert dev.get("disk_write_rate") is None
    assert dev.get("process_count") == 2


@pytest.mark.parametrize("match", [
    {"name": "a", "cmdline": "b"}, {"pid": 1}, {"self": False}, {"cmdline": "("}, "z-way-server",
])
def test_invalid_match_raises_config_error(monkeypatch, match):
    _patch_psutil_defaults(monkeypatch)
    with pytest.raises(ConfigError):
        _device(match=match)
