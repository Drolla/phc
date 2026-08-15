# Timer

[`extensions/timer/`](../extensions/timer/) lets you create, edit, and
delete timers at runtime — from the timer panel in
[`extensions/web_ui`](web-ui.md) — instead of having to hand-author a
`tasks:` entry and restart PHC. Each timer sets or toggles one target
device endpoint at a chosen time, optionally repeating, and is persisted to
its own YAML file so it survives a restart.

```yaml
extensions:
  timer:
    house:
      path: "state/timers.yaml"
      selectors: ["house.*/*"]
      catch_up: 5m

  web_ui:
    home:
      pages:
        - id: overview
          sections:
            - id: timers
              title: Timers
              panels:
                - kind: timers
                  id: house_timers
                  timer_instance: "timer.house"
```

- `path` (required) — YAML timer-definitions file path, resolved relative
  to the current working directory. Rewritten whenever a timer is added,
  edited, deleted, enabled/disabled, or fires; read once at startup to
  restore every pending timer.
- `selectors` (default `["*"]`) — a list of `"<device-glob>/<endpoint-glob>"`
  patterns, or bare `"*"` for everything writable — same glob syntax as
  [`extensions/logdb`](logdb.md)'s `selectors`. Only endpoints matched here
  can be picked as a timer's target, in both the web UI and the CRUD API.
  Every matched endpoint must be writable.
- `catch_up` (default `5m`) — grace window for a one-shot timer whose
  trigger time has already passed when PHC starts up. Missed by less than
  this: fires on the very first tick. Missed by more: dropped (logged),
  never fired late. A repeating timer is unaffected — its next occurrence
  is always computed fresh at startup (see below).

## How a timer fires

Each timer becomes an ordinary [scheduled task](configuration.md#tasks)
internally (tag `"<instance>.<id>"`), so it fires on the normal heartbeat,
one-shot timers retire themselves exactly like a one-shot `tasks:` entry,
and every timer shows up in the [debug portal](debug-portal.md)'s task list
like any other. There is no separate timer scheduler.

A repeating timer's next occurrence is recomputed at every startup using
the same "already past → advance by whole repeat intervals" logic ordinary
`tasks: repeat:` entries use (see [Time and duration
strings](configuration.md#time-and-duration-strings)) — so it always rolls
forward to the next future slot rather than firing a backlog of missed
occurrences.

## Creating/editing a timer

In the web UI's timer panel, pick a target (any endpoint matched by
`selectors`), an action (**set** a value, or **toggle** between the
target's two values), a trigger time (native date/time picker), and
optionally a repeat interval (e.g. `1h`, `1D`, `1W` — same [duration
string](configuration.md#time-and-duration-strings) syntax used elsewhere
in PHC) and a description. Leaving the repeat interval empty makes it a
one-shot timer, which disappears from the list once it fires.

Each row's own checkbox enables/disables that timer without deleting it —
a disabled timer stays persisted but has no live task, so it never fires
until re-enabled.
