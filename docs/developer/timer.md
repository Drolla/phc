# Timer internals

## Timers are Tasks, not a parallel scheduler

[`phc/extensions/timer/extension.py`](../../phc/extensions/timer/extension.py)'s
`TimerInstance` deliberately does not implement its own due-time loop.
Instead, every `TimerDef` is mirrored into an ordinary `core.task.Task` via
`core.task.register_task`, tagged `"<instance_key>.<id>"`. This buys, for
free, everything `core.task.Task.run()`/`core.scheduler.Scheduler` already
do correctly: one-shot retirement, repeat rearming with catch-up, per-task
failure isolation, and visibility in the debug portal's task list. Editing
a timer just re-registers its Task under the same tag (`register_task`
replaces by tag); deleting one calls `core.task.kill_tasks`.

`_task_spec()` builds the same dict shape a `tasks:` YAML entry would
parse into (`core.config._build_task`): `time:` is always the timer's own
literal Unix timestamp (hits `core.intervals.parse_time`'s digit-string
branch), and `repeat:` (when set) is exactly what drives `parse_time`'s
own "already past → advance by whole multiples" catch-up logic — no
separate rolling-forward code exists in this extension for repeating
timers.

## `on_bind`, not `configure()`, is where timers are restored

`configure()` runs during `core.config._load_extensions`, before `tasks:`
is built (see [`docs/developer/debug-portal.md`](debug-portal.md)'s own
explanation of the same constraint) — so there is no `System.tasks` list
yet to register a Task into. `TimerInstance.on_bind(system)` is where
persisted timers are actually loaded and turned into Tasks, capturing
`system.devices`/`system.tasks`/`system.extensions` for later CRUD calls
too.

A one-shot timer whose trigger time is more than `catch_up` seconds in the
past (`phc/extensions/timer/timer.py`'s `expired_one_shot()`) is dropped here
rather than registered — it never becomes a Task, so it can never fire
late. A repeating timer is never subject to this check: `parse_time`'s own
catch-up (above) already rolls it forward to a future slot.

## `on_tick` reconciles the store against the live task list

Two things can make a `TimerDef`'s persisted state stale without any CRUD
call happening:

- A one-shot timer's Task fires and is retired by
  `core.scheduler.Scheduler` (`task.finished` → removed from
  `Scheduler._tasks`, see its pass 2). The timer's own record must be
  dropped too, or it would look "pending" forever and get treated as
  massively expired (and thus dropped with a confusing log line) on the
  next restart.
- A repeating timer's Task fires and rearms (`Task.run()` advances
  `due_time` by whole `repeat` multiples). The timer's own `time` field
  must be mirrored forward to match, or a restart would restore a stale
  due time from before the last firing.

`TimerInstance.on_tick()` is a Scheduler tick hook (auto-collected exactly
like `extensions.recovery`'s, see `core.config.load_system`) that walks
the live task list once per tick, keyed by tag, and reconciles both cases
— writing the store only when something actually changed.

## Web UI seam: same pattern as `GraphPanel` → `logdb`

[`phc/extensions/web_ui/panels.py`](../../phc/extensions/web_ui/panels.py)'s
`TimersPanel` holds a `timer_instance` name, not a resolved reference — the
timer instance may be configured before or after the `web_ui:` instance,
so resolution happens per-request in
[`phc/extensions/web_ui/server.py`](../../phc/extensions/web_ui/server.py)'s
`_describe_timers_panel`, exactly mirroring `GraphPanel`/`handle_graph_data`
(see [`docs/developer/web-ui.md`](web-ui.md)). Unlike a graph panel's chart
data, though, a timers panel's target/timer lists are small enough to embed
directly in the page render (like a `DevicesPanel`'s widgets) rather than
being fetched separately by client-side JS — `_render_panel_data` was
extended to take `extensions_registry` for exactly this reason.

`server.py`'s timer routes (`GET /timers/{panel_id}`, `POST
/api/timers/{panel_id}[/delete|/enable]`) all funnel through
`_timers_response()`, which re-renders the whole panel fragment
(`templates/_timers_only.html` → `_timers.html`'s `render_timers_panel`
macro) — the single place timers panel markup is generated, matching
`_macros.html`'s own `render_widget` invariant. Unlike `handle_api_set`'s
empty `204` (a device write isn't observable until the next tick), a timer
CRUD write is extension state, already committed by the time the handler
returns, so returning the freshly-rendered fragment is correct immediately.

`static/timers.js` builds the add/edit form's value control (checkbox/
select/slider/number/text) client-side from a per-panel JSON blob of
target metadata, mirroring `phc/extensions/web_ui/widgets.py`'s
`infer_widget_kind` — the browser has no server round-trip to ask which
widget kind a newly-selected target needs. Listeners are delegated at the
document level (not bound per-element) because htmx replaces the whole
panel, form included, on every poll/mutation.

## Runtime task mutation is safe by construction

A web request handler mutating `system.tasks` (via `add_timer`/
`update_timer`/`delete_timer`/`set_enabled`) runs on the same event loop as
the Scheduler, interleaving only at `await` points. `core.scheduler.
Scheduler._tick_async` iterates a *snapshot* of the task list in its pass 2
(`for task in list(self._tasks)`) precisely so runtime mutation is safe —
the same guarantee `core.task.CreateTaskAction`/`KillTaskAction` already
rely on. No additional locking is needed here.

## Validation happens before persisting, not only at fire time

`TimerInstance._build_timer()` validates a "set" action's `value` against
the target endpoint's own `Endpoint.from_text()` at CRUD time — the same
conversion `core.task.SetAction.perform()` would run when the timer
actually fires (via `Device.set_text()`). This is a deliberate duplicate
call: without it, a bad value would only surface as a swallowed exception
inside a future tick's task pass (`Scheduler._tick_async` logs and
continues on a task failure), never as feedback to whoever created the
timer.
