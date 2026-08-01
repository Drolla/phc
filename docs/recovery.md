# Recovery

[`extensions/recovery/`](../extensions/recovery/) persists a selected set
of writable device endpoint values to a small YAML file, so PHC can
restore them to their last-known-good state after a crash or restart —
instead of every device falling back to its hardcoded `default:` — e.g. a
light left on, or a thermostat setpoint, comes back the way it was.

An `extensions.recovery.<instance>` entry resolves its `selectors` once at
startup: it rewrites the whole file whenever one of the selected endpoints
changes (at most once per tick), restores every persisted value —
independently and best-effort — once before the scheduler's first tick,
and does one final unconditional write on graceful shutdown.

```yaml
extensions:
  recovery:
    critical_state:
      selectors: ["house.desk_lamp/power", "house.thermostat/setpoint"]
      path: "state/recovery.yaml"
```

- `selectors` (required) — a list of `"<device-glob>/<endpoint-glob>"`
  patterns (e.g. `"house.desk_lamp/power"`, `"house.*/setpoint"`), or bare
  `"*"` for everything writable — same glob syntax as
  [`extensions/logdb`](logdb.md)'s `selectors`. Every matched endpoint must
  be writable — a selector that matches a read-only endpoint, or matches
  nothing, fails at startup rather than silently persisting less than
  configured.
- `path` (required) — YAML recovery file path, resolved relative to the
  current working directory.

## How restore interacts with hardware

Restoring a value calls the same `Device.set()` a task action would use —
it's a real write, pushed to the device immediately (there is no scheduler
tick active yet at startup). Whether that write's effect later shows up via
`get()` depends on the device reporting it back on a subsequent read, same
as for any other write — recovery doesn't bypass a device's normal
read/write model, it only automates *when* the write happens.

A device or endpoint that no longer exists (or is no longer writable) since
the file was last written is skipped with a warning, not treated as fatal —
your config may simply have changed. The same is true of a missing or
corrupt recovery file (nothing to recover yet, e.g. on the very first run).
Each persisted entry is restored independently, so one bad entry never
blocks the rest.
