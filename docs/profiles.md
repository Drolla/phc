# Endpoint and device profiles

A module can also declare a reusable library of endpoints in its
`module.yaml`, split along two independent axes: an **endpoint profile**
captures the *access pattern* — type, units, writability, and (for a module
with `endpoint_parameters:`) protocol fields like zway's `command_group` —
shared by several products, while a **device profile** names one *product*,
with optional `brand`/`type`/`product`/`description` metadata plus a keyed
`endpoints:` list that completes each endpoint profile with what's specific
to that product (typically an `address`, which an endpoint profile never
hardcodes, since the same access pattern wires up differently on different
hardware). A profile name never needs a module-name prefix — a device's
`device_profile:`/`endpoint_profile:` only ever resolves against its own
`module:`'s library, so e.g. a `zway` device can't accidentally reference a
`meteoswiss` profile even if both declared one under the same name:

```yaml
# devices/zway/module.yaml
endpoint_parameters:
  - name: command_group
  - name: address

endpoint_profiles:
  sensor_multilevel_temperature: { type: float, unit: "°C", command_group: SensorMultilevel }
  battery: { type: int, unit: "%", command_group: Battery }

device_profiles:
  everspring-st814:
    brand: Everspring
    product: ST814
    description: Temperature/Humidity Sensor
    endpoints:
      - { key: temp, endpoint_profile: sensor_multilevel_temperature, address: "{node}.0.1" }
      - { key: battery, endpoint_profile: battery, address: "{node}" }
```

```yaml
devices:
  - id: multi_liv
    module: zway
    name: Living Room Multisensor
    device_profile: everspring-st814   # whole device, from device_profiles
    node: 11                           # fills in every {node} template above
    endpoints:
      - { key: temp, name: "Living Room Temperature" }   # complete a profile-derived endpoint by key
  - id: fus18_meteo
    module: zway
    node: 15
    endpoints:
      - key: f18_temp
        endpoint_profile: sensor_multilevel_temperature   # single endpoint, no device profile
        address: "{node}.0.1"
```

A device's own `endpoints:` overlays whatever its `device_profile:`/
`endpoint_profile:` provided, by `key` — replacing only the fields it sets
(e.g. `address:`), so tweaking one value doesn't drop a profile-derived
sibling like `command_group`. Writing an endpoint out fully explicitly,
with neither key anywhere on the device, keeps working exactly as before —
profiles are a shortcut, not a replacement for the underlying
`key`/`type`/`values`/... spec plus whatever the module's own
`endpoint_parameters:` declare.

`{param}` template substitution itself is independent of profiles: it runs
on every endpoint's fields for every device of every module, whether that
endpoint came from a profile, an instance override, or a module's own
unconditional `endpoints:`. A module that never declares any templates
(most of them, today) is unaffected, since a spec with no `{...}` anywhere
just passes through unchanged.

See [`devices/zway/module.yaml`](../devices/zway/module.yaml) for the full
product list and [`examples/zway_system.yaml`](../examples/zway_system.yaml)
for a worked example mixing a whole-device profile with named endpoints, a
device profile with one field overridden, and a single endpoint profile
without a device profile.

## Extending a module's profile library from a system config

A system config can add to a module's `device_profiles`/`endpoint_profiles`
library too, under that module's own entry in the top-level `modules:`
section (see [Modules and shared configuration](configuration.md)), using
exactly the same shape `module.yaml` uses:

```yaml
modules:
  virtual:
    device_profiles:
      siren:
        endpoints:
          - key: state
            writable: true
            type: int
            values: { 0: "off", 1: "on" }
            default: 0

devices:
  - id: siren_hallway
    module: virtual
    device_profile: siren
  - id: siren_garage
    module: virtual
    device_profile: siren
```

Resolution stays module-scoped exactly like a `module.yaml`-declared
profile — `device_profile: siren` above only resolves against `virtual`,
invisible to a device of any other module. A name colliding with one
`module.yaml` already declares is a `ConfigError`, not a silent override,
and (as within `module.yaml` itself) `device_profiles` can't be combined
with a module whose own `endpoints:` is non-empty (e.g. `meteoswiss`) —
same base/overlay ambiguity either way.

Reach for this instead of editing the module's own `module.yaml` when a
profile is specific to your setup rather than a real shared product — e.g.
a `virtual` siren with no real hardware behind it doesn't belong in
`devices/virtual/module.yaml`'s generic library. It also composes with
`<<: !include` for free, since that's a plain YAML merge key: a
`device_profiles:` block can live in a shared `common/*.yaml` file included
from multiple system configs, the same way
[`examples/common/zway_controller_params.yaml`](../examples/common/zway_controller_params.yaml)
is today. See [`examples/virtual_system.yaml`](../examples/virtual_system.yaml)
for a worked example.
