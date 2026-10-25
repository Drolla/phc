"""SystemMonitorDevice: local computer performance metrics.

CPU, memory, network, disk, temperature, uptime; cross-platform
(Windows/Linux). Whole system, or selected processes via `match`."""

import os
import platform
import re
import time

import psutil

from phc.core.device import Device
from phc.core.errors import ConfigError
from phc.core.registry import register_module

# Linux sensor labels that carry a CPU-package/CPU-die temperature, in
# preference order: coretemp (typical x86 desktop/server), cpu_thermal /
# cpu-thermal (Raspberry Pi's SoC sensor under /sys/class/thermal).
_LINUX_CPU_TEMP_LABELS = ("coretemp", "cpu_thermal", "cpu-thermal")

_MATCH_KINDS = ("name", "cmdline", "self")

# Byte-valued endpoints report MB (decimal, 10**6 bytes).
_MB = 1_000_000


@register_module("system_monitor")
class SystemMonitorDevice(Device):
    """Local host performance metrics via psutil.

    CPU, memory, network, disk, temperature, uptime; cross-platform
    (Windows/Linux). Rate endpoints (network/disk I/O) are computed by
    diffing cumulative counters. Read-only.

    With a `match` parameter, reports the sum over the matching processes
    instead (see _receive_processes).
    """

    def setup(self):
        """Parse `match`; prime rate counters so first receive() can diff."""
        self._disk_path = self.params.get("disk_path") or (
            os.environ.get("SystemDrive", "C:") + "\\" if os.name == "nt" else "/"
        )
        self._match = self._parse_match(self.params.get("match"))
        psutil.cpu_percent(interval=None)
        self._prev_net = psutil.net_io_counters()
        self._prev_disk = psutil.disk_io_counters()
        self._prev_proc_io = {}
        self._prev_time = time.monotonic()

    def _parse_match(self, match):
        """Turn `match` into a predicate over process_iter() entries.

        None for whole-system mode (no/empty `match`). Raises ConfigError
        unless exactly one of _MATCH_KINDS is given."""
        if not match:
            return None
        if not isinstance(match, dict) or len(match) != 1 or not set(match) <= set(_MATCH_KINDS):
            raise ConfigError(f"device {self.id!r}: match needs exactly one of "
                              f"{', '.join(_MATCH_KINDS)}, got {match!r}")
        (kind, value), = match.items()
        if kind == "name":
            return lambda proc: proc.info["name"] == value
        if kind == "cmdline":
            try:
                pattern = re.compile(str(value))
            except re.error as exc:
                raise ConfigError(f"device {self.id!r}: invalid match.cmdline regex: {exc}")
            return lambda proc: bool(pattern.search(" ".join(proc.info["cmdline"] or ())))
        if value is not True:
            raise ConfigError(f"device {self.id!r}: match.self must be true")
        return lambda proc: proc.pid == os.getpid()

    def receive(self) -> dict:
        """Read the whole system's metrics, or the matched processes'."""
        mono = time.monotonic()
        # Divisor turning a byte-counter delta into MB/s.
        mb_seconds = max(mono - self._prev_time, 1e-6) * _MB
        self._prev_time = mono
        if self._match is not None:
            return self._receive_processes(mb_seconds)
        return self._receive_system(mb_seconds)

    def _receive_processes(self, mb_seconds: float) -> dict:
        """Sum CPU/memory/disk I/O over every process `match` selects.

        process_iter() caches Process instances across calls, so each
        process's cpu_percent(interval=None) diffs against the previous
        poll (0.0 on the poll a process is first seen). CPU is divided by
        the core count to share the system-mode scale (100 = whole
        machine), unlike top's per-core percent.

        Disk rates diff per PID, so a process starting or exiting doesn't
        skew them; None if any matched process denies io_counters() (e.g.
        root's, seen from an unprivileged PHC). Network, disk usage and
        temperature have no per-process value and stay None."""
        state = {key: None for key in self.endpoints}
        samples, io = [], {}
        for proc in psutil.process_iter(["name", "cmdline"]):
            if not self._match(proc):
                continue
            try:
                with proc.oneshot():
                    try:
                        counters = proc.io_counters()
                        io[proc.pid] = (counters.read_bytes, counters.write_bytes)
                    except (psutil.AccessDenied, AttributeError):
                        io[proc.pid] = None
                    samples.append((proc.cpu_percent(interval=None), proc.memory_percent(),
                                    proc.memory_info().rss, proc.create_time()))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                io.pop(proc.pid, None)
        prev_io, self._prev_proc_io = self._prev_proc_io, io

        state.update(running=bool(samples), process_count=len(samples))
        if not samples:
            return state
        cpu, mem_percent, mem_used, created = zip(*samples)
        state.update(
            cpu_percent=sum(cpu) / (psutil.cpu_count() or 1),
            memory_percent=sum(mem_percent),
            memory_used=sum(mem_used) / _MB,
            uptime_seconds=time.time() - min(created),
        )
        if None not in io.values():
            common = [pid for pid in io.keys() & prev_io.keys() if prev_io[pid] is not None]
            state["disk_read_rate"] = sum(io[p][0] - prev_io[p][0] for p in common) / mb_seconds
            state["disk_write_rate"] = sum(io[p][1] - prev_io[p][1] for p in common) / mb_seconds
        return state

    def _receive_system(self, mb_seconds: float) -> dict:
        """Read whole-system metrics.

        Diffs cumulative network/disk counters against the previous
        call for the rate endpoints."""
        net = psutil.net_io_counters()
        disk = psutil.disk_io_counters()

        rx_rate = (net.bytes_recv - self._prev_net.bytes_recv) / mb_seconds
        tx_rate = (net.bytes_sent - self._prev_net.bytes_sent) / mb_seconds
        if disk is not None and self._prev_disk is not None:
            read_rate = (disk.read_bytes - self._prev_disk.read_bytes) / mb_seconds
            write_rate = (disk.write_bytes - self._prev_disk.write_bytes) / mb_seconds
        else:
            read_rate = write_rate = None

        try:
            disk_usage_percent = psutil.disk_usage(self._disk_path).percent
        except OSError:
            disk_usage_percent = None

        memory = psutil.virtual_memory()
        state = {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_percent": memory.percent,
            "memory_used": memory.used / _MB,
            "process_count": len(psutil.pids()),
            "running": True,
            "network_rx_rate": rx_rate,
            "network_tx_rate": tx_rate,
            "disk_usage_percent": disk_usage_percent,
            "disk_read_rate": read_rate,
            "disk_write_rate": write_rate,
            "uptime_seconds": time.time() - psutil.boot_time(),
            "cpu_temperature": self._read_cpu_temperature(),
        }

        self._prev_net = net
        self._prev_disk = disk
        return state

    def _read_cpu_temperature(self):
        """Return the CPU temperature via the platform-appropriate backend.

        None if unavailable."""
        if platform.system() == "Windows":
            return self._read_cpu_temperature_windows()
        return self._read_cpu_temperature_linux()

    @staticmethod
    def _read_cpu_temperature_linux():
        """Read CPU temperature from psutil's Linux sensors.

        Preferring _LINUX_CPU_TEMP_LABELS, else falling back to any
        available sensor."""
        try:
            sensors = psutil.sensors_temperatures()
        except AttributeError:
            return None  # not exposed on this platform (e.g. macOS, Windows)
        for label in _LINUX_CPU_TEMP_LABELS:
            entries = sensors.get(label)
            if entries:
                return entries[0].current
        for entries in sensors.values():  # best-effort fallback: any sensor at all
            if entries:
                return entries[0].current
        return None

    @staticmethod
    def _read_cpu_temperature_windows():
        """Read CPU temperature via OpenHardwareMonitor WMI namespace.

        Reconnects on every call (not cached) so a monitor that isn't
        running yet self-heals on a later poll. Any failure reports
        None."""
        try:
            import wmi
        except ImportError:
            return None
        try:
            client = wmi.WMI(namespace="root\\OpenHardwareMonitor")
            for sensor in client.Sensor():
                if sensor.SensorType == "Temperature" and "CPU" in sensor.Name:
                    return sensor.Value
        except Exception:
            return None
        return None
