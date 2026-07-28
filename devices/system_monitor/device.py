"""SystemMonitorDevice: local computer performance metrics (CPU, memory,
network, disk, temperature, uptime) -- cross-platform (Windows/Linux)."""

import os
import platform
import time

import psutil

from core.device import Device
from core.registry import register_module

# Linux sensor labels that carry a CPU-package/CPU-die temperature, in
# preference order: coretemp (typical x86 desktop/server), cpu_thermal /
# cpu-thermal (Raspberry Pi's SoC sensor under /sys/class/thermal).
_LINUX_CPU_TEMP_LABELS = ("coretemp", "cpu_thermal", "cpu-thermal")


@register_module("system_monitor")
class SystemMonitorDevice(Device):
    """Local host performance metrics via psutil (CPU, memory, network, disk,
    temperature, uptime). Cross-platform (Windows/Linux). Rate endpoints
    (network/disk I/O) are computed by diffing cumulative counters. Read-only.
    """

    def setup(self):
        """Prime rate counters so first receive() can diff against them."""
        self._disk_path = self.params.get("disk_path") or (
            os.environ.get("SystemDrive", "C:") + "\\" if os.name == "nt" else "/"
        )
        psutil.cpu_percent(interval=None)
        self._prev_net = psutil.net_io_counters()
        self._prev_disk = psutil.disk_io_counters()
        self._prev_time = time.monotonic()

    def receive(self) -> dict:
        now = time.monotonic()
        elapsed = max(now - self._prev_time, 1e-6)

        net = psutil.net_io_counters()
        disk = psutil.disk_io_counters()

        rx_rate = (net.bytes_recv - self._prev_net.bytes_recv) / elapsed
        tx_rate = (net.bytes_sent - self._prev_net.bytes_sent) / elapsed
        if disk is not None and self._prev_disk is not None:
            read_rate = (disk.read_bytes - self._prev_disk.read_bytes) / elapsed
            write_rate = (disk.write_bytes - self._prev_disk.write_bytes) / elapsed
        else:
            read_rate = write_rate = None

        try:
            disk_usage_percent = psutil.disk_usage(self._disk_path).percent
        except OSError:
            disk_usage_percent = None

        state = {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_percent": psutil.virtual_memory().percent,
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
        self._prev_time = now
        return state

    def _read_cpu_temperature(self):
        if platform.system() == "Windows":
            return self._read_cpu_temperature_windows()
        return self._read_cpu_temperature_linux()

    @staticmethod
    def _read_cpu_temperature_linux():
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
        Reconnects on every call (not cached) so a monitor that isn't running
        yet self-heals on a later poll. Any failure reports None."""
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
