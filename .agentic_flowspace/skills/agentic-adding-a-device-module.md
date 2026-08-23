# Adding a Device Module

Applies when the user asks to add support for a new device, sensor,
actuator, or protocol in PHC: a new `phc/devices/<name>/` package, or an
out-of-tree module/plugin (see the "Shipping a module outside PHC"
section of the doc below).


## 1. Clarify the Physical Device First

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


## 2. Read the Template and the Pattern

Read [`examples/device-template/device_template/`](../../examples/device-template/device_template/)
— a complete, working module written to be copied. Its `device.py` and
`module.yaml` demonstrate the whole surface in one place: `setup()`,
`receive_async`/`transmit_async` (with the blocking `receive`/`transmit`
alternative commented alongside), `report_failure`, `self.context` for
state shared between a module's devices, an `endpoint_parameters` field,
and every `override`/`scope` combination a parameter can declare.

Then read
[`docs/developer/writing-a-device-module.md`](../../docs/developer/writing-a-device-module.md)
for the reference behind it — the same pattern explained, plus
`device_profiles`/`endpoint_profiles` and how to ship a module outside
PHC. Also skim
[`docs/developer/architecture.md`](../../docs/developer/architecture.md)
for how a device module fits into the rest of PHC, and
[`docs/configuration.md`](../../docs/configuration.md) /
[`docs/profiles.md`](../../docs/profiles.md) if the device needs shared
module config or a profile library.

Do NOT copy a caching or shared-state shape from an arbitrary existing
module without checking it against the template: state shared between a
module's devices belongs in `self.context`, never at module scope.


## 3. Scaffold the Module

**Copy the template rather than writing from scratch.** Copy
[`examples/device-template/device_template/`](../../examples/device-template/device_template/)
to the location settled in step 1 (`phc/devices/<name>/`, or an equivalent
package out-of-tree), then:

- Rename it in `@register_module()`, the class name, and `module.yaml`.
- Replace `_read_payload()`/`_write_payload()` with the real protocol —
  they are the template's entire I/O surface, deliberately isolated so
  this is a single-site edit.
- Replace the parameters and endpoints with the ones settled in step 1.
- Delete what this device doesn't need: the write half for a read-only
  device, `_TemplateState`/`self.context` if its devices share nothing,
  and every part marked `SIMULATION ONLY` (including the
  `simulate_failure` parameter and `_SIMULATED_UNITS`).

`module.yaml` `description` fields are user-facing (rendered in the web
UI) — plain English, not implementation notes; put implementation
rationale in `device.py` docstrings instead.


## 4. Propose an Example Configuration

Once the module works, propose — and on confirmation, implement — an
example system config under `examples/` that demonstrates it end to end.
Follow the existing conventions: a bare device-list file at
`examples/devices/<name>_*.yaml` (see [`meteoswiss_stations.yaml`](../../examples/devices/meteoswiss_stations.yaml) for the
`!include`-able list pattern) and/or a runnable system file at
`examples/<name>_*.yaml` (see [`meteo_multi_city.yaml`](../../examples/meteo_multi_city.yaml), or
[`device_template_system.yaml`](../../examples/device_template_system.yaml)
for the template's own minimal one) wiring it into a
minimal `tasks:`/`intervals:` setup that reads or writes the device's
endpoints. Confirm which shape fits before writing it if the device
doesn't obviously match one of the existing examples' style.


## 5. Tests

Add `tests/test_<name>.py` covering `receive`/`transmit` (or their async
counterparts), including the failure-to-`None` path. Follow
[`tests/test_device_template.py`](../../tests/test_device_template.py),
the template's own test, for the shape — and
[`tests/test_meteoswiss.py`](../../tests/test_meteoswiss.py) for the same
thing against a throwaway local HTTP server. Both drive the device through
a real `Scheduler` rather than mocking internals.

If the module shares state between its devices, note how those tests pass
one `context` dict to several devices to exercise it — a directly
constructed `Device` otherwise gets its own.


## 6. Package Data

Bundled modules are already covered by the `"phc.devices" =
["*/module.yaml"]` wildcard in [`pyproject.toml`](../../pyproject.toml) — nothing to add there
for a new `phc/devices/<name>/`. Only touch
`[tool.setuptools.package-data]` if the module ships extra non-`.py`
files beyond `module.yaml` (rare), or per the doc's "Shipping a module
outside PHC" section for an out-of-tree distribution.


## 7. Changelog, Tests, and Commits

Follow this repo's standing conventions for the rest:
[`instructions/changelog.md`](../instructions/changelog.md) (a **New
features** entry),
[`instructions/git-workflow.md`](../instructions/git-workflow.md)
(dedicated branch, separate commits per phase — code, docs/example
config, tests, changelog), and run `pytest`, `ruff check phc tests`, and
`mypy` before considering the module done (see [`CONTRIBUTING.md`](../../CONTRIBUTING.md)).
