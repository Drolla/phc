<p align="center">
  <img src="docs/logo.png" alt="Pylon Home Control logo" width="220">
</p>

# Pylon Home Control (PHC)

Pylon Home Control (PHC) is a small, YAML-configured home automation
framework. It polls and controls a tree of pluggable **devices** (weather
stations, sun position, virtual/test devices, and anything you add), and
runs **tasks** — condition- or time-driven automations — against their
state, all on a fixed-heartbeat scheduler.

## Documentation

- **User guide** — [`docs/`](docs/): [concepts](docs/concepts.md),
  [configuration reference](docs/configuration.md) (`!include`, module/
  parameter scoping, logging, time/duration syntax),
  [endpoint and device profiles](docs/profiles.md),
  [conditions, scripted actions & sticky values](docs/scripting.md), and one
  page per extension/integration: [random light control](docs/random-light.md),
  [mail alerts](docs/mail-alert.md), [log database](docs/logdb.md),
  [recovery](docs/recovery.md), [web UI](docs/web-ui.md), and the
  [Razberry/zWay Z-Wave integration](docs/zway.md).
- **Developer guide** — [`docs/developer/`](docs/developer/):
  [writing a device module](docs/developer/writing-a-device-module.md), plus
  internals for [zway](docs/developer/zway.md) and the
  [web UI](docs/developer/web-ui.md).

## Concepts

- **Device** — a node in a tree that exposes zero or more **endpoints**
  (readable/writable state) and may have child devices, backed by a
  plugin **module** declared in a system YAML file.
- **Module** — a device plugin: a `devices/<name>/device.py` (the `Device`
  subclass) plus a `devices/<name>/module.yaml` describing its parameters
  and endpoints declaratively. Modules are discovered automatically at
  startup.
- **Task** — an automation triggered either by a schedule (`time`/`repeat`)
  or by a device endpoint changing (`condition`), performing one or more
  **actions** (`set`, `toggle`, `log`, `create_task`, `kill_task`, `script`,
  ...).
- **Scheduler** — drives each device's fetch on its own interval and
  evaluates tasks once per heartbeat tick, running device I/O concurrently.

See [`docs/concepts.md`](docs/concepts.md) for the full picture (including
endpoint types/units/formatting), [`examples/`](examples/) for complete
system configurations, and the `module.yaml` file in each
[`devices/`](devices/) subfolder for what parameters/endpoints a given
device module supports. [`extensions/`](extensions/) is the home for
non-device PHC extensions, following the same package-plus-descriptor
pattern as device modules.

## Requirements

- Python >= 3.11
- Dependencies: `PyYAML`, `aiohttp`, `astral`, `Jinja2` (see `pyproject.toml`)

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

- `--log-level LEVEL` — default logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`);
  applies to every *stream* (`stdout`/`stderr`) destination in `log:`, never a
  file destination — see [Logging](docs/configuration.md#logging).
- `--log-level-module NAME=LEVEL` — override the level of one logger (e.g.
  `scheduler=DEBUG`) on every stream destination; repeatable.

Stop with Ctrl+C (SIGINT) or SIGTERM for a graceful shutdown.

## Tests

```
pytest
```

## License

MIT — see [LICENSE](LICENSE).
