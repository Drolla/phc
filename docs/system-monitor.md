# System Monitor

[`phc/devices/system_monitor/`](../phc/devices/system_monitor/) reports
the performance metrics of the machine PHC runs on: CPU, memory,
network and disk throughput, disk usage, CPU temperature and uptime.
It works on Windows and Linux (including Raspberry Pi). All endpoints are
read-only, and a metric that can't be read reports `null`.

```yaml
devices:
  - id: this_machine
    module: system_monitor
    name: This Machine
    disk_path: ""   # optional
```

- `disk_path` (optional): the filesystem path or drive that
  `disk_usage_percent` reports on, e.g. `"/data"` or `"D:\\"`. Leave it
  empty to use `/` on Linux or the system drive on Windows.

The default poll interval is `10s`. Override it with `update:`.

See [`examples/system_monitor_system.yaml`](../examples/system_monitor_system.yaml)
for a complete setup with live values and history graphs in the web UI.


## Endpoints

| Key                  | Unit | Description                          |
|----------------------|------|--------------------------------------|
| `cpu_percent`        | %    | CPU utilization since the last poll  |
| `memory_percent`     | %    | RAM in use                           |
| `network_rx_rate`    | B/s  | Inbound network throughput           |
| `network_tx_rate`    | B/s  | Outbound network throughput          |
| `disk_usage_percent` | %    | Used space on `disk_path`            |
| `disk_read_rate`     | B/s  | Disk read throughput                 |
| `disk_write_rate`    | B/s  | Disk write throughput                |
| `cpu_temperature`    | °C   | CPU temperature                      |
| `uptime_seconds`     | s    | Time since the machine last booted   |

The rate endpoints are averaged over the time since the previous poll.


## CPU Temperature

- **Linux**: read from `psutil.sensors_temperatures()`. The module looks
  for `coretemp` (x86) or `cpu_thermal` (Raspberry Pi) first, then falls
  back to any sensor it finds.
- **Windows**: read through WMI from
  [OpenHardwareMonitor](https://openhardwaremonitor.org/) or
  [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor).
  One of them must be running with admin rights. If neither is running,
  the value is `null`. Once a monitor starts, the next poll picks it up.
