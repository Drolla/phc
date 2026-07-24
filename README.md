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
- **Module** — a device plugin: a `modules/<name>/device.py` (the `Device`
  subclass) plus a `modules/<name>/module.yaml` describing its parameters
  and endpoints declaratively. Modules are discovered automatically at
  startup.
- **Task** — an automation triggered either by a schedule (`time`/`repeat`)
  or by a device endpoint changing (`condition`), performing one or more
  **actions** (`set`, `toggle`, `log`, `create_task`, ...).
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
`module.yaml` file in each [`modules/`](modules/) subfolder for what
parameters/endpoints a given device module supports.

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

A new device type is a new `modules/<name>/` package containing:

- `device.py` — a `Device` subclass decorated with `@register_module("<name>")`.
- `module.yaml` — its declared parameters and endpoints.

See any existing module (e.g. [`modules/virtual/`](modules/virtual/)) for
the minimal shape, or [`modules/meteoswiss/`](modules/meteoswiss/) for a
fuller, network-backed example.

## Tests

```
pytest
```

## License

MIT — see [LICENSE](LICENSE).
