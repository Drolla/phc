# Endpoint and Device Profiles

A module can also declare a reusable library of endpoints in its
`module.yaml`, split along two independent axes: an **endpoint profile**
captures the *access pattern* — type, units, writability, and (for a module
with `endpoint_parameters:`) whatever protocol fields that pattern always
implies — shared by several endpoints or products, while a **device
profile** names one *product*, with optional
`brand`/`type`/`product`/`description` metadata plus a keyed `endpoints:`
list that completes each endpoint profile with what's specific to that
product (typically an address, which an endpoint profile never hardcodes,
since the same access pattern wires up differently on different hardware).
A profile name never needs a module-name prefix — a device's
`device_profile:`/`endpoint_profile:` only ever resolves against its own
`module:`'s library, so e.g. a `zway` device can't accidentally reference a
`meteoswiss` profile even if both declared one under the same name.

`zway` ships the endpoint-profile axis alone: the controller's virtual
devices already abstract away the physical hardware, so an endpoint needs
only its access pattern plus a `device:` naming which virtual device it
reads (see [Razberry/Z-Way Z-Wave integration](zway.md)):

```yaml
# phc/devices/zway/module.yaml
endpoint_parameters:
  - name: device
  - name: match_case

endpoint_profiles:
  switch:      { type: bool, writable: true, values: { false: "off", true: "on" } }
  temperature: { type: float, unit: "°C" }
  battery:     { type: int, unit: "%" }
```

```yaml
devices:
  - id: sensors_indoor
    module: zway
    name: Indoor Sensors
    endpoints:
      - key: living_temp
        name: Living Room Temperature
        endpoint_profile: temperature     # single endpoint, no device profile
        device: "*living temp*"
```

The device-profile axis suits a module whose products differ in wiring
rather than in access pattern — several endpoints at addresses that vary
per product, built from the same handful of endpoint profiles:

```yaml
# a module.yaml declaring an `address` endpoint parameter
device_profiles:
  acme-multisensor:
    brand: Acme
    product: MS-3
    description: Temperature/Humidity Sensor
    endpoints:
      - { key: temp, endpoint_profile: temperature, address: "{node}.0.1" }
      - { key: battery, endpoint_profile: battery, address: "{node}" }
```

```yaml
devices:
  - id: multi_liv
    module: acme
    name: Living Room Multisensor
    device_profile: acme-multisensor   # whole device, from device_profiles
    node: 11                           # fills in every {node} template above
    endpoints:
      - { key: temp, name: "Living Room Temperature" }   # complete a profile-derived endpoint by key
```

A device's own `endpoints:` overlays whatever its `device_profile:`/
`endpoint_profile:` provided, by `key` — replacing only the fields it sets
(e.g. an `address:`), so tweaking one value doesn't drop a profile-derived
sibling. Writing an endpoint out fully explicitly, with neither key
anywhere on the device, keeps working exactly as before — profiles are a
shortcut, not a replacement for the underlying `key`/`type`/`values`/...
spec plus whatever the module's own `endpoint_parameters:` declare.

`{param}` template substitution itself is independent of profiles: it runs
on every endpoint's fields for every device of every module, whether that
endpoint came from a profile, an instance override, or a module's own
unconditional `endpoints:`. A module that never declares any templates
(most of them, today) is unaffected, since a spec with no `{...}` anywhere
just passes through unchanged.

See [`phc/devices/zway/module.yaml`](../phc/devices/zway/module.yaml) for a
full endpoint-profile library and
[`examples/devices/zway_devices.yaml`](../examples/devices/zway_devices.yaml)
for a worked example using it, endpoint by endpoint.


## Extending a Module's Profile Library From a System Config

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
            type: bool
            values: { false: "off", true: "on" }
            default: false

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
[`phc/devices/virtual/module.yaml`](../phc/devices/virtual/module.yaml)'s generic library. It also composes with
`<<: !include` for free, since that's a plain YAML merge key: a
`device_profiles:` block can live in a shared fragment file included from
multiple system configs (see [Splitting configuration across
files](configuration.md#splitting-configuration-across-files)). See
[`examples/emulated_system.yaml`](../examples/emulated_system.yaml) for a
worked example.
