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
beyond a bound ref's `.state`/`.changed`/`.text`/`.event`/`.sticky`, no
method-call chains, no unbounded loops — against one shared set of
functions, so these three surfaces can never expose different capabilities
by accident:

- Always available: `state(ref)`, `changed(ref)`, `text(ref)`, `event(ref)`,
  `sticky(ref)` (a since-last-`reset_sticky()` min/max window, the same
  mechanism [`extensions/logdb`](logdb.md) uses), and `devices(pattern)` (a
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
