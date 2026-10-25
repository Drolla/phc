# System Monitor

[`phc/devices/system_monitor/`](../phc/devices/system_monitor/) reports
the performance metrics of the machine PHC runs on: CPU, memory,
network and disk throughput, disk usage, CPU temperature and uptime.
It can also report the CPU and memory of selected processes instead of
the whole system (see [Monitoring processes](#monitoring-processes)).
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
- `match` (optional): selects processes instead of the whole system.

The default poll interval is `10s`. Override it with `update:`.

See [`examples/system_monitor_system.yaml`](../examples/system_monitor_system.yaml)
for a complete setup with live values and history graphs in the web UI.


## Endpoints

| Key                  | Unit | Whole system                    | Processes                         |
|----------------------|------|---------------------------------|-----------------------------------|
| `cpu_percent`        | %    | CPU utilization                 | CPU used by the processes         |
| `memory_percent`     | %    | RAM in use                      | RAM used by the processes         |
| `memory_used`        | MB   | RAM in use                      | RAM used by the processes (RES)   |
| `process_count`      |      | Running processes               | Matching processes                |
| `running`            |      | Always `true`                   | At least one process matches      |
| `network_rx_rate`    | MB/s | Inbound network throughput      | `null`                            |
| `network_tx_rate`    | MB/s | Outbound network throughput     | `null`                            |
| `disk_usage_percent` | %    | Used space on `disk_path`       | `null`                            |
| `disk_read_rate`     | MB/s | Disk read throughput            | Read by the processes (see below) |
| `disk_write_rate`    | MB/s | Disk write throughput           | Written by the processes          |
| `cpu_temperature`    | °C   | CPU temperature                 | `null`                            |
| `uptime_seconds`     | s    | Time since the machine booted   | Time since the oldest one started |

CPU and rate values are averaged over the time since the previous poll.
MB means 1,000,000 bytes.
`cpu_percent` is a share of the whole machine: 100 % means all cores are
busy. Note that `top` shows per-core values instead, so on a 4-core Pi a
process at 40 % in `top` reports 10 % here.


## Monitoring Processes

Add `match` with exactly one of these rules:

| Rule               | Selects                                                        |
|--------------------|----------------------------------------------------------------|
| `name: <name>`     | Processes with exactly this name, as shown by `top`            |
| `cmdline: <regex>` | Processes whose full command line matches the regex anywhere   |
| `self: true`       | PHC's own process                                              |

```yaml
devices:
  - id: zway_process
    module: system_monitor
    match: { name: z-way-server }
  - id: phc_process
    module: system_monitor
    match: { self: true }
  - id: matter_process
    module: system_monitor
    match: { cmdline: "matter-server" }
```

To see a process's full command line on Linux:

```
ps -eo pid,user,args | grep -i [m]atter
```

When several processes match, their values are added up. A process that
is restarted is picked up again on the next poll. While nothing matches,
`running` is `false`, `process_count` is `0` and the other values are
`null`.

A process's CPU and disk rates read `0` on the first poll after it's
found. Disk rates are `null` when PHC isn't allowed to read a process's
I/O counters, e.g. a root process seen from the `phc` user on Linux.


## CPU Temperature

- **Linux**: read from `psutil.sensors_temperatures()`. The module looks
  for `coretemp` (x86) or `cpu_thermal` (Raspberry Pi) first, then falls
  back to any sensor it finds.
- **Windows**: read through WMI from
  [OpenHardwareMonitor](https://openhardwaremonitor.org/), which must be
  running with admin rights. If it isn't running, the value is `null`.
  Once it starts, the next poll picks it up.
