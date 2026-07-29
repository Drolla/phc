# Configuration reference

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
    latitude: 47.3769
    longitude: 8.5417
```

The path is resolved relative to the file the `!include` appears in, not the
root config or the current directory, so an included file can itself use
`!include` to pull in further files. This is a plain substitution (the tagged
node is replaced by the included file's parsed content) rather than a merge,
so a shared fragment works best when it's a fully self-contained block, e.g.
a whole device (as `common/living_light_device.yaml` is above).

For a fragment that only supplies *some* of a mapping's fields -- e.g. a
device's or module's shared params, alongside other fields (`update:`, `id:`)
that differ per file -- use `<<: !include <relative-path>` instead, which
merges the included mapping's keys into the surrounding mapping rather than
replacing it wholesale. The surrounding mapping's own keys win over the
fragment's, the same precedence a plain YAML `<<: *anchor` merge already has:

```yaml
# common/sun_zurich_params.yaml
latitude: 47.3769
longitude: 8.5417
timezone: Europe/Zurich
```

```yaml
devices:
  - id: sun
    module: sun
    update: 1h              # this device's own field, not in the fragment
    <<: !include common/sun_zurich_params.yaml
```

See [`examples/common/`](../examples/common/) for fragments shared between
several of the example systems, and any example file under
[`examples/`](../examples/) that references them for real usage.

## Modules and shared configuration

A `module.yaml` declares each parameter's `scope` (default `device`) and
`override` (default `allowed`; `required` or `none` are the other two). A
declared parameter is an ordinary top-level field, set directly on a device
entry (`scope: device`) or under that module's entry in the top-level
`modules:` section (either scope) -- there is no `params:` nesting. The
top-level `modules:` section lets several devices of one module type share
configuration instead of repeating it on every device:

```yaml
modules:
  zway:
    update: zwave                       # falls between a device's own update: and module.yaml's default
    <<: !include common/zway_controller_params.yaml   # base_url/user/password/cache_time, shared by every zway device

devices:
  - id: light_corridor
    module: zway
    endpoints: [ ... ]   # no params of its own -- both come from modules.zway above
  - id: sensor_garage
    module: zway
    base_url: "http://a-different-controller:8083"   # overrides just this one param
    endpoints: [ ... ]
```

Precedence for a `scope: device` param: the device's own field → the same
field set directly under `modules.<name>` → the module's own `default:`. A
param declared `override: required` (e.g. zway's `base_url`) can be satisfied
by either the device or the module-level value — this is what lets every
device behind one controller omit `base_url` entirely once it's set under
`modules.zway`. `override: none` rejects a value being set anywhere but the
module's own `default:`. `modules.<name>.update` works the same way for a
device's update interval: device `update:` → `modules.<name>.update` → the
module's own `update:` default. `update` is the one key reserved at the
`modules.<name>` level -- a module cannot declare a parameter named `update`,
or `params` (reserved even though it's no longer a device/modules key
either, since a parameter literally named `params` would be indistinguishable
from the old nested-dict spelling).

A parameter declared `scope: module` (e.g. `meteoswiss`'s `data_url`/
`cache_time`) is different: it has exactly one value for every device of
that module type, settable *only* directly under `modules.<name>` — setting
it on a device is a `ConfigError`.

A module may similarly declare `endpoint_parameters:` — its own per-endpoint
protocol fields (e.g. zway's `command_group`/`address`), a list of `{name,
description}` entries mirroring `parameters:`'s device-level schema, but
with no `default`/`override`/`scope` (an endpoint has no equivalent of
`modules.<name>` to resolve against). A declared name becomes a legal
top-level key on any endpoint spec of that module, folded into
`Endpoint.params` once every profile/overlay/`{param}` step has resolved —
see [Endpoint and device profiles](profiles.md) for how that combines with
profiles. There is no `params: { ... }` nesting on a device entry or an
endpoint any more; an undeclared field anywhere on either (a typo, or a
value meant for the other one) is a `ConfigError` naming the field.

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

## Time and duration strings

`update:` intervals, a task's `repeat:`/`min_interval:`, and window bounds
(e.g. [`extensions/random_light`](random-light.md)'s `windows:`) accept a
**duration** string: a plain number of seconds, or a compact string
combining fixed-length units `ms`/`s`/`m`/`h`/`D` (day)/`W` (week), e.g.
`"10s"`, `"1h30m"`, `"2D12h"`. Calendar units (`Y`/`M`) aren't valid in a
duration, since they aren't a fixed number of seconds.

A task's `time:` accepts a **time** spec instead:

- a plain integer string — a literal Unix timestamp
- `"+<compact>"` (e.g. `"+1Y2M3D4h"`) — now, shifted by the given calendar
  (`Y`, `M`) and/or fixed (`W`, `D`, `h`, `m`, `s`, `ms`) offset
- `"<compact h/m/s only>"` (e.g. `"11h59m59s"`) — today at that
  time-of-day; rolled forward to tomorrow if already in the past
- `"HH:MM[:SS]"` — today at that time-of-day, no automatic roll-forward
- an ISO 8601 date/datetime string — no automatic roll-forward

If a task also sets `repeat:` and the resulting time is already in the
past, it's advanced by whole multiples of `repeat` until it's `>= now`, so
a stalled process catches up instead of firing a burst.
