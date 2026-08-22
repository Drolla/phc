"""aiohttp.web application for phc.extensions.debug_portal.

A live, heartbeat-paced view of the scheduler's task queue, the device
poll queue, and every selected endpoint's state/event/last_valid_state,
pushed over Server-Sent Events. build_app() only constructs the
Application/routes (safe with no running loop); extension.py's
DebugPortalInstance's on_start()/on_stop() own the actual
AppRunner/TCPSite lifecycle, driven by phc.core.scheduler.Scheduler's
start_hooks/stop_hooks -- see phc/extensions/web_ui/server.py for the
same split, which this mirrors."""

import asyncio
import importlib.resources
import logging
from pathlib import Path

import jinja2
from aiohttp import web

logger = logging.getLogger("phc.debug_portal")

# Templates and static assets are package DATA (see pyproject.toml's
# [tool.setuptools.package-data]), so they are located through the import
# system rather than from this file's own path -- the latter only happens
# to work in a source checkout, not an installed wheel. files() returns a
# real directory for any normal (unzipped) install, which is what
# Jinja2's FileSystemLoader and aiohttp's add_static both need.
_PACKAGE_DIR = Path(str(importlib.resources.files("phc.extensions.debug_portal")))
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"
_STATIC_DIR = _PACKAGE_DIR / "static"


class SseHub:
    """Fan-out of one pre-serialized JSON snapshot per tick to every client.

    Via a single-slot mailbox per client (asyncio.Queue(maxsize=1)) --
    a slow/stalled client has its pending snapshot overwritten by the
    next tick's, rather than queueing behind it or blocking
    broadcast(). No history is retained anywhere: exactly one pending
    snapshot per client, discarded once read.

    broadcast() is a plain synchronous method (no `await`): it is called
    directly from DebugPortalInstance.on_tick, itself called synchronously
    on the Scheduler's own event loop (see phc.core.scheduler.Scheduler's
    tick_hooks pass) -- the same loop every SSE handler's queue.get() runs
    on, so no thread-safety beyond the single loop is needed."""

    def __init__(self):
        self._queues: set[asyncio.Queue] = set()

    def register(self) -> asyncio.Queue:
        """Called by handle_events() when a client connects.

        Returns this client's own mailbox."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._queues.add(queue)
        return queue

    def unregister(self, queue: asyncio.Queue) -> None:
        """Called by handle_events() when a client disconnects.

        Normally or via shutdown."""
        self._queues.discard(queue)

    def broadcast(self, payload: str) -> None:
        """Hand `payload` to every connected client.

        `payload` is an already-json.dumps()'d snapshot; see class
        docstring for the coalescing behavior."""
        for queue in self._queues:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(payload)


class SnapshotHolder:
    """Mutable box for the most recently broadcast snapshot.

    Stored once under LAST_SNAPSHOT at build_app()-time and mutated via
    `.value` thereafter -- aiohttp's Application freezes on startup and
    warns (soon: raises) on any further `app[key] = ...` assignment, so
    the per-tick update in extension.py's on_tick() has to mutate an
    already-held object rather than replace the app's own mapping entry."""

    def __init__(self):
        self.value: dict | None = None


# Typed keys for per-app state (aiohttp's recommended alternative to plain
# string keys, see https://docs.aiohttp.org/en/stable/web_advanced.html).
PAIRS = web.AppKey("phc_debug_pairs", list)
HUB = web.AppKey("phc_debug_hub", SseHub)
LAST_SNAPSHOT = web.AppKey("phc_debug_last_snapshot", SnapshotHolder)
SHUTDOWN_EVENT = web.AppKey("phc_debug_shutdown_event", asyncio.Event)
JINJA_ENV = web.AppKey("phc_debug_jinja_env", jinja2.Environment)


def build_app(pairs: list[tuple[str, str]]) -> web.Application:
    """Construct the Application and routes for the selector-matched pairs.

    (device, endpoint) `pairs` to display. Safe to call with no running
    event loop; see module docstring for who owns starting/stopping it."""
    app = web.Application()
    app[PAIRS] = pairs
    app[HUB] = SseHub()
    app[LAST_SNAPSHOT] = SnapshotHolder()
    app[SHUTDOWN_EVENT] = asyncio.Event()
    app[JINJA_ENV] = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(["html"]),
    )

    app.router.add_get("/", handle_index)
    app.router.add_get("/events", handle_events)
    app.router.add_get("/api/snapshot", handle_api_snapshot)
    app.router.add_static("/static/", _STATIC_DIR, name="static")
    return app


async def handle_index(request: web.Request) -> web.Response:
    """Render the endpoint table's skeleton once, server-side.

    One row per selector-matched (device, endpoint) pair, keyed by
    `data-key` for portal.js to patch in place every tick. The
    task/device-poll queues are NOT pre-rendered here: unlike endpoints
    (fixed at load time), the task list can be mutated at runtime
    (create_task/kill_task), so portal.js builds those two tables
    entirely from the live snapshot stream."""
    pairs = request.app[PAIRS]
    rows = [{"key": f"{qid}/{key}", "device": qid, "endpoint": key} for qid, key in pairs]
    template = request.app[JINJA_ENV].get_template("portal.html")
    html = template.render(rows=rows)
    return web.Response(text=html, content_type="text/html")


async def handle_api_snapshot(request: web.Request) -> web.Response:
    """One-shot JSON read of the most recently broadcast snapshot.

    For curl/scripting, and for tests that don't want to hold an SSE
    connection open. 503 before the first tick has run."""
    snapshot = request.app[LAST_SNAPSHOT].value
    if snapshot is None:
        raise web.HTTPServiceUnavailable(text="no scheduler tick has run yet")
    return web.json_response(snapshot)


async def handle_events(request: web.Request) -> web.StreamResponse:
    """Server-Sent Events stream: one `data: <json>\\n\\n` frame per tick.

    For as long as the client stays connected. Waits on this client's
    own mailbox (see SseHub) and on SHUTDOWN_EVENT together, so
    on_stop() setting the shutdown event wakes every open connection
    immediately instead of leaving AppRunner.cleanup() to wait out the
    full shutdown_timeout for each one."""
    hub = request.app[HUB]
    shutdown_event = request.app[SHUTDOWN_EVENT]
    queue = hub.register()
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    shutdown_task = asyncio.ensure_future(shutdown_event.wait())
    try:
        while True:
            queue_task = asyncio.ensure_future(queue.get())
            done, _pending = await asyncio.wait(
                {queue_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED)
            if shutdown_task in done:
                queue_task.cancel()
                break
            await resp.write(f"data: {queue_task.result()}\n\n".encode())
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        shutdown_task.cancel()
        hub.unregister(queue)
    return resp
