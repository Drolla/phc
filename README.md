# Pylon Home Control (PHC)

Pylon Home Control (PHC) is a small, YAML-configured home automation
framework. It polls and controls a tree of pluggable **devices** (weather
stations, sun position, virtual/test devices, and anything you add), and
runs **tasks** — condition- or time-driven automations — against their
state, all on a fixed-heartbeat scheduler.

## Concepts

- **Device** — a node in a tree that exposes zero or more **endpoints**
  (readable/writable state) and may have child devices. Devices are backed
  by a plugin **module** (e.g. `meteoswiss`, `open_meteo`, `sun`,
  `virtual`, `host`), declared in a system YAML file.
- **Module** — a device plugin: a `devices/<name>/device.py` (the `Device`
  subclass) plus a `devices/<name>/module.yaml` describing its parameters
  and endpoints declaratively. Modules are discovered automatically at
  startup.
- **Task** — an automation triggered either by a schedule (`time`/`repeat`)
  or by a device endpoint changing (`condition`), performing one or more
  **actions** (`set`, `toggle`, `log`, `create_task`, `kill_task`, `script`,
  ...), with an optional `min_interval` retrigger cooldown.
- **Scheduler** — drives each device's fetch on its own interval and
  evaluates tasks once per heartbeat tick, running device I/O concurrently.

### Endpoint types, units & text

Unless otherwise specified, an endpoint's value is untyped and passes
through unchanged. An endpoint definition may opt into:

- `type` — `int`, `float`, `bool`, or `str`.
- `unit` — a display unit, e.g. `"°C"`, appended when formatting a
  numeric value as text.
- `values` — a raw value → text label mapping, e.g. `{ 0: "off", 1: "on" }`.

Given these, `Endpoint.to_text()`/`from_text()` (and the matching
`Device.get_text()`/`set_text()`) are the standard way to format a raw
value as display text and to parse text (or a raw value/label, e.g. `1` or
`"on"`) back into the endpoint's raw value — used by the `log` action's
`{text}` placeholder and the `set` action's `value:` parameter.

See [`examples/`](examples/) for complete system configurations, and the
`module.yaml` file in each [`devices/`](devices/) subfolder for what
parameters/endpoints a given device module supports.

[`extensions/`](extensions/) is the home for non-device PHC extensions
(e.g. [`extensions/logdb/`](extensions/logdb/), a CSV-backed sample store),
following the same package-plus-descriptor pattern as device modules.

### Conditions, scripted actions & sticky values

Beyond the `condition: { device, changed }` shorthand (fire when one
endpoint changes) and simple actions like `set`/`toggle`/`log`, a task's
`condition` and a `script` action's `code` can use a small, sandboxed
subset of Python — enough to combine several devices' state, spawn/cancel
tagged follow-up tasks, and read/reset a sticky min/max window, without
writing a new device module or extension:

```yaml
tasks:
  - tag: intrusion
    condition:
      refs: { armed: "security.armed", motion: "hallway.motion" }
      expr: "armed.state == 1 and motion.changed and motion.state == 1"
    min_interval: 5m   # don't refire more than once every 5 minutes
    action:
      kind: script
      code: |
        log("intrusion detected")
        set_state("siren.state", 1)
        create_task({ tag: "siren_off", time: "+3m",
                       action: { kind: "set", device: "siren.state", value: 0 } })
```

Both `condition.expr` and a `script` action's `code` run in the same
restricted sandbox (see `core/scripting.py`) — no imports, no attribute
access beyond a bound ref's `.state`/`.changed`/`.text`/`.event`/`.sticky`,
no method-call chains, no unbounded loops — against one shared set of
functions (`core/task.py`'s `_build_rule_namespace`), so a condition and a
script can never expose different capabilities by accident:

- Always available: `state(ref)`, `changed(ref)`, `text(ref)`, `event(ref)`,
  `sticky(ref)` (a since-last-`reset_sticky()` min/max window, the same
  mechanism `extensions/logdb` uses — see `log_aggregation` above), and
  `devices(pattern)` (a `core/selectors.py` glob, e.g. `"house.*/*"`, usable
  as a `for` target).
- Only in a `script` action (never in a condition, which must stay
  side-effect-free): `set_state(ref, value)`, `create_task(spec)` (same
  shape as a top-level `tasks:` entry), `kill_task(*tag_globs)` (remove
  matching tasks — the declarative form is the `kill_task` action kind),
  `reset_sticky(ref)`, and `log(msg)`.
- `ref` is a `"device.endpoint"` string, either inline (`state("a.b")`) or
  bound to a short name via an optional `refs: { name: "a.b" }` map for the
  `name.state`/`name.changed`/... attribute form used above.

See [`examples/surveillance_system.yaml`](examples/surveillance_system.yaml)
for a fuller worked example (arm/disarm, retriggered intrusion detection,
timed follow-ups, mass-cancel on disarm).

## Requirements

- Python >= 3.11
- Dependencies: `PyYAML`, `aiohttp`, `astral` (see `pyproject.toml`)

## Install

```
pip install -e .
```

For running the test suite, install the `dev` extra instead:

```
pip install -e ".[dev]"
```

## Usage

Run PHC against one of the example systems:

```
python phc.py --config examples/virtual_system.yaml
```

Useful flags:

- `--log-level LEVEL` — default logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`).
- `--log-level-module NAME=LEVEL` — override the level of one module's logger
  (e.g. `scheduler=DEBUG`); repeatable.

Stop with Ctrl+C (SIGINT) or SIGTERM for a graceful shutdown.

## Adding a device module

A new device type is a new `devices/<name>/` package containing:

- `device.py` — a `Device` subclass decorated with `@register_module("<name>")`.
- `module.yaml` — its declared parameters and endpoints.

See any existing module (e.g. [`devices/virtual/`](devices/virtual/)) for
the minimal shape, or [`devices/meteoswiss/`](devices/meteoswiss/) for a
fuller, network-backed example.

## Tests

```
pytest
```

## License

MIT — see [LICENSE](LICENSE).
