# Conditions, scripted actions & sticky values

Beyond the `condition: { device, changed }` shorthand (fire when one
endpoint changes) and a `set` action's plain literal `value:`, a task's
`condition`, a `script` action's `code`, and a `set` action's `expr` can
use a small, sandboxed subset of Python — enough to combine several
devices' state, spawn/cancel tagged follow-up tasks, and read/reset a
sticky min/max window, without writing a new device module or extension:

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

`condition.expr`, a `script` action's `code`, and a `set` action's `expr`
all run in the same restricted sandbox — no imports, no attribute access
beyond a bound ref's `.state`/`.changed`/`.text`/`.event`/`.sticky`/
`.history`, no method-call chains, no unbounded loops — against one shared
set of functions, so these three surfaces can never expose different
capabilities by accident:

- Always available: `state(ref)`, `changed(ref)`, `text(ref)`, `event(ref)`,
  `sticky(ref)` (a since-last-`reset_sticky()` min/max window, the same
  mechanism [`extensions/logdb`](logdb.md) uses), `history(ref)`,
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
- Indexing (`ref[i]`, `event[2]`) and dict-key access (`d['key']`) work on
  any value a function/attribute returns, e.g. to pull one element out of
  a multi-item `event` list; slicing (`x[1:3]`) is not supported.

A `set` action's `expr` is a single expression (not a multi-line script)
evaluated fresh each time the action fires, and its result is written the
same way a literal `value:` would be — the declarative way to derive one
endpoint's value from another's, without a `script` action just to read
one endpoint and write another:

```yaml
tasks:
  - tag: mirror_relay
    condition: { device: "relay_a.state", changed: true }
    actions:
      - { kind: set, device: "relay_b.state", expr: "state('relay_a.state')" }
```

Since the result is written exactly like a literal `value:` would be, a
`values`-mapped endpoint whose labels aren't a plain on/off pair may need
the expr to produce the target's own raw value or label explicitly, e.g.
a ternary: `expr: "'clear' if not motion.state else 'motion'"` (ternaries,
comparisons, `and`/`or`/`not`, and arithmetic are all allowed).

See [`examples/surveillance_system.yaml`](../examples/surveillance_system.yaml)
for a fuller worked example (arm/disarm, retriggered intrusion detection,
timed follow-ups, mass-cancel on disarm).

An endpoint's `read_transform`/`write_transform` (see [Endpoint types,
units & text](concepts.md#endpoint-types-units--text)) reuse the same
underlying expression compiler but *not* the same namespace: they only see
`value` (the raw or logical value being corrected) plus the sandbox's safe
builtins — no `state()`/`changed()`/refs, since a transform runs on one
endpoint's own value in isolation, not against the wider device tree.

## Value history & fractiles

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
device profiles](profiles.md) and the `set` action example earlier on this
page for the pattern.
