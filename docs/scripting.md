# Conditions, scripted actions & sticky values

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
restricted sandbox — no imports, no attribute access beyond a bound ref's
`.state`/`.changed`/`.text`/`.event`/`.sticky`, no method-call chains, no
unbounded loops — against one shared set of functions, so a condition and a
script can never expose different capabilities by accident:

- Always available: `state(ref)`, `changed(ref)`, `text(ref)`, `event(ref)`,
  `sticky(ref)` (a since-last-`reset_sticky()` min/max window, the same
  mechanism [`extensions/logdb`](logdb.md) uses), and `devices(pattern)` (a
  glob, e.g. `"house.*/*"`, usable as a `for` target).
- Only in a `script` action (never in a condition, which must stay
  side-effect-free): `set_state(ref, value)`, `create_task(spec)` (same
  shape as a top-level `tasks:` entry), `kill_task(*tag_globs)` (remove
  matching tasks — the declarative form is the `kill_task` action kind),
  `reset_sticky(ref)`, and `log(msg)`.
- `ref` is a `"device.endpoint"` string, either inline (`state("a.b")`) or
  bound to a short name via an optional `refs: { name: "a.b" }` map for the
  `name.state`/`name.changed`/... attribute form used above.

See [`examples/surveillance_system.yaml`](../examples/surveillance_system.yaml)
for a fuller worked example (arm/disarm, retriggered intrusion detection,
timed follow-ups, mass-cancel on disarm).
