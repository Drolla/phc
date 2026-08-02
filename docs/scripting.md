# Conditions, scripted actions & sticky values

## Overview

A task's `condition`, a `script` action's `code`, and a `set` action's
`expr` all run in the same small, sandboxed subset of Python — enough to
combine several devices' state, spawn/cancel tagged follow-up tasks, and
read/reset a sticky min/max window, without writing a new device module or
extension:

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

No imports, no method-call chains beyond what's explicitly allowed, no
unbounded loops — this is mistake-containment for a trusted, locally-
authored YAML file, not a sandbox against a hostile author (see
`core/scripting.py`'s own docstring). The rest of this page covers what
[the shared sandbox](#the-shared-sandbox) offers all three surfaces, the
two ways to write a task's [condition](#conditions), the different
`kind:`s an [action](#actions) can take and when to reach for each, and
the [sticky/history](#sticky-values--history) mechanisms available
throughout.

## The shared sandbox

`condition.expr`, a `script` action's `code`, and a `set` action's `expr`
all run against one shared set of functions, so these three surfaces can
never expose different capabilities by accident:

- Always available: `state(ref)`, `changed(ref)`, `text(ref)`, `event(ref)`,
  `sticky(ref)` (see [Sticky values](#sticky-values), below), `history(ref)`,
  `fractile(ref, f)`, `median(ref)`, `average(ref)` (see [Value history &
  fractiles](#value-history--fractiles), below), and `devices(pattern)` (a
  glob, e.g. `"house.*/*"`, usable as a `for` target).
- Only in a `script` action (never in a condition or a `set` action's
  `expr`, both of which must stay side-effect-free): `set_state(ref,
  value)`, `create_task(spec)` (same shape as a top-level `tasks:` entry),
  `kill_task(*tag_globs)` (remove matching tasks — the declarative form is
  the `kill_task` action kind), `reset_sticky(ref)`, and `log(msg)`.
- `ref` is a `"device.endpoint"` string, either inline (`state("a.b")`) or
  bound to a short name via an optional `refs: { name: "a.b" }` map for the
  `name.state`/`name.changed`/... attribute form used above. `refs:` is
  accepted as a sibling key at all three of these surfaces -- `condition`,
  a `script` action's `code`, and a `set` action's `expr` -- not just on
  the condition shown above.
- Attribute access on a bound ref is limited to `.state`/`.changed`/
  `.text`/`.event`/`.sticky`/`.history`. Indexing (`ref[i]`, `event[2]`)
  and dict-key access (`d['key']`) work on any value a function/attribute
  returns, e.g. to pull one element out of a multi-item `event` list;
  slicing (`x[1:3]`) is not supported.

An endpoint's `read_transform`/`write_transform` (see [Endpoint types,
units & text](concepts.md#endpoint-types-units--text)) reuse the same
underlying expression compiler but *not* the same namespace: they only see
`value` (the raw or logical value being corrected) plus the sandbox's safe
builtins — no `state()`/`changed()`/refs, since a transform runs on one
endpoint's own value in isolation, not against the wider device tree.

## Conditions

A task fires either on a schedule (`time`/`repeat`) or when its
`condition` holds — and a `condition` can be written two ways: the
`{device, changed, value}` shorthand, for gating on one endpoint, or
`expr:`, a general boolean expression, for anything involving more than
one device or richer logic.

### The `{device, changed, value}` shorthand

`changed` and `value` are independent filters, ANDed together — either
one left out is trivially satisfied and doesn't constrain the result at
all:

- `changed: true` — holds only on a tick the endpoint has a fresh change
  event (`event(ref)` is not `None`).
- `changed: false` — the negation: holds only on a tick with **no** change
  event. This is a real filter, not "ignore changes" — leave `changed:`
  out entirely for that.
- `value: X` — holds whenever the endpoint's *current* state equals `X`,
  regardless of whether it just changed. `value:` alone is therefore a
  **level** check ("holds every tick state currently matches X"); paired
  with `changed: true`, it becomes an **edge** check ("holds only the one
  tick state transitions to X"), since state and the change event coincide
  exactly on the tick of a change.
- Neither given: no constraint at all — the condition is unconditionally
  `True` every tick.

```yaml
condition: { device: "relay_a.state", changed: true }                 # any change
condition: { device: "surveillance.armed", changed: true, value: 1 }  # armed, just now
condition: { device: "surveillance.armed", value: 1 }                 # armed, any tick
condition: { device: "surveillance.armed", changed: false, value: 1 } # armed, steady (not the arriving tick)
```

The level-check reading (`value:` with `changed` left out, or explicitly
`changed: false`) combined with `min_interval:` is a pattern that used to
need a `time:`-driven task plus a manual `expr:` check inside the action:

```yaml
tasks:
  - tag: nag_while_armed
    condition: { device: "surveillance.armed", value: 1 }
    min_interval: 1h   # at most once an hour, for as long as armed stays 1
    action:
      kind: mail_alert
      instance: "mail_alert.house"
      title: "Still armed"
      message: "System has been armed for a while"
```

### `expr:`

For anything beyond one endpoint's own changed/value, `expr:` is a
restricted-Python boolean expression evaluated against [the shared
sandbox](#the-shared-sandbox):

```yaml
condition:
  refs: { armed: "security.armed", motion: "hallway.motion" }
  expr: "armed.state == 1 and motion.changed and motion.state == 1"
```

### Five equivalent ways to say the same thing

To compare the shorthand against `expr:`'s different styles directly, here
are five conditions that all fire on the exact same tick — the one
`surveillance.armed` transitions to `1`:

```yaml
# 1. The shorthand
condition: { device: "surveillance.armed", changed: true, value: 1 }

# 2. expr, refs-bound attribute style
condition:
  refs: { armed: "surveillance.armed" }
  expr: "armed.changed and armed.state == 1"

# 3. Same, using .event instead of .changed + .state
condition:
  refs: { armed: "surveillance.armed" }
  expr: "armed.event == 1"

# 4. expr, inline function-call style (no refs:)
condition: { expr: "changed('surveillance.armed') and state('surveillance.armed') == 1" }

# 5. Same, using event() instead of changed() + state()
condition: { expr: "event('surveillance.armed') == 1" }
```

Forms 2/3 and 4/5 are equivalent pairs because `event(ref)` *is*
`state(ref)` on the tick of a change (both come from the same
`update_state()` commit — see `core/endpoint.py`) and `None` on every
other tick, so `event(ref) == 1` already implies "changed, to 1" in one
comparison. Reach for the shorthand (form 1) when a single endpoint's
value is all the condition needs — it's the shortest, and doesn't require
naming any of the sandbox's functions at all; reach for `expr:` when the
condition spans more than one device or needs boolean logic the shorthand
can't express.

## Actions

A task's `action`/`actions:` list dispatches on `kind:`. Nine kinds are
registered across the codebase:

| kind | what it does |
|---|---|
| `set` | Set the target endpoint to a literal `value:` or a dynamic `expr:` result. |
| `toggle` | Flip the target endpoint between its two declared `values`, or `"on"`/`"off"`. |
| `log` | Log a `message` template (`{state}`/`{text}` available) against the target. |
| `create_task` | Build and register a new task at runtime from a nested `specs:`. |
| `kill_task` | Remove every task whose tag matches any of `tags` (fnmatch glob). |
| `script` | Run a restricted-Python script against the shared sandbox, writable. |
| `mail_alert` | Send one message through a configured SMTP instance — see [mail alerts](mail-alert.md). |
| `log_db` | Sample a configured `logdb` instance — see [log database](logdb.md). |
| `random_light` | Run or force a `random_light` instance's randomize pass — see [random light control](random-light.md). |

`set` and `script` are the two kinds this sandbox actually powers, and
often overlap — the same effect can usually be written either way.

### The same fixed effect, three ways

Turning the siren off is a fixed target (`0`), achievable with any of the
three general-purpose kinds:

```yaml
actions:
  - { kind: set, device: "siren.state", value: 0 }        # a literal value
  - { kind: set, device: "siren.state", expr: "0" }        # expr producing a constant
  - { kind: script, code: "set_state('siren.state', 0)" }  # a one-line script
```

All three write the same raw value the same way (`Endpoint.from_text()`/
`set_text()`), so for a fixed target the plain literal `value:` is the
simplest choice — reach for `expr:`/`script` once the target stops being a
constant.

### The same dynamic effect, two ways

`value:` can only ever be a literal — deriving a value from *another*
endpoint needs `expr:` or `script`:

```yaml
# expr: a single expression, re-evaluated fresh each time the action fires
actions:
  - { kind: set, device: "relay_b.state", expr: "state('relay_a.state')" }
```

```yaml
# script: the same mirror, spelled out as a statement
actions:
  - kind: script
    code: "set_state('relay_b.state', state('relay_a.state'))"
```

Since the result is written exactly like a literal `value:` would be, a
`values`-mapped endpoint whose labels aren't a plain on/off pair may need
the expr to produce the target's own raw value or label explicitly, e.g. a
ternary: `expr: "'clear' if not motion.state else 'motion'"` (ternaries,
comparisons, `and`/`or`/`not`, and arithmetic are all allowed).

### Beyond a single value: when to reach for `script`

`set`'s `expr:` is a single expression — it can only ever produce the one
value it writes. `script`'s `code:` is a sequence of statements, and is
the only place `set_state`/`create_task`/`kill_task`/`reset_sticky`/`log`
are available at all — so anything with more than one step (logging *and*
writing *and* scheduling a follow-up, say) needs `script`, not `set`. The
[intrusion example](#overview) at the top of this page is exactly that
case: one action logs, sets two endpoints, and schedules a timed follow-up
task, all together.

See
[`examples/virtual_surveillance_system.yaml`](../examples/virtual_surveillance_system.yaml)
for a fuller worked example (arm/disarm, retriggered intrusion detection,
timed follow-ups, mass-cancel on disarm).

## Sticky values & history

### Sticky values

`sticky(ref)` reads a since-last-`reset_sticky()` min/max window on one
endpoint — the same mechanism [`extensions/logdb`](logdb.md) uses to make
sure a brief spike between two samples isn't lost. Every endpoint a
condition/script/`set expr:` references this way is subscribed under the
owning task's tag (`task_tag`, typically the task's own `tag:`) as its
`subscriber_id`, so two different tasks tracking the same endpoint each
get their own independent window — resetting one doesn't affect the
other's:

```yaml
tasks:
  - tag: report_daily_peak
    time: "07:00"
    repeat: 1D
    action:
      kind: script
      code: |
        log(f"yesterday's peak was {sticky('outdoor_temp.value')}")
        reset_sticky('outdoor_temp.value')
```

`sticky(ref)` returns `None` until at least one value has been observed
since the last reset (or since startup) — the same "nothing yet"
convention as `state()`/`history()`/`fractile()`.

### Value history & fractiles

An endpoint can keep a short in-memory buffer of its own past numeric
values, sampled on a cadence, for combining several recent readings into
one smoothed value inside a condition/script/`set expr:` — e.g. damping a
noisy sensor, or reproducing a hysteresis band that shouldn't react to a
single outlier reading. Opt in with `history:` on the endpoint:

```yaml
endpoints:
  - key: temperature
    type: float
    history: 4                              # shorthand for {size: 4}
  - key: pressure
    type: float
    history: { size: 8, interval: 5m }       # explicit sampling cadence
```

`size` is the number of past samples kept (oldest dropped once full — a
plain bounded FIFO, not a time window). `interval` (optional) is how often
a new sample is taken; if omitted, it defaults to the *owning device's*
`update:` interval, i.e. one sample per poll. An `interval` shorter than
the heartbeat just means "every tick" — not an error, but rarely useful.
Give `interval:` explicitly when a device has no `update:` interval of its
own (e.g. a `virtual` device, or any `update: null` device) — declaring
`history:` there without an explicit `interval:` is a `ConfigError`, since
there would be nothing driving the sample cadence.

Only a `type: str` endpoint is rejected outright (it can never produce a
numeric sample); an untyped endpoint is allowed, since `type:` itself is
optional. A `None`/not-yet-read value, any non-numeric value, and a float
`NaN` are silently skipped when sampling — one `NaN` in the buffer would
otherwise make every subsequent read return nonsense for as long as it
stays in the window. A `bool`-typed endpoint's history is fully supported
(a 0/1 series is a legitimate median-filter debounce). Unlike
`update_state()`'s change-only `event`, history samples the *current*
value on every interval, whether or not it actually changed since the
last sample — a stalled sensor's unchanged reading is deliberately
re-recorded, the same way THC's original `VHistory_Add` behaved.

Four functions read a declared history, each accepting either a single
`"device.endpoint"` ref or a **list** of refs — a list pools every listed
endpoint's buffer into one combined set before computing the result,
letting several sensors contribute to a single smoothed value:

- `history(ref)` — the raw buffer, oldest sample first.
- `fractile(ref, f)` — the value at relative position `f` (0.0–1.0) once
  the pool is sorted: `f=0` the smallest sample, `f=0.5` the middle one,
  `f=1` the largest. Always one of the actually recorded samples, never an
  interpolated value in between. Raises if `f` is outside `[0, 1]`.
- `median(ref)` — shorthand for `fractile(ref, 0.5)`. For an even-sized
  pool this is the *lower* of the two middle samples, not their average
  (ported from the original Tcl system's rounding rule, see below) — if you
  need the interpolated average of the two middle values instead, compute
  it yourself from `sorted(history(ref))`.
- `average(ref)` — the arithmetic mean of the pool.

All four return `None` if the pool is empty (nothing recorded yet) — the
same convention as `state()` on an unread endpoint. Since a script can't
use `if x is None:` unless comparing against `None` this way, remember
that `x == None` also works but `is`/`is not` reads more naturally and is
supported. Pooling an endpoint that never declared `history:` raises a
`ValueError` naming it — this can only be caught at the point a script
actually runs, not at config load time, since a list argument (or a
`devices(pattern)` selector) can't be checked ahead of time the way a
single string-literal ref can.

A worked example — indoor/outdoor temperature smoothing across two sensors
per side, biased toward "fan turns on sooner" by picking a lower fraction
on the outside pool and a higher one on the inside pool:

```yaml
devices:
  - id: living_room_sensor
    module: zway
    endpoints:
      - { key: temp, history: 4 }
  - id: cellar_sensor
    module: waveplus_bridge
    endpoints:
      - { key: temperature, history: 4 }

tasks:
  - tag: fan_control
    time: +1m
    repeat: 5m
    action:
      kind: script
      code: |
        inside = fractile(['living_room_sensor.temp',
                            'cellar_sensor.temperature'], 0.625)
        outside = fractile(['outdoor_sensor_a.temp',
                             'outdoor_sensor_b.temp'], 0.375)
        diff = inside - outside
        # ... hysteresis / threshold logic using diff ...
```

`fractile()`'s index rule (`int((n - 1) * f + 0.5)`) reproduces the
previous Tcl-based system's `VHistory_Get` exactly, including its
round-half-away-from-zero behavior — not Python's `round()` (banker's
rounding), which agrees with it only for some pool sizes/fractions.

A history buffer is in-memory only: it starts empty on every restart and
fills back up from scratch as new samples arrive (no persistence, and
deliberately not restored by [`extensions/recovery`](recovery.md) — a
days-old reading would actively corrupt the smoothing for a full window
after restart). It is a different mechanism from
[`extensions/logdb`](logdb.md): `logdb` is a long-term, disk-backed,
graphable series driven by its own `log_db` task; `history:` is a short,
volatile ring buffer read directly by a script/condition/`set expr:`, with
no separate task or storage of its own. Nothing stops you from also
publishing a `fractile()` result to a `virtual` device's endpoint via a
`set` action's `expr:` if you want it graphable too — see [Endpoint and
device profiles](profiles.md) and [the dynamic-mirror example
above](#the-same-dynamic-effect-two-ways) for the pattern.
