<p align="center">
  <img src="docs/logo.png" alt="Pylon Home Control logo" width="220">
</p>

# Pylon Home Control (PHC)

Pylon Home Control (PHC) is a small, YAML-configured home automation
framework. It polls and controls a tree of pluggable **devices** (weather
stations, sun position, virtual/test devices, and anything you add), and
runs **tasks** — condition- or time-driven automations — against their
state, all on a fixed-heartbeat scheduler.

## Concepts

- **Device** — a node in a tree that exposes zero or more **endpoints**
  (readable/writable state) and may have child devices. Devices are backed
  by a plugin **module** (e.g. `meteoswiss`, `open_meteo`, `waveplus_bridge`,
  `zway`, `sun`, `virtual`, `host`), declared in a system YAML file.
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
- `min`/`max` — a numeric range hint, stored only (never enforced against
  a write) — e.g. used by [`extensions/web_ui/`](extensions/web_ui/) to
  decide whether a writable numeric endpoint gets a bounded slider.

Given these, `Endpoint.to_text()`/`from_text()` (and the matching
`Device.get_text()`/`set_text()`) are the standard way to format a raw
value as display text and to parse text (or a raw value/label, e.g. `1` or
`"on"`) back into the endpoint's raw value — used by the `log` action's
`{text}` placeholder and the `set` action's `value:` parameter.

See [`examples/`](examples/) for complete system configurations, and the
`module.yaml` file in each [`devices/`](devices/) subfolder for what
parameters/endpoints a given device module supports.

[`extensions/`](extensions/) is the home for non-device PHC extensions
(e.g. [`extensions/logdb/`](extensions/logdb/), a CSV-backed sample store,
and [`extensions/random_light/`](extensions/random_light/), randomized
light control), following the same package-plus-descriptor pattern as
device modules.

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

### Random light control

