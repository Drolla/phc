# Web UI internals

## Panel-kind registry

[`phc/extensions/web_ui/panels.py`](../../phc/extensions/web_ui/panels.py) keeps its
own kind → class registry, local to this extension — `phc/core/registry.py` is
not involved, since only this extension's own `configure()` ever dispatches
on panel kind. `Panel` is the base class (`describe()` returns the data
`templates/_macros.html`'s `render_panel` macro needs); `DevicesPanel` (the
selector-matched device/endpoint subtree), `GraphPanel` (a time-series
chart backed by a named `phc/extensions/logdb` instance), and `TimersPanel` (a
create/edit/delete UI backed by a named `phc/extensions/timer` instance — see
[`docs/developer/timer.md`](timer.md)) are the three v1 kinds. Adding a new
kind: subclass `Panel`, decorate with `@register_panel_kind("...")`, and
add the matching branch to `templates/_macros.html`'s `render_panel` macro.

`GraphPanel`/`TimersPanel` deliberately do *not* resolve their
`logdb_instance`/`timer_instance` reference at construction time —
`extensions.web_ui`'s own `configure()` may run before that other
extension's instance has been configured, depending on the system YAML's
own `extensions:` iteration order — so resolution is deferred to request
time, in `server.py`'s `handle_graph_data`/`_describe_timers_panel` (an
unresolvable instance surfaces as a 404, or an inline error for a page
render, not a `ConfigError` at load time). Unlike a graph panel's chart
data (fetched separately, client-side), a timers panel's target/timer
lists are small enough to be embedded directly in the page render, like a
`DevicesPanel`'s widgets — see `_render_panel_data`'s `extensions_registry`
parameter.

## Widget-rendering architecture

[`phc/extensions/web_ui/widgets.py`](../../phc/extensions/web_ui/widgets.py) is the
**one and only** place that decides which widget kind (`toggle`, `dropdown`,
`slider`, `number`, `text`, `label`) represents an endpoint — purely from
its existing generic metadata (`readable`/`writable`/`value_type`/`values`/
`min`/`max`), no per-device or per-endpoint-kind registration.
`describe_endpoint()` builds the single JSON-serializable shape shared by
the JSON read API (`GET /api/tree`) and every Jinja2 render (full page and
single `/widget/{device}/{endpoint}` fragment alike) — `_macros.html`'s
`render_widget` macro branches on the result but never re-derives it.
`describe_device()` recursively prunes the device tree to only the
branches that lead to at least one visible endpoint, so a page/section
with a narrow selector doesn't render empty group headers for devices
outside its own selection.

## `server.py`

`build_app()` only constructs the `Application`/routes/Jinja2 environment
(safe with no running event loop); the actual `AppRunner`/`TCPSite`
lifecycle is owned by `extension.py`'s `WebUiInstance.on_start()`/
`on_stop()`, invoked by `core.scheduler.Scheduler`'s `start_hooks`/
`stop_hooks` once the Scheduler's own loop is running.

Per-app state is held in typed `web.AppKey`s (aiohttp's recommended
alternative to plain string keys) rather than ad hoc attributes — the
device tree, page/section/panel layout, the full set of readable pairs (for
the generic `/api/tree` read), the live `extensions_registry` (so a
`GraphPanel` can resolve a `logdb_instance` declared *after* this `web_ui:`
instance — by request time, `load_system()` has long since returned and the
registry is complete), and every `GraphPanel` indexed by its own `id` (for
direct `GET /api/graph/{id}` lookup).

`_log_requests` is the one middleware: handlers signal redirects/errors by
*raising* `web.HTTPException` subclasses rather than returning them, so the
middleware wraps the call in `try`/`except` to still log the resulting
status before re-raising for aiohttp's own handling.

`handle_api_set` deliberately returns no body/markup on a successful write:
a write isn't observable via `get()` until the *next* scheduler tick's
`fetch()`/`update_state()`, so rendering "updated" markup immediately would
show stale state or a fabricated optimistic value — the widget's own
polling (`GET /widget/...`) picks up the real committed value on its next
refresh instead. The write itself is off-loaded onto the loop's default
executor (the Scheduler's own bounded thread pool) so a slow/blocking
`transmit()` can't stall the shared event loop.

`handle_graph_data` replaces a `NaN` reading with `None`/`null` before
JSON-encoding a graph's rows — `json.dumps` would otherwise emit a literal
`NaN` token, which isn't valid JSON and throws in the browser's
`fetch().json()`.

The timers routes (`GET /timers/{panel_id}`, `POST
/api/timers/{panel_id}[/delete|/enable]`) all funnel through
`_timers_response()`, which re-renders the whole panel fragment and returns
it directly — unlike `handle_api_set`'s empty `204`, a timer CRUD write is
extension state, already committed by the time the handler returns, so
there's no next-tick staleness to avoid rendering. See
[`docs/developer/timer.md`](timer.md) for the full writeup, including how
`phc/extensions/timer` itself turns a timer into a real `core.task.Task`.

## `extension.py`

`WebUiInstance.__init__` builds `build_app()`'s `Application` (safe without
a running loop) at `configure()`-time; `on_start()`/`on_stop()` own the
actual server lifecycle, since `AppRunner`/`TCPSite` require a running
loop. `extensions_registry` is held by reference, not snapshotted, so a
`GraphPanel`'s `logdb_instance` can resolve against an extension declared
after this `web_ui:` instance in the system YAML.
