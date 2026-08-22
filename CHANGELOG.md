# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Changes merged into `main` since the 0.1.0 release, in order.

### 2026-08-22

**Developer experience**

- Documented that a new device module or extension is templated enough to
  build with an LLM coding assistant, and pointed `README.md` and
  `CONTRIBUTING.md` at `.agentic_flowspace/` for the shared conventions one
  should follow in this repo.
- Added `.agentic_flowspace/skills/agentic-adding-a-device-module.md`, a workflow
  for scaffolding a new device module that has the assistant clarify the
  target physical device and its endpoints with the user before writing
  code, and propose an example config demonstrating it once the module
  works.
- Documented how to have an AI assistant draft a new
  `.agentic_flowspace/skills/` workflow in
  `docs/developer/agentic-creating-a-skill.md`, using the conversation
  that produced the device-module skill above as the worked example.

**Internal changes**

- Project conventions (git workflow, documentation, code style, changelog)
  now live once in `.agentic_flowspace/instructions/`, with a single shared
  usage guide (`.agentic_flowspace/README.md`) referenced by every
  coding-assistant tool's own root file (`CLAUDE.md`, `AGENTS.md` for
  Codex, `GEMINI.md` for Gemini CLI, `.github/copilot-instructions.md` for
  Copilot) instead of duplicating the guidance per tool.
  `.agentic_flowspace/index.json` indexes the shared instructions plus
  placeholder `skills/`/`agents/` folders for future project-specific work.

### 2026-08-21

**Internal changes**

- The scheduler's two clocks now travel as a single frozen `Now(wall, mono)`
  value (`phc/core/clock.py`) instead of a `now`/`now_mono` parameter pair,
  so every use site names the clock it reads. `Scheduler.tick()`,
  `Task.run()` and the debug portal's `build_snapshot()` lost their
  `now_mono` parameter; all three still accept a bare number, expanded to
  both clocks reading that instant, so a caller driving ticks at explicit
  times is unaffected. Functions needing only one clock now take a `mono:`
  or `wall:` float rather than an ambiguous `now:` — `Device.due()`,
  `Device.mark_run()`, `DeviceHealth.record_success()`/`record_failure()`
  and `Endpoint.get_age()`. No behavior change, and no YAML change; the
  wall/monotonic split itself is unchanged.

### 2026-08-16

**Packaging**

- Installing PHC any way other than `pip install -e .` now works. The
  built wheel previously contained no `module.yaml`, no `extension.yaml`,
  and none of the web UI's templates or static assets — nothing but `.py`
  files — so a real install failed at startup on the first device
  (`module 'sun' has no module.yaml`) and served an unstyled UI. These are
  now declared as package data, located at runtime through
  `importlib.resources` rather than by walking up from a source file's
  path, and a `wheel-install` CI job installs a built wheel into a clean
  environment and boots an example from outside the checkout.

**New features**

