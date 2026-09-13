# Razberry/Z-Way Z-Wave Integration

[`phc/devices/zway/`](../phc/devices/zway/) controls the **virtual devices**
a Z-Way (Razberry) controller exposes, over its ZAutomation WebSocket API.
Nothing needs to be installed on the controller — no helper script, no
automation module — only a URL and an API token, issued in the controller's
own user profile:

```yaml
modules:
  zway:
    update: 1s
    url: "ws://192.168.1.21:8083"
    token: !placeholder <Token>       # "<id>/<uuid>", from the user profile

devices:
  - id: lights
    module: zway
    name: House Lights
    endpoints:
      - key: corridor
        name: Corridor Light
        device: "Rez: Light corridor"
        endpoint_profile: switch
```

A virtual device is the controller's own abstraction over the physical
Z-Wave hardware: a two-relay module shows up as two independent switches, a
multisensor as one virtual device per reading. PHC talks to that layer and
nothing below it, so there is no node number, instance, command class or
product wiring anywhere in the config — those stay the controller's
business.

One `zway` device therefore groups whatever virtual devices you want to see
together (a room's lights, a floor's sensors), one per endpoint. It is not
tied to one piece of hardware.


## Naming a Virtual Device

Each endpoint names its virtual device with `device:`, an
[fnmatch](https://docs.python.org/3/library/fnmatch.html) glob matched
against **both** the device id and its title — a hit in either counts:

```yaml
- key: parent_room
  device: ZWayVDev_zway_7-1-37       # the generated id, stable across renames
  endpoint_profile: switch
- key: living
  device: "Rez: Light living"        # the title, usually easier to read
  endpoint_profile: switch
- key: living_temp
  device: "*living temp*"            # a glob, if that pins down one device
  endpoint_profile: temperature
```

The pattern must match **exactly one** device. Matching none, or several,
is a `ConfigError` naming the candidates it did find — so an ambiguous glob
tells you what to narrow it against rather than silently picking one.

Matching **ignores case** by default, unlike PHC's selector globs
elsewhere: Z-Way titles are hand-written on the controller and
inconsistently capitalised. Set `match_case: true` on an endpoint to opt
back into case-sensitivity.

Patterns are resolved to device ids **once per connection**, right after
the device list is read; everything after that addresses the controller by
id. So a glob is config-time convenience, never a per-poll cost — and a
device renamed on the controller is picked up on the next reconnect or
resync, not on the next read.


## Finding Out What to Put There

The module ships a standalone demo that lists a controller's devices
without loading a PHC config at all — the quickest way to see what `device:`
can name:

```
python phc/devices/zway/demo.py --url ws://192.168.1.132:8083 --token T list
```

It prints each device's id, title, deviceType, and both its raw wire level
and the PHC value that translates to. `get DEVICE` shows one device,
`on`/`off`/`set DEVICE VALUE` write one (useful for confirming a device
does what you think before wiring it into a config), and `--raw` prints the
controller's own JSON instead of the summary. `DEVICE` is the same glob the
YAML takes, including `--match-case`; add `--verbose` to log every request
sent.


## Values and Types

Readings are translated into PHC's own types rather than passed through
raw:

- A binary device reports the strings `"on"`/`"off"` on the wire, and
  appears in PHC as a real `bool` — so a task or condition compares against
  `true`/`false`, never against a protocol spelling. (This matters more
  than it looks: `bool("off")` is `True`.)
- An unreadable, empty or unrecognised level reads as `None`, as does a
  device the controller flags as failed. Never a stand-in value.

The translation table lives in
[`phc/devices/zway/module.yaml`](../phc/devices/zway/module.yaml) under
`zway_types:`, keyed by the `deviceType` the controller reports
(`switchBinary`, `switchMultilevel`, `sensorMultilevel`, `battery`, ...).
Supporting a Z-Way device type that isn't listed there yet is a change to
that table, not to any Python. A type that is genuinely unknown has its
level passed through unchanged on reads, and refuses writes.

`endpoint_profiles` give the endpoint its PHC-side shape — `switch`,
`dimmer`, `motion`, `temperature`, `humidity`, `luminosity`, `pressure`,
`battery` — declaring type, unit, writability and display values so a
config doesn't spell them out per endpoint. See [Endpoint and device
profiles](profiles.md).


## Writes

Which commands a device accepts follows the `deviceType` the **controller**
reports for it, and PHC checks that locally before sending anything. That
check is not belt-and-braces: the controller answers `200 OK` to commands
that make no sense for the device (`exact?level=50` to a binary switch,
`on` to a temperature sensor), so without it a config mistake would fail
completely silently. Sensors and battery endpoints are read-only, and any
write to one is rejected with a failure naming the endpoint.

> **Dimmer quirk:** writing `true` to a `switchMultilevel` lands at level
> **99** and `false` at **0**, so a bool write to a dimmer reads back as a
> number rather than as the bool that was written. Write the level you want
> directly if that matters.


## Connection, Freshness and Failure

Every device sharing a `url` and `token` shares **one** WebSocket — which
is why both are normally set once under `modules.zway` rather than repeated
per device (see [Modules and shared
configuration](configuration.md#modules-and-shared-configuration)). Devices
on a different physical controller need their own `url` and `token`, and
simply get their own connection.

The controller **pushes** state events whenever any device changes, so an
endpoint is typically fresh within a tick of the change rather than at the
next poll — a short `update:` costs no extra traffic here. A full re-read
of every device every `resync_interval` (default `5m`) is the safety net
behind those pushes, for a dropped event or a device that reports through
some other channel. An idle socket is silent; that is normal, not a
failure.

A controller that is unreachable is retried after **1s, 10s, 60s, and then
every 5 minutes**. While it is down every endpoint reads `None` and the
device is marked unhealthy (see [Device health](concepts.md#device-health)).
On reconnect the device list is re-read and every `device:` pattern
re-resolved, so devices renamed while it was gone are picked up.

`request_timeout` (default `5s`) bounds one reply. It is also the only
thing that catches a bad or empty API token: such a token still completes
the WebSocket handshake, and the controller then simply never answers.

See [zway internals](developer/zway.md) for the architecture behind this,
and [`examples/zway_system.yaml`](../examples/zway_system.yaml) with
[`examples/devices/zway_devices.yaml`](../examples/devices/zway_devices.yaml)
for a worked configuration.
