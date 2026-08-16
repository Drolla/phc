# Debug portal

[`phc/extensions/debug_portal/`](../phc/extensions/debug_portal/) is a small
aiohttp.web server for watching a running system's internals live: the
scheduler's task queue, the device poll queue, and every selected
endpoint's state/event/last-valid-state. It exists purely for
debugging/observability — it has no equivalent of [web UI](web-ui.md)'s
widgets, and cannot write to any device.

**Disabled by default.** Nothing loads, and no port opens, unless an
`extensions.debug_portal.<instance>:` entry is present in the system YAML:

```yaml
extensions:
  debug_portal:
    debug:
      host: 127.0.0.1
      port: 8081
      selectors: ["*"]
```

Open `http://127.0.0.1:8081/` (or whatever `host`/`port` you configured)
in a browser. The page has three parts:

- **Task queue** — every configured task, in planned execution order:
  soonest due time first, then condition-gated tasks with no due-time
  countdown (`mode: cond` or `cond+time` — these are re-evaluated every
  tick rather than counting down), then any task with neither a countdown
  nor a condition to re-arm it. `mode` is `cond` (a bare `condition:`),
  `time` (a `time:`/`repeat:` schedule), or `cond+time` (both given —
  e.g. "check at 22:00, but only if X"). Each row also shows its `repeat`
  interval and any remaining `min_interval` cooldown. A one-shot task
  (`repeat:` omitted) removes itself the tick it fires, so it simply
  disappears from the list rather than lingering as `never`.
- **Device poll queue** — every device with an `update:` interval, sorted
  by time until its next scheduled fetch.
- **Endpoints** — one row per selector-matched `(device, endpoint)` pair:
  its current state, its last non-empty ("valid") state, this tick's
  change event (if any), and how long ago it last updated. A row where
  `state` and `last_valid` disagree despite no event is exactly the case
  where a value changed but nothing was actually notified of it — see
  [`phc/core/endpoint.py`](../phc/core/endpoint.py)'s two-phase state model.

The page updates **once per scheduler tick**, pushed from the server over
Server-Sent Events — there is no separate refresh interval to configure,
and no history is kept: each tick's snapshot is shown and then discarded.
Any cell that changed since the previous tick flashes red for exactly that
one tick period, so you can watch state propagate in real time without
staring for a diff. A filter box narrows the endpoint table to a
device/endpoint substring, and an "events only" checkbox hides every
endpoint with no event on the current tick. The pause button freezes the
display (the stream keeps running underneath) so you can read a row that
would otherwise keep changing under you.

`GET /api/snapshot` returns the same data as one JSON object, for
`curl`/scripting rather than watching the page.

## Parameters

- `host` (default `127.0.0.1`) / `port` (default `8081`) — where to bind.
  As with [web UI](web-ui.md), there is no authentication in v1, so only
  bind beyond loopback on a trusted LAN.
- `selectors` (default `["*"]`) — which endpoints appear in the endpoint
  table (same `"<device-glob>/<endpoint-glob>"` syntax as
  [`phc/extensions/logdb`](logdb.md)'s own `selectors`). The task queue and
  device poll queue always show everything, regardless of this setting.
- `shutdown_timeout` (default `5`) — seconds aiohttp waits for an
  in-flight request to finish when the server stops. An open live view
  connection is woken immediately on shutdown, so this mainly bounds an
  in-flight `GET /api/snapshot`.

See [`examples/debug_portal_system.yaml`](../examples/debug_portal_system.yaml)
for a complete runnable example: three devices polled on different
intervals (so the device poll queue actually reorders), and one task of
each kind the portal distinguishes (repeating time-driven, condition-driven,
and one-shot).