- Device health is now first-class. A failed poll was logged and swallowed
  so one flaky device could not stall the tick — which also made a dead
  device invisible, since its endpoints keep their last-good values and a
  frozen reading looks exactly like a steady one. Each device now records
  whether its most recent I/O succeeded, and each endpoint when it last
  produced an actual reading (distinct from `update_time`, which only moves
  when the value *changes*). Surfaced four ways: `available(ref)` and
  `age(ref)` in task conditions and scripts, a "not responding" marker on
  affected web UI widgets, a health column in the debug portal's poll
  queue, and a `phc.health` logger that reports a device starting to fail
  or recovering — once per transition, not once per tick. Modules that
  catch their own network errors report them via the new
  `Device.report_failure()`; all four bundled network modules do. See
  [Is this reading still trustworthy?](docs/scripting.md#is-this-reading-still-trustworthy).
- Endpoints can enforce their declared `min`/`max` on writes, via a new
  `on_invalid:` of `pass` (default, unchanged behaviour), `reject` or
  `clamp`. The bounds were previously stored but never checked — fine as
  the UI hint they were added for, less fine when a task computes a
  setpoint and sends 95 to something that accepts 5-30. Checked before
  `write_transform`, since the bounds describe the logical value, not the
  raw one the hardware receives.
- New CLI subcommands. `phc validate --config FILE` performs the entire
  load — discovery, parameter and endpoint resolution, task and action
  building — and reports what it built, without starting the scheduler,
  binding a port or touching hardware; it exits non-zero on a broken
  config, so it works as a pre-deploy check. `phc list-modules` /
  `phc list-extensions` report what an installation can actually use, with
  each plugin's package, description and declared parameters
  (`--plugin-path DIR` includes out-of-tree ones). The original
  `phc --config FILE` spelling is unchanged and remains the default action.
- Device modules and extensions no longer have to live inside PHC. A
  module is discovered the same way wherever it lives, and a system YAML
  cannot tell the difference — `module: <name>` either way, with its
  `module.yaml` read from whichever package defines it. Two new sources
  alongside the bundled ones: an entry point in the `phc.devices` /
  `phc.extensions` group (the normal way to publish a plugin), and
  `plugin_paths:` in the system YAML, a list of directories laid out like
  `phc/devices/` for a private module not worth packaging. See
  [Using device modules and extensions from outside PHC](docs/configuration.md#using-device-modules-and-extensions-from-outside-phc).

**Bug fixes**

- A web UI `graph`/`timers` panel naming an extension instance that does
  not exist now fails at startup with a `ConfigError` naming the panel and
  listing what is configured. These references are resolved per request
  (the referenced instance may be declared later in the file), so a typo
  previously survived the whole load and surfaced only as a 404 in a
  browser, and only if someone opened that page. The check asks for the
  capability the panel actually uses, so pointing a graph at a real
  instance of the wrong kind is caught too.
- A plugin whose own `device.py`/`extension.py` fails to import now
  reports that error instead of being silently skipped. Discovery caught
  `ModuleNotFoundError` broadly, so it could not tell "this package has no
  device.py" from "device.py exists but its `import serial` failed" — a
  module with a missing dependency simply did not exist, and the config
  naming it failed later, confusingly, as an unknown module.
- A typo'd `module:`/extension name now reports what *is* available
  instead of raising a bare `KeyError` from the registry.

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

**Developer experience**

- Added an architecture overview
  ([`docs/developer/architecture.md`](docs/developer/architecture.md)) and
  a guide to writing an extension
  ([`docs/developer/writing-an-extension.md`](docs/developer/writing-an-extension.md)),
  the counterpart to the existing device-module guide.
- The extension lifecycle is now an explicit contract
  (`phc.core.extension`) rather than four `hasattr` checks, and a
  misspelled hook (`on_tik`) is rejected at load. Hooks are found by name,
  so such a method was not a broken hook but simply never called — the
  extension loaded fine and silently never did its job.
- Added `ruff` and `mypy` configuration and a CI lint job, plus coverage
  reporting (currently 96%). mypy is set up as a ratchet: modules with
  pre-existing findings are listed as exempt so the gate is green today
  and tightens by deleting entries.

**Internal structure**

- `phc/core/config.py` (1469 lines, ~15 responsibilities) is now a package
  with one module per stage of the load — `yamlio`, `descriptors`,
  `params`, `endpoints`, `devices`, `extensions`, `tasks`, `hooks`,
  `system` — arranged as a dependency DAG. Every name it previously
  exposed is re-exported, so imports are unchanged.
- Device modules can share per-instance state through a new
  `Device.context`, a dict scoped to one loaded system. `devices/zway`
  moves its nine module-level globals there: two systems loaded in one
  process previously shared zway's batched-fetch registry, response cache,
  session cookies and helper-loaded markers, which (among other things)
  kept the response cache permanently invalid, since its freshness check
  compares against the identifier count. See
  [Sharing state between a module's devices](docs/developer/writing-a-device-module.md#sharing-state-between-a-modules-devices).
- `ConfigError` moved to a new, dependency-free `phc.core.errors` (still
  re-exported from `phc.core.config`), alongside a new `PhcError` base.
  Naming the exception used to mean importing the whole config loader —
  `phc.core.selectors`, a leaf module, did exactly that, as did every
  extension.
- The live task list is now a `phc.core.task.TaskRegistry` rather than a
  bare `list` shared and mutated by the Scheduler, every Action, and
  `extensions.timer`. It also owns the context needed to build tasks at
  runtime, which removes the last import cycle in `phc.core`:
  `create_task`/`kill_task` no longer reach into the config loader through
  a function-local import of a private name. `importing phc.core.task` no
  longer pulls in the config loader at all. The Scheduler is unchanged and
  still accepts a plain list of tasks.

**Breaking changes**

- **An extension's relative file paths now resolve against the system YAML
  file's directory**, not the process's working directory — matching `log:`
  destinations and `plugin_paths:`, which always did. Affects `logdb`'s
  `csv_path` and `recovery`'s/`timer`'s `path`. Previously, where an
  installation's history and recovery files landed depended on where it was
  started from, so a service started from `/` wrote somewhere different
  from a hand-started one. **If you have existing data, move it next to
  your config**; PHC logs a warning naming both locations when it finds
  data at the old one and nothing at the new one. Absolute paths are
  unaffected.
- `phc.core.task.register_task()` and `kill_tasks()` are replaced by
  `TaskRegistry.create()` and `TaskRegistry.kill()`. Affects only code
  driving PHC's task list directly; no system YAML changes.
- The Python packages moved under a single `phc` package: `core` →
  `phc.core`, `devices` → `phc.devices`, `extensions` → `phc.extensions`,
  and the `phc.py` script → `phc.cli`. Installing PHC used to claim the
  top-level names `core`, `devices` and `extensions` in site-packages,
  which are about as collision-prone as names get. Only code that imports
  PHC is affected — no system YAML changes, since `module:`/`extensions:`
  entries name modules logically, not by Python path.
- `python phc.py --config ...` is now `python -m phc --config ...`. A
  root `phc.py` next to the `phc/` package would shadow it and make
  `import phc.core` ambiguous. The installed `phc` console command is
  unchanged.

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
