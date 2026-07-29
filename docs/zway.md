# Razberry/zWay Z-Wave integration

[`devices/zway/`](../devices/zway/) controls Z-Wave devices through a
Razberry/zWay controller, via `thc_zWay.js`
(https://github.com/Drolla/thc/tree/master/modules/thc_zWay), a small helper
script ported from the earlier THC project that you install on the zWay
server yourself (PHC does not push it there). One `zway` device is one
physical Z-Wave node; give it whatever endpoints that node needs (a switch's
`state`, a sensor's `battery`, ...), each naming its own zWay identifier via
`command_group` and `address`, written as an ordinary top-level field on
the endpoint:

```yaml
modules:
  zway:
    update: 30s
    base_url: "http://192.168.1.21:8083"
    user: admin
    password: admin

devices:
  - id: light_corridor
    module: zway
    endpoints:
      - key: state
        writable: true
        type: int
        values: { 0: "off", 255: "on" }
        command_group: SwitchBinary
        address: "7.1"
```

`command_group` is one of `SwitchBinary`, `SwitchMultilevel`,
`SwitchMultiBinary`, `SensorBinary`, `SensorMultilevel`, `Battery`, or
`TagReader`; `address` is an opaque zWay `"node.instance[.datarecord]"`
identifier, passed through verbatim. A device with a `TagReader` endpoint
additionally needs its own `node` param set (the zWay node number) — used
for a one-time `Configure_TagReader` setup call the first time that device
is polled, the same `node` used to fill in any `{node}` template below.

`devices/zway/module.yaml` ships a two-axis profile library instead of
writing every endpoint out fully explicitly: an `endpoint_profile` (named
after its command group, e.g. `switch_binary`,
`sensor_multilevel_temperature` — no module-name prefix needed, since a
profile is only ever resolved against the module of the device referencing
it) captures the *access pattern* — type, units, writability — shared by
every product using that command group, while a `device_profile` names one
*product* — e.g. `fibaro-fgs222`, `everspring-st814`, `popp-z-weather`
(see [`devices/zway/module.yaml`](../devices/zway/module.yaml) for the full
list) — supplying that product's own addresses, which an `endpoint_profile`
never hardcodes (the same command group wires up differently on different
hardware). Set `node:` and `device_profile:` directly on the device to get
a whole product's endpoints at once, then complete them by `key` with a
human-readable `name:`:

```yaml
devices:
  - id: light_corridor
    module: zway
    name: Corridor Light Switch
    device_profile: fibaro-fgs222
    node: 7
    endpoints:
      - { key: sw1, name: "Corridor Light" }
      - { key: sw2, name: "Closet Light" }
```

See [Endpoint and device profiles](profiles.md) for how the two libraries
combine, and [`examples/zway_system.yaml`](../examples/zway_system.yaml) for
a worked example mixing a whole-device profile with named endpoints, a
device profile with one field overridden, and a single endpoint profile
without a device profile.

Every `zway` device behind the same controller (`base_url`) self-registers
its endpoints' identifiers into a shared, module-level registry; whichever
device is due first each poll window issues one combined status request
covering *every* currently-registered identifier for that controller, cached
for `cache_time` (default `30s`) — so a whole controller's worth of
Z-Wave devices coalesces into a single HTTP request per poll, rather than
one round-trip per device. Set the same `update` interval on every device
behind one controller to keep them polling together and get full sharing —
typically by setting both `update` and the shared params once under
`modules.zway`, as above, rather than repeating them on every device.

See [zway internals](developer/zway.md) for the batching/caching/auth
architecture behind this, and per-product wiring notes in the profile
library.