[`extensions/random_light/`](extensions/random_light/) randomizes a set of
"light" devices to make an empty house look occupied — each light gets one
or more on/off time-of-day windows (a fixed local `"HH:MM"`, or
`"sunrise"`/`"sunset"` plus/minus an offset, resolved against a
[`devices/sun/`](devices/sun/) device's live sunrise/sunset), a minimum
switch interval, and a probability of being on. `windows`/`min_interval`/
`probability_on` cascade three ways: [`extension.yaml`](extensions/random_light/extension.yaml)'s
own default → this instance's own `windows`/`min_interval`/`probability_on`
(applies to every light below that doesn't set its own) → each light's own
override:

```yaml
extensions:
  random_light:
    house:
      enable_ref: "surveillance.armed"   # optional: only randomize while armed
      pause_ref: "alarm.state"           # optional: skip entirely during an active alarm
      lights:
        - device: "hallway_light.state"
          default: true   # forced on if, after a pass, no light ended up on
          # no windows/min_interval/probability_on of its own -- inherits
          # extension.yaml's own defaults (see below)
        - device: "porch_light.state"
          windows:
            - { start: "sunset+12m", end: "23:30" }
            - { start: "06:00", end: "sunrise-10m" }
          min_interval: 15m
          probability_on: 0.4

tasks:
  - tag: random_light_tick
    time: "+5s"
    repeat: 1m
    action: { kind: random_light, instance: "random_light.house" }
```

A `kind: random_light` action with `force: 0`/`force: 1` bypasses windows,
probability, and `enable_ref`/`pause_ref` entirely, forcing every
configured light to that value immediately — for a surrounding system to
drop into its own tasks' `actions:` list (e.g. force everything off when
arming/disarming, force everything on as a deterrent during an alarm), as
seen throughout
[`examples/surveillance_system.yaml`](examples/surveillance_system.yaml).

### Mail alerts

[`extensions/mail_alert/`](extensions/mail_alert/) sends a message through
one configured SMTP server — an `extensions.mail_alert.<instance>` entry
holds the server's connection details plus a default sender/recipient
list, and a `kind: mail_alert` task action sends one message through it,
alongside a task's other actions (`set`, `random_light`, ...). A recipient
can be an ordinary mailbox or an email-to-SMS gateway address — both are
just SMTP recipients as far as this extension is concerned. Because a
task's actions run inline on the scheduler's own thread (see Task/Action
above), the actual SMTP send happens on a small background thread pool
instead, so a slow or unreachable mail server can't stall the whole
system — delivery success/failure is logged, not surfaced back to the
firing task:

```yaml
extensions:
  mail_alert:
    house:
      smtp_host: "smtp.example.com"
      username: "alerts@example.com"
      password: "..."           # plain text -- phc has no secrets mechanism; guard this file accordingly
      from: "alerts@example.com"
      to:
        - "someone@example.com"
        - "15555550123@sms.example.com"   # an email-to-SMS gateway, same as any other recipient

tasks:
  - tag: intrusion_alert
    condition: { device: "hallway_motion.state" }
    min_interval: 5m   # don't refire more than once every 5 minutes
    actions:
      - kind: set
        device: "siren.state"
        value: 1
      - kind: mail_alert
        instance: "mail_alert.house"
        title: "Home Security - Alarm Alert"
        message: "Sensor triggered"
```

`to`/`from` on the action itself override the instance's defaults when
given. See
[`examples/surveillance_system.yaml`](examples/surveillance_system.yaml)
for a fuller worked example, wired into its intrusion-detection task.

### Razberry/zWay Z-Wave integration

[`devices/zway/`](devices/zway/) controls Z-Wave devices through a
Razberry/zWay controller, via `thc_zWay.js`
(https://github.com/Drolla/thc/tree/master/modules/thc_zWay), a small helper
script ported from the earlier THC project that you install on the zWay
server yourself (PHC does not push it there). One `zway` device is one
physical Z-Wave node; give it whatever endpoints that node needs (a switch's
`state`, a sensor's `battery`, ...), each naming its own zWay identifier via
`parameters`:

```yaml
modules:
  zway:
    update: 30s
    params: { base_url: "http://192.168.1.21:8083", user: admin, password: admin }

devices:
  - id: light_corridor
    module: zway
    endpoints:
      - key: state
        writable: true
        type: int
        values: { 0: "off", 255: "on" }
        parameters: { command_group: SwitchBinary, value_id: "7.1" }
```

`command_group` is one of `SwitchBinary`, `SwitchMultilevel`,
`SwitchMultiBinary`, `SensorBinary`, `SensorMultilevel`, `Battery`, or
`TagReader`; `value_id` is an opaque zWay `"node.instance[.datarecord]"`
identifier, passed through verbatim. A `TagReader` endpoint additionally
needs `node_id`, used for a one-time setup call the first time that device
is polled. `devices/zway/module.yaml` also ships an endpoint/device
profile library for the common case where one node's endpoints all derive
from the same node number — see [Endpoint and device
profiles](#endpoint-and-device-profiles) above.

Every `zway` device behind the same controller (`base_url`) self-registers
its endpoints' identifiers into a shared, module-level registry; whichever
device is due first each poll window issues one combined status request
covering *every* currently-registered identifier for that controller, cached
for `cache_time` (default `30s`) -- so a whole controller's worth of
Z-Wave devices coalesces into a single HTTP request per poll, the same
batching the old THC `thc_zWay` module did, rather than one round-trip per
device. Set the same `update` interval on every device behind one
controller to keep them polling together and get full sharing -- typically
by setting both `update` and `params` once under `modules.zway`, as above,
rather than repeating them on every device. See
[`examples/zway_system.yaml`](examples/zway_system.yaml) for a worked
example with a light switch, a motion+battery sensor, and a TagReader node.

### Web UI

[`extensions/web_ui/`](extensions/web_ui/) is a small aiohttp.web server
(sharing the scheduler's own event loop) that renders the live device tree
as a browser dashboard — view current status and flip/slide/select new
values — with **no per-device UI code**: each endpoint's widget is
inferred purely from its existing metadata:

| Endpoint                                  | Widget     |
|--------------------------------------------|------------|
| not `writable`                              | label      |
| `writable`, `type: bool`                    | toggle     |
| `writable`, has `values`                    | dropdown   |
| `writable`, numeric, both `min` and `max`   | slider     |
| `writable`, numeric, missing `min` or `max` | number     |
| `writable`, `str` or untyped                | text       |

Layout is either a single flat page (the `selectors` shorthand, default
everything) or an explicit `pages:` list, each holding one or more
collapsible `sections:` (folded by default) that pick their devices via
the same selector syntax `extensions/logdb` uses:

```yaml
extensions:
  web_ui:
    home:
      host: 127.0.0.1
      port: 8080
      refresh_interval: 2s
      pages:
        - id: overview
          title: Overview
          sections:
            - id: lights
              title: Lights
              collapsed: false
              selectors: ["house.*.light*/*"]
            - id: climate
              title: Climate
              selectors: ["house.*/temperature", "house.*/humidity"]
```

Writes POST through the same `Device.set_text()` path a task action uses;
every widget independently polls its own small HTML fragment on
`refresh_interval` to pick up live state (its own write included, once the
next scheduler tick commits it — there is no WebSocket/push channel).
Interactivity is [HTMX](https://htmx.org) and styling is
[Bootstrap](https://getbootstrap.com) (CSS only, no `bootstrap.bundle.min.js`
or jQuery — only Bootstrap's pure-CSS form-control classes are used, so a
widget stays fully styled and interactive immediately after its own HTMX
poll swap, with no re-initialization needed). Both are vendored, unmodified,
single-file static assets (see `extensions/web_ui/static/`), so no build
tooling is needed beyond the project's own small pieces of JS: an inline
snippet in `base.html` that mirrors OS light/dark preference onto
Bootstrap's `data-bs-theme` attribute, and `graph.js`, which mounts a
`kind: graph` panel's chart (see below).

A section's content is a list of **panels**, dispatched by `kind` (default
`"devices"`, the widgets described above) through a small registry local
to this extension (`extensions/web_ui/panels.py`), independent of
`core/registry.py`.

`kind: graph` renders a [Dygraphs](https://dygraphs.com) time-series chart
over one or more endpoints' logged history, backed by a named
`extensions/logdb` instance:

```yaml
extensions:
  logdb:
    house_log:
      selectors: ["house.desk_lamp/*"]
      csv_path: "logs/house_log.csv"

  web_ui:
    home:
      pages:
        - id: overview
          sections:
            - id: history
              title: History
              panels:
                - kind: graph
                  id: desk_lamp_history
                  logdb_instance: "logdb.house_log"
                  selectors: ["house.desk_lamp/*"]
                  title: "Desk Lamp"
                  window: 6h
                  decimation:
                    - older_than: 25h
                      factor: 3
                    - older_than: 8D
                      factor: 8
```

`id` (required, unique across this web_ui instance) addresses the panel's
own `GET /api/graph/{id}` JSON data route — fetched client-side by
`graph.js`, not embedded in the page render. `logdb_instance` (required)
is resolved lazily, at request time, so it may be declared either before
or after this `web_ui:` instance. `selectors` picks which endpoints to
plot (same syntax as `extensions/logdb`'s own `selectors`) — each must
also be covered by the referenced logdb instance, or its series is empty.
`window` (default `24h`) sets the chart's initial zoom; the full retained
history is still fetched and pannable via the range selector. `decimation`
(optional) is a list of `{older_than, factor}` tiers: samples older than
`older_than` are averaged in groups of `factor`, bounding how much history
data is shipped to the browser as it grows — see
`extensions/logdb/logdb.py`'s `LogDb.get_decimated()`.

There is no authentication — bind `host` to a trusted interface only
(defaults to `127.0.0.1`, loopback-only). See
[`examples/web_ui_system.yaml`](examples/web_ui_system.yaml) for a
complete runnable example, and
[`examples/logdb_system.yaml`](examples/logdb_system.yaml) for `kind:
graph` paired with the `logdb` instance it charts.

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
  file destination — see [Logging](#logging) below.
- `--log-level-module NAME=LEVEL` — override the level of one logger (e.g.
  `scheduler=DEBUG`) on every stream destination; repeatable.

Stop with Ctrl+C (SIGINT) or SIGTERM for a graceful shutdown.

## Logging

`log:` is a list of independently-levelled destinations:

```yaml
log:
  - dest: stdout
    levels:
      default: INFO
      scheduler: DEBUG   # per-logger override, dotted suffix of "phc.<name>"
  - dest: warn_err.log    # any dest other than stdout/stderr is a file path,
    levels:               # resolved relative to this YAML's own directory
      default: WARNING    # and appended to
```

`dest` is `stdout`, `stderr`, or any other string (a file path). Each
destination's `levels` map works like `virtual_system.yaml`'s comment
explains: `default` sets the base level for any logger that doesn't have
its own entry; every other key overrides one logger by the dotted suffix
of its `"phc.<name>"` name (`scheduler`, `tasks`, `scripting`, `logdb`,
`mail_alert`, `web_ui`, ...) — name-agnostic, so a typo'd name is silently
never matched rather than rejected. Destinations are independent: the same
logger can be `INFO` on stdout and `WARNING` in a file at the same time.
At most one destination (the first stream one) shows the scheduler's live
in-place tick countdown; a file destination never receives it. `--log-level`/
`--log-level-module` only ever affect stream destinations, so a file
destination configured to stay sparse can't be accidentally flooded from
the command line.

There is no `log_levels:` top-level key — every destination carries its own
`levels:` instead.

## Splitting configuration across files

A system YAML can pull in another YAML file with `!include <relative-path>`,
anywhere a value is expected -- a mapping value, a list item, nested
arbitrarily deep:

```yaml
devices:
  - !include common/living_light_device.yaml
  - id: sun
    module: sun
    update: 1h
    params: !include common/sun_zurich_params.yaml
```

The path is resolved relative to the file the `!include` appears in, not the
root config or the current directory, so an included file can itself use
`!include` to pull in further files. This is a plain substitution (the tagged
node is replaced by the included file's parsed content) rather than a merge,
so a shared fragment works best when it's either a fully self-contained block
(e.g. a whole device) or a nested value that doesn't vary between the files
including it (e.g. just the `params:` of a device whose other fields differ
per file). See [`examples/common/`](examples/common/) for fragments shared
between several of the example systems, and any example file under
[`examples/`](examples/) that references them for real usage.

## Modules and shared configuration

A `module.yaml` declares each parameter's `scope` (default `device`) and
`override` (default `allowed`; `required` or `none` are the other two).
`scope: device` params are normally set per device, under that device's own
`params:`. The top-level `modules:` section lets several devices of one
module type share configuration instead of repeating it on every device:

```yaml
modules:
  zway:
    update: zwave                              # falls between a device's own update: and module.yaml's default
    params: !include common/zway_controller_params.yaml   # base_url/user/password/cache_time, shared by every zway device

devices:
  - id: light_corridor
    module: zway
    endpoints: [ ... ]   # no params:/update: of its own -- both come from modules.zway above
  - id: sensor_garage
    module: zway
    params: { base_url: "http://a-different-controller:8083" }   # overrides just this one param
    endpoints: [ ... ]
```

Precedence for a `scope: device` param: the device's own `params.<name>` →
`modules.<name>.params.<name>` → the module's own `default:`. A param
declared `override: required` (e.g. zway's `base_url`) can be satisfied by
either the device or the module-level value — this is what lets every
device behind one controller omit `base_url` entirely once it's set under
`modules.zway.params`. `override: none` rejects a value being set anywhere
but the module's own `default:`. `modules.<name>.update` works the same
way for a device's update interval: device `update:` → `modules.<name>.update`
→ the module's own `update:` default.

A parameter declared `scope: module` (e.g. `meteoswiss`'s `data_url`/
`cache_time`) is different: it has exactly one value for every device of
that module type, settable *only* under `modules.<name>.params` — setting
it on a device's own `params:` is a `ConfigError`.

## Endpoint and device profiles

A module can also declare a reusable library of endpoints in its
`module.yaml`, for devices whose endpoints mostly differ by one templated
value (e.g. a Z-Wave node number). An `endpoint_profiles` entry is a full
endpoint spec — the same shape written out by hand — with `{param}`
templates in its `parameters:` values, filled in from the device's own
resolved `params`; a `device_profiles` entry is a named list of
`{key, profile}` pairs:

```yaml
# devices/zway/module.yaml
endpoint_profiles:
  temperature: { type: float, unit: "°C",
                 parameters: { command_group: SensorMultilevel, value_id: "{node}.0.1" } }
  battery:     { type: int, unit: "%",
                 parameters: { command_group: Battery, value_id: "{node}" } }
device_profiles:
  multisensor_t: [ { key: temp, profile: temperature }, { key: battery, profile: battery } ]
```

```yaml
devices:
  - id: multi_liv
    module: zway
    profile: multisensor_t   # whole device, from device_profiles
    params: { node: 11 }     # fills in every {node} template above
  - id: fus18_meteo
    module: zway
    endpoints:
      - key: f18_temp
        profile: temperature   # single endpoint, no device profile
    params: { node: 15 }
```

A device's own `endpoints:` overlays whatever its `profile:` provided, by
`key` — deep-merging just `parameters:`, so tweaking one templated value
(e.g. a node whose `value_id` doesn't follow the usual pattern) doesn't
silently drop a sibling parameter. Writing an endpoint out fully
explicitly, with no `profile:` anywhere on the device, keeps working
exactly as before — profiles are a shortcut, not a replacement for the
underlying `key`/`type`/`values`/`parameters`/... spec. See
[`devices/zway/module.yaml`](devices/zway/module.yaml) and
[`examples/zway_system.yaml`](examples/zway_system.yaml) for a worked
example mixing both styles on one system.

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
