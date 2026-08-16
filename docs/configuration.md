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
# common/some_location_params.yaml
latitude: 47.3769
longitude: 8.5417
timezone: Europe/Zurich
```

```yaml
devices:
  - id: sun_zurich
    module: sun
    update: 1h              # this device's own field, not in the fragment
    <<: !include common/some_location_params.yaml
```

This is most useful when several system YAML files in the same project
genuinely share a fragment -- e.g. a `zway:` controller's connection
params, reused across every system config talking to that same
controller -- rather than for a single system's own internal structure.

A `- !include <relative-path>` list item whose target file is itself a YAML
sequence is *spliced* into the surrounding list rather than nested as one
list-of-lists element -- so a whole topic file of several entries (e.g.
several related tasks merged into one file) composes with an ordinary
literal item in the same list, at every place a list of entries is built:
`devices:`, a `host` device's `children:`, `task_specs:`, and `tasks:`.

```yaml
# radon_tasks.yaml -- a plain list of several tasks
- tag: radon_control
  ...
- tag: radon_alert
  ...
```

```yaml
tasks:
  - tag: log_history          # an ordinary literal task, unaffected
    ...
  - !include radon_tasks.yaml # spliced in as two tasks, not one nested list
```

## Placeholder values

A value that has to be filled in with something real before a config can
run -- a credential, another system's URL -- can be tagged
`!placeholder <example>` instead of given a literal value:

```yaml
modules:
  zway:
    base_url: !placeholder <URL>
    user: !placeholder <UserName>
    password: !placeholder <Password>
