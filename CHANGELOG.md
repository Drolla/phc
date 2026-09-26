# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Changes merged into `main` since the 0.1.0 release, in order.

### 2026-09-26

**New features**

- A zway device type's `write:` table can now be keyed by command name
  instead of only `true`/`false` — `doorlock` takes `"open"`, `"close"`
  and `"clear"` this way, each a string naming its own command. This is
  what makes a tag reader's lock/unlock alarm usable from PHC: bound to a
  Dummy Device on the controller (see [`docs/zway.md`](docs/zway.md)'s
  "Tag Readers" section), it reports the raw command name verbatim as a
  plain `type: str` endpoint, letting a task treat `"open"`/`"close"` as
  a one-shot handshake and reset it with a plain string write.

**Improvements**

- [`examples/zway_system.yaml`](examples/zway_system.yaml) and
  [`examples/devices/zway_devices.yaml`](examples/devices/zway_devices.yaml)
  now demonstrate every zway config pattern in real use: grouping several
  virtual devices as endpoints of one logical device, a bare
  `type`/`unit` endpoint with no `endpoint_profile`, the `battery`
  profile, a tag-reader handshake device with its reset task, a named
  `update:` interval, and an endpoint `history:`.

### 2026-09-13

**Breaking changes**

- The `zway` module is rewritten from scratch onto the Z-Way controller's
  **ZAutomation WebSocket API**, and no longer needs anything installed on
  the controller: the `thc_zWay.js` helper script and the `/JS/Run/` HTTP
  path are gone, replaced by a URL and an API token. Every existing zway
  config needs updating:
  - `base_url` becomes `url` (a `ws://` URL, e.g. `ws://192.168.1.21:8083`),
    and `user`/`password` become a single `token` (issued in the
    controller's own user profile).
  - `cache_time` is gone. The controller pushes live state events, so
    values are typically fresh within a tick of changing; `resync_interval`
    (default `5m`) only sets how often the full device list is re-read as a
    safety net.
  - Endpoints now name one of the controller's **virtual devices** with
    `device:` — a glob matched against both the device id and its title,
    e.g. `ZWayVDev_zway_20-1-37`, `"Rez: Light corridor"` or
    `"*living temp*"` — instead of `command_group` + `address`. It must
    match exactly one device; matching ignores case unless the endpoint
    sets `match_case: true`.
  - The per-product `device_profiles` (`fibaro-fgs222`,
    `everspring-st814`, `popp-z-weather`, …) and the `node:` parameter are
    gone with them: the WebSocket API exposes only virtual devices, which
    already abstract the physical Z-Wave hardware, so there is no
    per-product wiring left to describe. `endpoint_profiles` (`switch`,
    `dimmer`, `motion`, `temperature`, `humidity`, `luminosity`,
    `pressure`, `battery`) remain.

  See [`docs/zway.md`](docs/zway.md) and
  [`examples/zway_system.yaml`](examples/zway_system.yaml).

**New features**

- zway values are translated to PHC's own types rather than passed through
  raw: a binary device reports the strings `"on"`/`"off"` on the wire and
  appears in PHC as a real `bool`, and an unreadable, empty or unrecognised
  level — or a device the controller flags as failed — reads as `None`
  instead of a misleading value. The translation table lives in the
  module's `module.yaml` under `zway_types:`, keyed by the device type the
  controller reports, so supporting a new Z-Way device type is a config
  change rather than a code change.
- Which commands a zway endpoint accepts now follows the device type the
  controller reports for it, and a write that device type does not accept
  (any write to a sensor, a string to a thermostat) is rejected locally.
  The controller itself answers `200 OK` to commands that make no sense for
  the device, so without this a config mistake failed silently.
- `python phc/devices/zway/demo.py --url ... --token ... list` lists a
  controller's devices with their ids, titles, types and current values —
  the quickest way to find what to put in an endpoint's `device:`. It also
  takes `get`/`on`/`off`/`set` for trying a device out before wiring it
  into a config.
- A zway controller that is unreachable is retried after 1s, 10s, 60s and
  then every 5 minutes, with every endpoint reading `None` and the device
  marked unhealthy while it is down. Devices sharing a `url` and `token`
  share one WebSocket connection, and one reconnect.

### 2026-09-05

**Breaking changes**

- `extensions.web_ui` and `extensions.debug_portal` now default `host` to
  `0.0.0.0` (all interfaces) instead of `127.0.0.1` (loopback-only), so
  both are reachable from other devices on the LAN out of the box — no
  config change needed for the common case of running PHC on a Raspberry
  Pi and browsing the dashboard from another machine. Neither server has
  authentication, so only run PHC on a trusted LAN; set `host: 127.0.0.1`
  explicitly to restrict either one back to the local machine. See
  [`docs/web-ui.md`](docs/web-ui.md), [`docs/debug-portal.md`](docs/debug-portal.md)
  and [`docs/raspberry-pi-install.md`](docs/raspberry-pi-install.md).

### 2026-08-30

**New features**

- `virtual_latency` is renamed `emulated_device`, and its endpoints can now
  declare `simulate: { kind: toggle | drift, ... }` to generate their own
  readings each poll — `toggle` flips a bool on with some probability and
  clears it again after a delay (a motion sensor, a momentary tag-reader
  event); `drift` random-walks a numeric value within its own `min`/`max`
  (a battery gauge, a temperature reading). This is independent of
  `writable:`, and exists specifically so an endpoint standing in for
  read-only real hardware can still be genuinely read-only (see the next
  entry) while still moving on its own in a demo — a task/script has no way
  to write to a non-writable endpoint (`set_state`/`set_text`/`set` all
  reject it), so generation has to live in the device itself. See
  [`docs/configuration.md`](docs/configuration.md)'s `endpoint_parameters:`
  section.

**Improvements**

- Endpoints in the example configs that stand in for a real device's
  read-only reading (motion/PIR sensors, battery levels, an outdoor
  temperature/humidity/radon station) are now declared `writable: false`
  and, where the demo benefits from the value changing, `simulate:`-driven
  — they no longer render as a dropdown/toggle a person could click, which
  misrepresented what the real hardware they emulate can do.
- Endpoints that are genuinely two-state — on/off, open/closed,
  armed/disarmed, clear/motion — are now declared `type: bool` with a
  `{false: ..., true: ...}` label mapping instead of `type: int` with a
  `{0: ..., 1: ...}` one. Conditions read as plain truth tests
  (`armed.state`, `not alarm.state`) rather than comparisons against a
  magic `0`/`1`, and the web UI renders each as a toggle rather than a
  two-item dropdown while still displaying the mapping's own wording.
  Applied across the zway and open_meteo modules, the example configs,
  and the device template. See "Two-state endpoints" in
  [`docs/concepts.md`](docs/concepts.md).
- A `type: bool` endpoint's reading is now coerced to an actual bool.
  Hardware and web APIs report `0`/`1`, so the declared type had been
  metadata only — `get()` handed a script an int. A failed read is still
  `None`, not `False`.
- zway's `switch_binary` keeps its `0`/`255` wire values behind
  `read_transform`/`write_transform` rather than exposing them to every
  task that switches a light. Its read side now accepts any non-zero as
  on, so a dimmer-capable node reporting an intermediate `1..99` is no
  longer displayed as a bare `"99"`.
- `examples/virtual_full_system.yaml`, `virtual_surveillance-system_setup.yaml`
  (and its `-task_defs_{1,2,3}` companions), and `virtual_system.yaml` are
  renamed to `emulated_full_system.yaml`/`emulated_surveillance-*`/
  `emulated_system.yaml` — they demonstrate the same real systems as
  `full_house_system.yaml`/a surveillance setup with real hardware swapped
  for emulated stand-ins, not "a virtual system" in the abstract. The
  devices they mirror (`light_corridor`, `siren`, `tag_reader_front`,
  `porch_light`) now use `module: emulated_device` accordingly, matching
  the read-only sensors already switched over; `surveillance`/`alarm`,
  with no physical device behind them, stay `module: virtual`. The shared
  filler devices in `examples/devices/virtual_demo_devices.yaml` (now
  `emulated_demo_devices.yaml`) move the same way.
- `emulated_full_system.yaml`'s web UI gains two graph sections — indoor
  vs. outdoor temperature on Home, radon on Environment — and drops the
  now-redundant second graph on the History page that covered the same
  ground.
- A `report_*` task that logs a continuous environmental/battery/meteo
  reading (temperature, humidity, radon, battery level) now declares
  `min_interval: 1m`, so a fast-drifting `simulate:`d value — or ordinary
  sensor noise — logs at most once a minute instead of on every tick.
  Event-like tasks (motion, a relay flip, sunset/daylight, CPU load) are
  unaffected.
- The README shows a screenshot of the web UI instead of the project logo.

**Bug fixes**

- The `toggle` action had no bool case: it wrote the literal string
  `"on"`/`"off"` via a raw write, and since both are truthy the endpoint
  reported `"true"` either way and the toggle froze in the on position.
- The timers panel inferred its value control with `bool` and `values`
  in the opposite order to the dashboard, so one endpoint could render
  as a toggle in one place and a dropdown in the other.
- `recovery` restored persisted values with a raw write, skipping the
  `write_transform` that converts a logical value into what the hardware
  expects.
- `full_house_system.yaml`'s `porch_light` was modeled as a software flag
  (`module: virtual`) alongside `surveillance`/`alarm`, even though it's a
  physical light like `light_corridor`. It's now a real `module: zway`
  device (the `duwi` single-relay profile), and its endpoint is `sw`
  (matching that profile) rather than `state`.

**Breaking changes**

- A config using `module: virtual_latency` fails to load — rename it to
  `module: emulated_device` (its parameters and endpoint behavior are
  otherwise unchanged; only the module name and its example file,
  `examples/emulated_device_system.yaml`, moved).
- A config that writes zway's raw switch values directly needs updating:
  `value: 255` now parses as `false` (255 is not recognized truthy text),
  which would silently turn a light or siren *off*. Write `true`/`false`
  instead. Raw `1`/`0` still work, since `True == 1` in Python, as do
  `== 1`/`== 0` comparisons and the `"on"`/`"off"` labels.
- A recovery file written before this change is still restored
  correctly — the restore now normalizes it — but an endpoint whose type
  changed will be re-typed on the next write.
- `examples/virtual_full_system.yaml`, `virtual_surveillance-system_setup.yaml`,
  `virtual_surveillance-task_defs_{1,2,3}-*.yaml`, `virtual_system.yaml`, and
  `examples/devices/virtual_demo_devices.yaml` no longer exist under those
  names — see the `emulated_*` renames above. `house.porch_light.state` is
  now `house.porch_light.sw` in both `full_house_system.yaml` and its
  emulated mirror.

### 2026-08-23

**New features**

- Added `examples/device-template/`, a complete and working device module
  written to be copied when adding a new device. It demonstrates the whole
  pattern in one place — `self.context` for state shared between a
  module's devices, response caching with coalescing across siblings,
  `report_failure`, reads and writes, an `endpoint_parameters` field, and
  every `override`/`scope` combination a parameter can declare — and needs
  no hardware or network, so it runs as shipped. All of its fake I/O sits
  behind two methods, so swapping in a real protocol is a single edit.
  `examples/device_template_system.yaml` runs it, and covers the
  out-of-tree `plugin_paths:` path at the same time.
- `docs/developer/writing-a-device-module.md`, `CONTRIBUTING.md`, the new
  `phc/devices/README.md` and the `agentic-adding-a-device-module` skill
  now all start from that template rather than from "read a few existing
  modules and follow the pattern".

**Internal changes**

- `meteoswiss`, `open_meteo` and `waveplus_bridge` now hold their response
  cache and its lock in a per-system object in `self.context`, as `zway`
  already did, instead of in process-global module state. A module-scope
  cache outlived the `System` it belonged to and could serve one system's
  data to the next, and its `asyncio.Lock` bound to the first event loop
  that contended for it and then failed against any later one — which the
  `waveplus_bridge` tests had been working around by replacing the lock
  between cases. No effect on a normal run, which has one system on one
  event loop.
- The three cache-clearing test fixtures that module-scope state required
  are gone; the tests now pass an explicit shared `context` where they mean
  devices to share a cache.

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
- Documented the general workflow for adding a new device module with an AI
  assistant in `docs/developer/agentic-adding-a-device.md`.

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
