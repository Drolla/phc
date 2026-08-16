"""Tests for phc/devices/system_monitor: local host performance metrics via
psutil, plus a platform-specific CPU temperature path. All psutil/platform
calls are monkeypatched so these tests are deterministic and independent of
the actual host's hardware.
"""

import sys
import types

from phc.core.endpoint import Endpoint
from phc.devices.system_monitor import device as system_monitor_device
from phc.devices.system_monitor.device import SystemMonitorDevice
from tests.conftest import fetch_sync

_ENDPOINT_KEYS = (
    "cpu_percent", "memory_percent", "network_rx_rate", "network_tx_rate",
    "disk_usage_percent", "disk_read_rate", "disk_write_rate",
    "cpu_temperature", "uptime_seconds",
)


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
                         lambda: types.SimpleNamespace(percent=55.0))

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


def _device(disk_path=""):
    return SystemMonitorDevice("host", params={"disk_path": disk_path}, endpoints=_endpoints())


def test_reads_all_endpoints_from_psutil(monkeypatch):
    _patch_psutil_defaults(monkeypatch, disk_counters=_io(read_bytes=0, write_bytes=0))
    dev = _device()
    fetch_sync(dev)
    dev.update_state()

    assert dev.get("cpu_percent") == 12.5
    assert dev.get("memory_percent") == 55.0
    assert dev.get("disk_usage_percent") == 42.0
    assert dev.get("uptime_seconds") == 2000.0 - 1000.0
    assert dev.get("network_rx_rate") == 0.0
    assert dev.get("network_tx_rate") == 0.0
    assert dev.get("disk_read_rate") == 0.0
    assert dev.get("disk_write_rate") == 0.0
    assert dev.get("cpu_temperature") is None


def test_network_and_disk_rates_are_computed_from_counter_deltas(monkeypatch):
    baseline_net = _io(bytes_recv=1000, bytes_sent=500)
    later_net = _io(bytes_recv=2000, bytes_sent=1500)
    baseline_disk = _io(read_bytes=200, write_bytes=100)
    later_disk = _io(read_bytes=700, write_bytes=600)

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
