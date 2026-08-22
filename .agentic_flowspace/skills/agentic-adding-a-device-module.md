# Adding a device module

Applies when the user asks to add support for a new device, sensor,
actuator, or protocol in PHC: a new `phc/devices/<name>/` package, or an
out-of-tree module/plugin (see the "Shipping a module outside PHC"
section of the doc below).

## 1. Clarify the physical device first

Before writing any code, ask the user which physical device or product
this module targets, and what it should expose as endpoints. Don't infer
endpoints from a vague request ("add support for my thermostat") — pin
down:

- The specific device/product (make/model) and how PHC would talk to it
  (local REST API, MQTT, serial, an existing vendor SDK/library, etc.).
- Which values it should expose: name, `readable`/`writable`, `type`,
  `unit`, and a plain-English `description` for each endpoint.
- Whether one module needs to support several similar products sharing a
  protocol (a candidate for `device_profiles`/`endpoint_profiles`) or
  just one shape of device.
- Any device-level parameters needed to address the specific unit (host,
  IP, serial port, station id, ...), and whether each is per-device or
  shared module-wide (`scope: device` vs `scope: module`).
- Whether the module belongs in this repo (`phc/devices/<name>/`) or
  out-of-tree (own distribution or `plugin_paths:` directory — see the
  doc's "Shipping a module outside PHC" section).

Don't proceed to scaffolding until this is settled — a wrong or
incomplete endpoint list is expensive to unwind once `device.py` and an
example config both depend on it.

## 2. Read the pattern first

Read
[`docs/developer/writing-a-device-module.md`](../../docs/developer/writing-a-device-module.md)
for the full `device.py`/`module.yaml` pattern (the `Device` subclass
shape, `receive`/`transmit` vs the `_async` pair, `report_failure`,
`self.context` for module-shared state, the `module.yaml` schema
including `endpoint_parameters`/`device_profiles`/`endpoint_profiles`,
and how to ship a module outside PHC). Also skim
[`docs/developer/architecture.md`](../../docs/developer/architecture.md)
for how a device module fits into the rest of PHC, and
[`docs/configuration.md`](../../docs/configuration.md) /
[`docs/profiles.md`](../../docs/profiles.md) if the device needs shared
module config or a profile library.

## 3. Scaffold the module

Create `device.py` and `module.yaml` following that pattern, using the
endpoints and parameters settled in step 1, in the location settled
there (`phc/devices/<name>/`, or an equivalent package out-of-tree).
`module.yaml` `description` fields are user-facing (rendered in the web
UI) — plain English, not implementation notes; put implementation
rationale in `device.py` docstrings instead.

## 4. Propose an example configuration

Once the module works, propose — and on confirmation, implement — an
example system config under `examples/` that demonstrates it end to end.
Follow the existing conventions: a bare device-list file at
`examples/devices/<name>_*.yaml` (see `meteoswiss_stations.yaml` for the
`!include`-able list pattern) and/or a runnable system file at
`examples/<name>_*.yaml` (see `meteo_multi_city.yaml`) wiring it into a
minimal `tasks:`/`intervals:` setup that reads or writes the device's
endpoints. Confirm which shape fits before writing it if the device
doesn't obviously match one of the existing examples' style.

## 5. Tests

Add `tests/test_<name>.py` covering `receive`/`transmit` (or their async
counterparts), including the failure-to-`None` path. Follow
`tests/test_meteoswiss.py`'s pattern of driving the device through a real
`Scheduler` against a throwaway local server/fixture rather than mocking
internals.

## 6. Package data

Bundled modules are already covered by the `"phc.devices" =
["*/module.yaml"]` wildcard in `pyproject.toml` — nothing to add there
for a new `phc/devices/<name>/`. Only touch
`[tool.setuptools.package-data]` if the module ships extra non-`.py`
files beyond `module.yaml` (rare), or per the doc's "Shipping a module
outside PHC" section for an out-of-tree distribution.

## 7. Changelog, tests, and commits

Follow this repo's standing conventions for the rest:
[`instructions/changelog.md`](../instructions/changelog.md) (a **New
features** entry),
[`instructions/git-workflow.md`](../instructions/git-workflow.md)
(dedicated branch, separate commits per phase — code, docs/example
config, tests, changelog), and run `pytest`, `ruff check phc tests`, and
`mypy` before considering the module done (see `CONTRIBUTING.md`).
