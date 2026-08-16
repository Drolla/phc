# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Changes merged into `main` since the 0.1.0 release, in order.

### 2026-08-16

**Bug fixes**

- The heartbeat no longer drifts. Each tick is now scheduled one heartbeat
  after the previous tick's *start* rather than after it finishes, so the
  real tick period was previously `heartbeat + tick duration` — a system
  with a 1s heartbeat and a 200ms tick actually ran 20% slow, and every
  `update:`/`repeat:` interval in it with it. An overrunning tick now skips
  the missed grid points (one WARNING per overrun episode) instead of
  accumulating a backlog.
- Shutdown is immediate. `Scheduler.stop()` (Ctrl-C/SIGTERM) previously
  only set a flag, leaving the process to wait out the pending heartbeat
  sleep before exiting — up to a full heartbeat, which on a quiet
  installation using a 10s+ heartbeat looked like a hang.
- Intervals now run on a monotonic clock instead of the wall clock: a
  device's `update:`, an endpoint's `history.interval` and a task's
  `min_interval:`. An NTP correction or a daylight-saving change that moved
  the system clock backwards used to stall *all* polling for the size of
  the step, and a step forwards fired a burst of catch-up polls. A task's
  `time:`/`repeat:` still use the wall clock, since they name an absolute
  time of day. See
  [Which clock each schedule runs on](docs/configuration.md#which-clock-each-schedule-runs-on).

**Breaking changes**

- A device is now polled only on its own `update:` interval. Previously
  `Device.fetch()` recursed into child devices, so a child was also
  fetched whenever *any* ancestor was due — meaning a child could be
  polled far more often than its own `update:` asked for, and one with
  `update: null` was polled anyway. No shipped device module or example
  config is affected (only `host` defaults to `update: null`, and it has
  no endpoints); a hand-written config that relied on a parent to drive
  its children's polling now needs an explicit `update:` on each child.

### 2026-08-15

**New features**

- Added `extensions/timer`: user-programmable, persisted timers that set
  or toggle a device endpoint at a chosen time (optionally repeating),
  created/edited at runtime rather than only via hand-authored YAML
  tasks. Includes a "timers" panel for `extensions/web_ui`.
- Added the `!placeholder` YAML tag: marks a scalar (credential, another
  system's URL, ...) that must be replaced before a system config is fit
  to run; `load_system` now refuses to start if any `!placeholder` value
  survives, listing every offending field.

**Improvements**

- `--config` load errors are now reported as a clean message from the
  CLI instead of a raw traceback.
- Lowered `zway`'s default update interval from 1m to 1s.
- Examples: added `timer_system.yaml` and `virtual_full_system.yaml`,
  extended `full_house_system.yaml` with tag-reader arm/disarm, and
  sanitized example credentials/URLs with `!placeholder`.

### 2026-08-09

**Bug fixes**

- Fixed a silent write drop when writing to a native-async device (e.g.
  `zway`) through the web UI's `/api/set` endpoint: the HTTP request
  reported success but the underlying hardware write never happened.

### 2026-08-08

**New features**

- `zway` now auto-loads `thc_zWay.js` on the controller before use if it
  isn't already loaded, retrying on the next poll rather than blocking
  startup.

**Improvements**

- Lowered `zway`'s default `cache_time` from 30s to 1s and
  `meteoswiss`'s default update interval from 10m to 1m.
- Added INFO/DEBUG logging to the `zway` device module (connection/
  registration lifecycle at INFO, every physical-device request/response
  at DEBUG); fetch/write failures now log at ERROR instead of failing
  silently.

**Bug fixes**

- Fixed traceback spam on every shutdown log line when Ctrl-C leaves
  stdout piped to an already-exited process on Windows.

### 2026-08-05

**New features**

- Reworked task scheduling so condition and time/repeat are independent
  gates: a task can be condition-gated, due-time-gated, both, or
  neither, matching the previous Tcl system's job model.
- Added `!include` list-splicing: a `- !include <path>` list item whose
  target file is itself a YAML sequence now splices into the
  surrounding list instead of nesting as one list-of-lists element.

**Improvements**

- Removed `random_light`'s `enable_ref`/`pause_ref` in favor of
  expressing the same gating via the firing task's own `condition:`.
- Examples: consolidated shared device definitions into reusable
  device-group files.

**Bug fixes**

- Fixed `zway`'s `""` "no value yet" sentinel crashing any
  `read_transform` expecting a number; it's now normalized to `None`.
- Config YAML files are now opened with explicit UTF-8 encoding, fixing
  potential mis-decoding on systems where UTF-8 isn't the default.

### 2026-08-04

**New features**

- Added `task_specs:` and `create_task`'s `template:`, for defining a
  reusable task/follow-up shape once and instantiating it by name
  instead of repeating or deeply nesting the same `specs:` at every
  spawn site.

**Improvements**

- Lowered the `virtual` device module's default update interval from 5s
  to 1s.
- Examples: split the surveillance example into a reusable setup file
  plus separate task-definition files.

### 2026-08-03

**Internal change**

- A one-shot task is now removed from the scheduler once it fires,
  instead of staying resident forever.

## [0.1.0] - 2026-08-02

Initial public release.

- Core scheduler, device/endpoint model, task/condition/action engine, and
  YAML configuration loader (`!include`, module/parameter scoping, profiles).
- Device modules: `host`, `meteoswiss`, `open_meteo`, `sun`,
  `system_monitor`, `virtual`, `virtual_latency`, `waveplus_bridge`, `zway`.
- Extensions: `logdb`, `mail_alert`, `random_light`, `recovery`, `web_ui`.
- `phc` console command (in addition to `python phc.py`).
