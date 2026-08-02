# Debug portal internals

## Why push, not poll

[`extensions/web_ui`](web-ui.md) works by having each widget poll its own
small HTML fragment. That model can't show change events:
`core.endpoint.Endpoint._event` is cleared at the top of every
`update_state()` call (see `Endpoint`'s own docstring), so it only exists
for the one tick that produced it — any polling cadence slower than the
heartbeat would silently miss most of them, and even polling at exactly
the heartbeat rate can't guarantee alignment.

Instead, [`extensions/debug_portal/extension.py`](../../extensions/debug_portal/extension.py)'s
`DebugPortalInstance.on_tick()` is registered as a Scheduler tick hook (see
`core.config.load_system`'s auto-collection of `on_tick`), so it runs once
per tick, after that tick's state is fully committed (`core.scheduler`'s
pass 4). It builds one snapshot and pushes it to every connected browser
over Server-Sent Events. Nothing is buffered: a snapshot is built, sent,
and discarded every tick, by design (see the extension's `extension.yaml`
description for the user-facing rationale).

## `on_bind`: a fourth hook kind

`configure()` runs during `core.config._load_extensions`, which happens
*before* `tasks:` is parsed (`load_system()` builds `tasks` afterward) —
so at `configure()`-time there is no task list to reference yet. Rather
than special-case this one extension, `core.config.load_system()` gained a
generic fourth hook, auto-collected exactly like `on_tick`/`on_start`/
`on_stop`: any extension instance defining `on_bind(system)` has it called
once, synchronously, right after the `System` is fully built and just
before `load_system()` returns. `DebugPortalInstance.on_bind` simply
stores the `System` for later `on_tick()` calls to read `system.tasks`/
`system.heartbeat` from.

No other shipped extension currently uses `on_bind` — it exists because
this is the first one that needs the *task list itself*, not just a
per-tick callback into individual devices.

## `SseHub`: single-slot mailbox, not a queue

[`extensions/debug_portal/server.py`](../../extensions/debug_portal/server.py)'s
`SseHub` gives each connected client an `asyncio.Queue(maxsize=1)`.
`broadcast()` (called synchronously from `on_tick`, on the same event loop
every SSE handler runs on) overwrites a full queue rather than blocking or
growing it — a stalled/slow browser just skips ticks, and `on_tick` itself
can never be made to wait on a socket. There is deliberately no broadcast
history beyond that one pending slot per client; `GET /api/snapshot`
(backed by `SnapshotHolder`, see below) is the only thing that remembers
anything past the very next read.

`SnapshotHolder` exists only because aiohttp's `Application` freezes once
`AppRunner.setup()` runs, and its `__setitem__` calls `_check_frozen()`
unconditionally — so a plain `app[LAST_SNAPSHOT] = snapshot` on every tick
warns (and is documented as becoming an error). The fix is the same shape
as `SseHub` itself: assign the mutable holder into the app once, at
`build_app()`-time, and mutate its `.value` attribute afterward instead of
reassigning the app's own mapping entry.

`handle_events()` waits on `asyncio.wait({queue.get(), shutdown_event.wait()})`
rather than only the queue, so `on_stop()` setting `SHUTDOWN_EVENT` wakes
every open connection immediately — without it, `AppRunner.cleanup()`
would wait out the full `shutdown_timeout` for each still-open SSE stream
before the process could exit.

## Snapshot shape

[`extensions/debug_portal/snapshot.py`](../../extensions/debug_portal/snapshot.py)'s
`build_snapshot()` is deliberately free of any aiohttp/HTTP concern (same
split as `extensions/web_ui/widgets.py`'s `describe_endpoint()`/
`describe_device()`), so it's directly unit-testable.

Endpoint `state`/`last_valid` are sent as `repr()` strings, not
`to_text()`: `to_text()` applies the endpoint's `values:`/unit display
mapping, which is exactly the wrong thing for a debugger to show, and
`repr()` is the only representation that tells a `None` state apart from
the literal string `"None"` or `""` on the wire. `event`, by contrast, is a
plain JSON `null` (not a repr'd `"None"`) when nothing fired that tick, so
`portal.js`'s highlight logic is a single truthy check rather than a
string comparison against a magic value.

Task rows mirror `core.scheduler.Scheduler._log_task_countdown`'s own
due-time classification (condition-driven / exhausted / counting down),
as structured `{mode, due_in, repeat, cooldown}` fields rather than a
single debug log line. `_task_sort_key` orders soonest numeric `due_in`
first, then `mode: cond` tasks (always re-evaluated, not a countdown),
then exhausted one-shot tasks last.

## Frontend: server-rendered skeleton, client-patched cells

`GET /` renders the endpoint table's `<tr>` skeleton once via Jinja2, each
row keyed by `data-key="device/endpoint"`. The task and device-poll tables
are **not** pre-rendered: `create_task`/`kill_task` actions mutate the live
task list at runtime, so
[`extensions/debug_portal/static/portal.js`](../../extensions/debug_portal/static/portal.js)
builds those two tables entirely from the snapshot stream, keeping a
`Map` of key → `<tr>` and re-`appendChild`-ing each tick to keep DOM order
matching the snapshot's own (already-sorted) order — `appendChild` on an
already-attached node moves it, it doesn't clone it, so this reorders
without recreating rows or losing their `.changed` transition state.

A full `innerHTML` swap at heartbeat rate was rejected early: it would
destroy in-progress text selection and ship several times the bytes for
no benefit, since the row set barely changes tick to tick.

### Change highlighting

Every cell that differs from its previous rendered value gets a
`.changed` CSS class (toggled, not added-then-removed, so a cell that
keeps changing every tick stays solid rather than re-triggering a fade
each time — see `portal.js`'s `setCell()`). This one mechanism covers the
endpoint `event` column for free: it reads `null` → some value → `null`,
so it is "changed" on exactly the tick an event exists.

The countdown-style columns (`due_in`, `age`, `cooldown`) can't use plain
inequality — they drift every tick by construction and would sit
permanently red. They use a reset predicate instead, on the theory that a
reset is exactly the tick the underlying thing fired: `due_in`/`cooldown`
highlight when they **increase**, `age` highlights when it **decreases**.
`portal.js`'s `prevNumber()` helper exists to make this safe across a
`null` previous value (e.g. a condition-driven task's `due_in`, or an
endpoint that has never updated) — `Number("")` is `0` in JavaScript, so a
missing/`null` previous value has to be tracked as an actual `null`, not
read back as a false "previously zero".

`haveRenderedOnce` (in `portal.js`) suppresses highlighting on the very
first render and again after any SSE reconnect (`EventSource.onerror`),
since with no prior value every cell would otherwise look "changed"
relative to the skeleton's placeholder text.