```

`load_system` checks for `!placeholder` right after parsing, before
building any device or extension, and refuses to start if it finds one
anywhere in the config -- including one pulled in through `!include`/`<<:
!include` -- listing every offending field by its path (e.g.
`modules.zway.base_url`). This is how every example config that talks to
real hardware or a real mail server ships: safe to read and copy, but not
runnable until its placeholders are replaced with real values. Plain,
un-tagged example text (e.g. `smtp_host: "smtp.example.com"`) is just
illustrative and isn't checked -- use `!placeholder` for anything that
must not be left as-is.

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

Each device is polled strictly on **its own** resolved `update:` interval,
including a device nested under a `host` (or any other parent) — nesting
groups devices in the tree and in their qualified ids, but never makes one
device's poll cadence depend on another's. A child with no interval at all
(`update: null` at every level) is therefore never auto-polled, whatever
its parent does.

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

## Tasks

A task fires its `action:`/`actions:` when it's *due* and its `condition:`
(if any) holds. Every task needs a unique `tag:` (used by `kill_task`/
`create_task` to target it, see [scripting.md](scripting.md#reusable-task-templates))
and exactly one of `action:`/`actions:`.

### Gating: `condition:` and `time:`/`repeat:`

`condition:` and `time:`/`repeat:` are independent knobs, not mutually
exclusive -- a task may give either, both, or neither:

- Neither `condition:` nor `time:`/`repeat:` -- due every heartbeat tick,
  unconditionally.
- `condition:` only (no `time:`, no `repeat:`) -- due every tick, but only
  fires when the condition holds (e.g.
  `{ device: "house.motion.state", changed: true }` or an `expr:`, see
  [scripting.md](scripting.md#conditions)). This is how to express an
  always-armed rule, since there's no separate "permanent job" category.
- `time:` and/or `repeat:` given (with or without `condition:`) -- fires on
  the schedule described below, and only when the condition also holds at
  that moment if one is given (e.g. "check at 22:00, but only if the light
  is still on").

### `repeat:` — one-shot, permanent, or repeating

`repeat:` controls what happens after a due-time task fires:

- Omitted -- **one-shot**: fires once at `time:`, then the task is removed
  from the running system.
- `0` or negative -- **permanent**: `time:` only matters for the very first
  check; after that the task is due every tick, forever (subject to
  `condition:`/`min_interval:` as usual).
- A positive duration -- **repeating**: after firing, `time:` advances by
  whole multiples of `repeat:` until it's `>= now`, so a stalled process
  catches up instead of firing a burst, then the task fires again once
  that new time is reached.

`time:` is always optional. Omitting it defaults the first due-time to
"now" (the task fires on the next heartbeat) -- except with no `repeat:`
and a `condition:` given, which has no due-time schedule at all:

| `time:` | `repeat:` | `condition:` | First due-time | Behavior |
|---|---|---|---|---|
| omitted | omitted | omitted | now | one-shot, fires next heartbeat |
| omitted | omitted | given | *(none)* | always-armed, condition-gated only |
| omitted | 0/negative | either | now | permanent, fires next heartbeat then every tick |
| omitted | positive | either | now | repeating, anchored at now |
| given | omitted | either | `time:` | one-shot, fires once |
| given | 0/negative | either | `time:` | permanent, first check at `time:` |
| given | positive | either | `time:` | repeating, anchored at `time:` |

("either" means `condition:` may or may not also be given; it just adds
the condition gate on top of the due-time behavior in that row.)

### `min_interval:` — retrigger cooldown

An optional `min_interval:` adds a retrigger cooldown on top of the above:
once fired, a task won't fire again until at least that long has passed,
regardless of how often its condition holds or its due-time schedule comes
up in between -- primarily useful for debouncing a `changed`/`expr`
condition, or a permanent/fast-repeating task, that would otherwise
re-fire every tick.

When a task becomes due, its gates are checked in a fixed order, each one
short-circuiting the rest: `time:`/`repeat:` due-ness first, then
`condition:`, then `min_interval:`. In particular, a false `condition:` or
a cooldown still in effect stops the check right there — the task's
actions never run, and `min_interval:`'s own retrigger timer isn't touched
by a fire that didn't happen.

### Tasks and the heartbeat: a one-tick lag

Every task in a running system is checked once per heartbeat `tick`, all
against the *same* snapshot of device state -- specifically, whatever the
previous tick committed. A tick first fetches every due device, then runs
tasks, then commits the newly fetched values and computes this tick's
change events. Because tasks run *before* that commit, a `condition:
{ changed: true }` (or an `expr:` using `changed()`/`event()`) reacts to a
device's change exactly one tick after the value was actually fetched --
never on the same tick the change was observed. This is deliberate and
keeps every task's view of the world consistent within a tick, rather than
having task order affect which tasks see a change first.

### Which clock each schedule runs on

PHC measures **intervals** and **absolute times** on two different clocks,
so that a clock correction can't disturb the polling loop:

| Setting | Clock | Why |
| --- | --- | --- |
| `heartbeat:` | monotonic | A tick period is an interval, not a time of day. |
| a device's `update:` | monotonic | Ditto — "every 10 minutes" means elapsed minutes. |
| an endpoint's `history.interval` | monotonic | Ditto. |
| a task's `min_interval:` | monotonic | A cooldown is elapsed time since the last firing. |
| a task's `time:` | wall clock | It names an actual time of day (`"22:00"`, an ISO date). |
| a task's `repeat:` | wall clock | It advances an absolute `time:`, staying anchored to the clock. |

The monotonic clock only ever moves forwards, at a steady rate, regardless
of what happens to the system clock. So when NTP corrects a drifting
Raspberry Pi clock, or a daylight-saving change shifts local time by an
hour, polling simply continues: nothing stalls waiting for the wall clock
to catch back up, and nothing fires a burst of catch-up polls.

Wall-clock scheduling stays exactly that, though: a task set for `22:00`
fires at 22:00 local time, and a DST change moves it with the clock — which
is the whole point of writing a time of day rather than an interval.

The heartbeat itself is a **fixed grid**: each tick is scheduled one
heartbeat after the previous tick's *start*, not after it finishes, so a
tick that takes 200ms on a 1s heartbeat is still followed by the next tick
800ms later — the period stays 1s rather than becoming 1.2s. If a tick
overruns its heartbeat entirely, the missed grid points are skipped (with
one WARNING per overrun episode) rather than queued up and worked off in a
burst.

### Actions

Each `action:`/entry in `actions:` runs one effect against a device, the
task list, or an extension -- `set`, `toggle`, `log`, `create_task`,
`kill_task`, `script`, or an extension-provided kind (e.g.
[`mail_alert`](mail-alert.md)). See
[scripting.md](scripting.md#actions) for the full list and when to reach
for each, and [Reusable task templates](scripting.md#reusable-task-templates)
for spawning/replacing tasks at runtime via `create_task`.

## Time and duration strings

`update:` intervals, a task's `repeat:`/`min_interval:`, an endpoint's
`history.interval` (see [Value history & fractiles](scripting.md#value-history--fractiles)),
and window bounds (e.g. [`extensions/random_light`](random-light.md)'s
`windows:`) accept a **duration** string: a plain number of seconds, or a
compact string combining fixed-length units `ms`/`s`/`m`/`h`/`D` (day)/`W`
(week), e.g. `"10s"`, `"1h30m"`, `"2D12h"`. Calendar units (`Y`/`M`) aren't
valid in a duration, since they aren't a fixed number of seconds.

A device's `update:` and an endpoint's `history.interval` also accept a
**name** looked up in the system YAML's top-level `intervals:` map, instead
of a literal duration — handy for a shared cadence used by several devices,
retunable in one place:

```yaml
intervals:
  fast: 3s
  radon: 5m

devices:
  - id: desk_lamp
    module: virtual
    update: fast          # -> 3s
    endpoints:
      - { key: temp, type: float, history: { size: 4, interval: radon } }  # -> 5m
```

A device/endpoint entry can still give a literal duration directly instead
of a name — the two are interchangeable at every place that accepts one.

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
