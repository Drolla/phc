"""aiohttp.web application for extensions.web_ui: server-rendered (Jinja2)
device pages/sections/panels + a JSON read API + a write endpoint + a
per-widget HTML fragment endpoint used by HTMX for polling refresh, plus a
timers-panel fragment/CRUD endpoint set (see _describe_timers_panel).
build_app() only constructs the Application/routes (safe with no running
loop); extension.py's WebUiInstance.on_start()/on_stop() own the actual
AppRunner/TCPSite lifecycle, driven by core.scheduler.Scheduler's
start_hooks/stop_hooks."""

import asyncio
import logging
import math
from datetime import datetime
from pathlib import Path

import jinja2
from aiohttp import web

from core.device import Device
from core.selectors import resolve_selectors
from extensions.web_ui.panels import Panel
from extensions.web_ui.widgets import describe_device, describe_endpoint

logger = logging.getLogger("phc.web_ui")

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent / "static"

# Typed keys for per-app state (aiohttp's recommended alternative to plain
# string keys, see https://docs.aiohttp.org/en/stable/web_advanced.html).
DEVICES = web.AppKey("phc_devices", dict)
PAGES = web.AppKey("phc_pages", list)
PAGES_BY_ID = web.AppKey("phc_pages_by_id", dict)
ALL_PAIRS = web.AppKey("phc_all_pairs", set)
REFRESH_INTERVAL = web.AppKey("phc_refresh_interval", float)
JINJA_ENV = web.AppKey("phc_jinja_env", jinja2.Environment)
# The live core.config._load_extensions registry (see extension.py's
# WebUiInstance) -- lets a panel kind (e.g. "graph") resolve a reference to
# another extension's instance lazily, per request, rather than at
# configure()-time. See extensions/web_ui/panels.py's GraphPanel.
EXTENSIONS_REGISTRY = web.AppKey("phc_extensions_registry", dict)
# Every GraphPanel across all pages/sections, keyed by its own `id` --
# addressed directly by GET /api/graph/{graph_id} (a graph panel's data
# isn't embedded in its page's own render; the page just points the browser
# at this route, see handle_graph_data and extensions/web_ui/static/graph.js).
GRAPH_PANELS_BY_ID = web.AppKey("phc_graph_panels_by_id", dict)
# Every TimersPanel across all pages/sections, keyed by its own `id` --
# addressed by GET /timers/{panel_id} (poll refresh) and the
# /api/timers/{panel_id}[/delete|/enable] CRUD routes below. Unlike a graph
# panel, a timers panel's data IS embedded in its page's own render (see
# _describe_timers_panel) -- these routes only serve the re-rendered
# fragment after a poll/mutation, mirroring handle_widget's role for a
# single device endpoint.
TIMER_PANELS_BY_ID = web.AppKey("phc_timer_panels_by_id", dict)


@web.middleware
async def _log_requests(request: web.Request, handler):
    """Handlers here signal redirects/errors by raising web.HTTPException
    subclasses (e.g. handle_index's HTTPFound, handle_page's HTTPNotFound)
    rather than returning them, so those must be caught here to still log
    their status before re-raising for aiohttp's own handling."""
    if request.method in ("POST", "PUT", "PATCH"):
        # request.post() caches its result on `request`, so this doesn't
        # consume anything the handler's own request.post() call still needs.
        body = dict(await request.post())
        logger.debug("%s %s %s", request.method, request.path, body)
    else:
        logger.debug("%s %s", request.method, request.path)
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        logger.debug("%s %s -> %s", request.method, request.path, exc.status)
        raise
    logger.debug("%s %s -> %s", request.method, request.path, response.status)
    return response


def build_app(devices: dict[str, Device], pages: list, refresh_interval: float,
              extensions_registry: dict) -> web.Application:
    app = web.Application(middlewares=[_log_requests])
    app[DEVICES] = devices
    app[PAGES] = pages
    app[PAGES_BY_ID] = {page.id: page for page in pages}
    # The whole readable tree, independent of any page/section's own
    # (narrower) selectors -- used by GET /api/tree, a generic read API.
    app[ALL_PAIRS] = set(resolve_selectors(["*"], devices))
    app[REFRESH_INTERVAL] = refresh_interval
    app[EXTENSIONS_REGISTRY] = extensions_registry
    app[GRAPH_PANELS_BY_ID] = {
        panel.id: panel
        for page in pages for section in page.sections for panel in section.panels
        if panel.kind == "graph"
    }
    app[TIMER_PANELS_BY_ID] = {
        panel.id: panel
        for page in pages for section in page.sections for panel in section.panels
        if panel.kind == "timers"
    }
    app[JINJA_ENV] = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(["html"]),
    )

    app.router.add_get("/", handle_index)
    app.router.add_get("/page/{page_id}", handle_page)
    app.router.add_get("/api/tree", handle_api_tree)
    app.router.add_get("/widget/{device}/{endpoint}", handle_widget)
    app.router.add_get("/api/graph/{graph_id}", handle_graph_data)
    app.router.add_post("/api/set", handle_api_set)
    app.router.add_get("/timers/{panel_id}", handle_timers_fragment)
    app.router.add_post("/api/timers/{panel_id}", handle_api_timers_set)
    app.router.add_post("/api/timers/{panel_id}/delete", handle_api_timers_delete)
    app.router.add_post("/api/timers/{panel_id}/enable", handle_api_timers_enable)
    app.router.add_static("/static/", _STATIC_DIR, name="static")
    return app


def _roots(devices: dict[str, Device]) -> list[Device]:
    return [device for qualified_id, device in devices.items() if "." not in qualified_id]


def _describe_timer(t) -> dict:
    """One TimerDef (extensions.timer.timer.TimerDef), reshaped for the
    "timers" panel template: adds `target` (the "device/endpoint" pair-key
    convention shared with extensions.recovery/extensions.logdb, used as
    the form's target <select> value) and `time_local` (a naive local
    "YYYY-MM-DDTHHMM"-shaped string an <input type="datetime-local"> can be
    pre-filled with, matching core.intervals' own naive-local time
    convention -- see core/intervals.py's parse_time)."""
    return {
        "id": t.id,
        "device": t.device,
        "endpoint": t.endpoint,
        "target": f"{t.device}/{t.endpoint}",
        "action": t.action,
        "value": t.value,
        "time": t.time,
        "time_local": datetime.fromtimestamp(t.time).strftime("%Y-%m-%dT%H:%M"),
        "repeat": t.repeat or "",
        "description": t.description,
        "enabled": t.enabled,
    }


def _describe_timers_panel(panel, devices: dict[str, Device],
                            extensions_registry: dict) -> tuple[dict, object | None]:
    """Build a "timers" panel's template-ready data (targets + current
    timer list), resolving `panel.timer_instance` against the live
    EXTENSIONS_REGISTRY -- deferred to request/render time for the same
    reason as GraphPanel's logdb_instance (see panels.TimersPanel). Returns
    (data, instance): `data["error"]` is set instead of `targets`/`timers`
    if the reference can't be resolved, and `instance` is None in that
    case -- callers driving a CRUD mutation (not just rendering) use this
    to 404 instead of proceeding."""
    instance = extensions_registry.get(panel.timer_instance)
    if instance is None or not hasattr(instance, "list_timers"):
        return ({"kind": "timers", "id": panel.id, "title": panel.title,
                  "error": f"unknown timer instance {panel.timer_instance!r}"}, None)
    targets = [describe_endpoint(qid, devices[qid].endpoint(key)) for qid, key in instance.target_pairs]
    timers = [_describe_timer(t) for t in instance.list_timers()]
    return ({"kind": "timers", "id": panel.id, "title": panel.title,
              "targets": targets, "timers": timers}, instance)


def _render_panel_data(panel: Panel, devices: dict[str, Device], extensions_registry: dict) -> dict:
    """Panel.describe() gives the raw (kind, resolved-selection) shape from
    configure()-time; this turns it into what each panel kind's own branch
    of templates/_macros.html's render_panel macro actually needs. "devices"
    gets a pruned device tree scoped to its own matched pairs. "timers" gets
    its target/timer lists (see _describe_timers_panel) -- unlike a graph
    panel's chart data (fetched separately by the browser, see
    handle_graph_data), a timers panel's data is small enough to embed
    directly, so it needs `extensions_registry` here at page-render time.
    Any other kind's describe() output (e.g. "graph") is already
    template-ready and passed through unchanged."""
    described = panel.describe()
    if described["kind"] == "devices":
        pairs = set(described["pairs"])
        tree = [d for d in (describe_device(root, pairs) for root in _roots(devices)) if d is not None]
        return {"kind": "devices", "devices": tree}
    if described["kind"] == "timers":
        data, _ = _describe_timers_panel(panel, devices, extensions_registry)
        return data
    return described


def _describe_section(section, devices: dict[str, Device], extensions_registry: dict) -> dict:
    return {
        "id": section.id,
        "title": section.title,
        "collapsed": section.collapsed,
        "panels": [_render_panel_data(panel, devices, extensions_registry) for panel in section.panels],
    }


async def handle_index(request: web.Request) -> web.Response:
    pages = request.app[PAGES]
    raise web.HTTPFound(f"/page/{pages[0].id}")


async def handle_page(request: web.Request) -> web.Response:
    page_id = request.match_info["page_id"]
    page = request.app[PAGES_BY_ID].get(page_id)
    if page is None:
        raise web.HTTPNotFound(text=f"unknown page {page_id!r}")
    devices = request.app[DEVICES]
    extensions_registry = request.app[EXTENSIONS_REGISTRY]
    sections = [_describe_section(section, devices, extensions_registry) for section in page.sections]
    template = request.app[JINJA_ENV].get_template("page.html")
    html = template.render(pages=request.app[PAGES], page=page, sections=sections,
                            refresh_interval=request.app[REFRESH_INTERVAL])
    return web.Response(text=html, content_type="text/html")


async def handle_api_tree(request: web.Request) -> web.Response:
    devices = request.app[DEVICES]
    pairs = request.app[ALL_PAIRS]
    tree = [d for d in (describe_device(root, pairs) for root in _roots(devices)) if d is not None]
    return web.json_response({"roots": tree})


async def handle_widget(request: web.Request) -> web.Response:
    devices = request.app[DEVICES]
    device_id = request.match_info["device"]
    endpoint_key = request.match_info["endpoint"]
    device = devices.get(device_id)
    if device is None:
        raise web.HTTPNotFound(text=f"unknown device {device_id!r}")
    try:
        endpoint = device.endpoint(endpoint_key)
    except KeyError:
        raise web.HTTPNotFound(text=f"unknown endpoint {endpoint_key!r}")
    ep = describe_endpoint(device_id, endpoint)
    logger.debug("web_ui widget %s/%s -> value=%r text=%r", device_id, endpoint_key, ep["value"], ep["text"])
    template = request.app[JINJA_ENV].get_template("_widget_only.html")
    html = template.render(ep=ep, refresh_interval=request.app[REFRESH_INTERVAL])
    return web.Response(text=html, content_type="text/html")


async def handle_graph_data(request: web.Request) -> web.Response:
    """History data for one GraphPanel (see extensions/web_ui/panels.py),
    fetched client-side by extensions/web_ui/static/graph.js. The panel's
    `logdb_instance` reference is resolved here, against the live
    EXTENSIONS_REGISTRY, rather than at configure()-time -- see
    GraphPanel's own docstring for why -- so an unresolvable instance
    surfaces as a 404, not a config-time error."""
    graph_id = request.match_info["graph_id"]
    panel = request.app[GRAPH_PANELS_BY_ID].get(graph_id)
    if panel is None:
        raise web.HTTPNotFound(text=f"unknown graph {graph_id!r}")
    instance = request.app[EXTENSIONS_REGISTRY].get(panel.logdb_instance)
    if instance is None or not hasattr(instance, "store"):
        raise web.HTTPNotFound(text=f"unknown logdb instance {panel.logdb_instance!r}")

    rows = instance.store.get_decimated(labels=panel.labels, decimation=panel.decimation)
    # json.dumps would otherwise emit a literal NaN token for a missing
    # value -- not valid JSON, and fetch().json() throws on it in the
    # browser -- so replace with None/null here.
    wire_rows = [
        [row["time"], *(None if (v := row.get(label)) is None or math.isnan(v) else v
                         for label in panel.labels)]
        for row in rows
    ]
    logger.debug("web_ui graph %s -> %d row(s) from %s", graph_id, len(wire_rows), panel.logdb_instance)
    return web.json_response({
        "labels": ["Time", *panel.series_titles],
        "unit": panel.unit,
        "window": panel.window,
        "rows": wire_rows,
    })


async def handle_api_set(request: web.Request) -> web.Response:
    """Form-encoded body (device, endpoint, text) -> device.set_text(text,
    name=endpoint), off-loaded onto the loop's default executor (the
    Scheduler's own bounded thread pool) so a slow/blocking transmit()
    can't stall the shared event loop. Deliberately returns no body/markup:
    Device.set()'s own docstring is explicit that a write isn't observable
    via get() until the NEXT scheduler tick's fetch()/update_state() --
    rendering "updated" markup here would show stale state or a fabricated
    optimistic value. The widget's own polling (GET /widget/...) picks up
    the real committed value on its next refresh instead."""
    devices = request.app[DEVICES]
    data = await request.post()
    device_id, endpoint_key, text = data.get("device"), data.get("endpoint"), data.get("text")
    if device_id is None or endpoint_key is None or text is None:
        raise web.HTTPBadRequest(text="expected form fields: device, endpoint, text")

    device = devices.get(device_id)
    if device is None:
        raise web.HTTPNotFound(text=f"unknown device {device_id!r}")
    try:
        endpoint = device.endpoint(endpoint_key)
    except KeyError:
        raise web.HTTPNotFound(text=f"unknown endpoint {endpoint_key!r}")
    if not endpoint.writable:
        raise web.HTTPForbidden(text=f"endpoint {endpoint_key!r} is read-only")

    logger.debug("web_ui set %s/%s = %r", device_id, endpoint_key, text)
    try:
        await asyncio.to_thread(device.set_text, text, endpoint_key)
    except (ValueError, TypeError) as exc:
        logger.debug("web_ui set %s/%s failed: %s", device_id, endpoint_key, exc)
        raise web.HTTPBadRequest(text=str(exc))
    logger.debug("web_ui set %s/%s succeeded", device_id, endpoint_key)
    return web.Response(status=204)


def _get_timers_panel(request: web.Request):
    panel_id = request.match_info["panel_id"]
    panel = request.app[TIMER_PANELS_BY_ID].get(panel_id)
    if panel is None:
        raise web.HTTPNotFound(text=f"unknown timers panel {panel_id!r}")
    return panel


def _timers_response(request: web.Request, panel) -> web.Response:
    """Re-render one timers panel's fragment (table + form), the response
    every timers route (poll and every CRUD mutation alike) returns --
    unlike /api/set's empty 204, a timer write is extension state, already
    committed and immediately readable, so there is no next-tick staleness
    to avoid rendering (see handle_api_set's own docstring for that
    contrast)."""
    devices = request.app[DEVICES]
    data, _ = _describe_timers_panel(panel, devices, request.app[EXTENSIONS_REGISTRY])
    template = request.app[JINJA_ENV].get_template("_timers_only.html")
    html = template.render(panel=data, refresh_interval=request.app[REFRESH_INTERVAL])
    return web.Response(text=html, content_type="text/html")


async def handle_timers_fragment(request: web.Request) -> web.Response:
    """GET /timers/{panel_id}: the timers panel's own poll-refresh fragment,
    mirroring handle_widget's role for a single device endpoint."""
    return _timers_response(request, _get_timers_panel(request))


def _resolve_timer_instance(request: web.Request, panel):
    """Shared by every timers CRUD handler: 404 if `panel`'s
    timer_instance can't be resolved (see _describe_timers_panel) --
    otherwise return the live TimerInstance to mutate."""
    devices = request.app[DEVICES]
    _, instance = _describe_timers_panel(panel, devices, request.app[EXTENSIONS_REGISTRY])
    if instance is None:
        raise web.HTTPNotFound(text=f"unknown timer instance {panel.timer_instance!r}")
    return instance


def _parse_timer_form(data) -> dict:
    """Extract one POSTed timer form (see templates/_timers.html's
    render_timers_panel) into extensions.timer.extension.TimerInstance's
    add_timer()/update_timer() kwargs. `target` is "device/endpoint" (the
    same pair-key convention as extensions.recovery/extensions.logdb,
    rpartition'd on the LAST "/" since a qualified device id never
    contains one itself -- see core.task.resolve_endpoint_ref's analogous
    last-dot split). A "value" field absent from the POST body (e.g. an
    unchecked checkbox contributes nothing) becomes None, same as
    action == "toggle" needing none."""
    target = data.get("target", "")
    device, _, endpoint = target.rpartition("/")
    action = data.get("action", "set")
    return {
        "time_spec": data.get("time", ""),
        "device": device,
        "endpoint": endpoint,
        "action": action,
        "value": data.get("value") if action == "set" else None,
        "repeat_spec": data.get("repeat") or None,
        "description": data.get("description", ""),
        "enabled": data.get("enabled") == "true",
    }


async def handle_api_timers_set(request: web.Request) -> web.Response:
    """POST /api/timers/{panel_id}: create (no `id` field) or update
    (`id` present) a timer. Form-encoded, same convention as /api/set."""
    panel = _get_timers_panel(request)
    instance = _resolve_timer_instance(request, panel)
    data = await request.post()
    fields = _parse_timer_form(data)
    timer_id = data.get("id")
    logger.debug("web_ui timers %s %s %s", panel.id, "update" if timer_id else "add", fields)
    try:
        if timer_id:
            instance.update_timer(int(timer_id), **fields)
        else:
            instance.add_timer(**fields)
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(text=str(exc))
    return _timers_response(request, panel)


async def handle_api_timers_delete(request: web.Request) -> web.Response:
    """POST /api/timers/{panel_id}/delete: form field `id`."""
    panel = _get_timers_panel(request)
    instance = _resolve_timer_instance(request, panel)
    data = await request.post()
    logger.debug("web_ui timers %s delete id=%s", panel.id, data.get("id"))
    try:
        instance.delete_timer(int(data.get("id")))
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(text=str(exc))
    return _timers_response(request, panel)


async def handle_api_timers_enable(request: web.Request) -> web.Response:
    """POST /api/timers/{panel_id}/enable: form fields `id`, `enabled`
    ("true"/"false") -- the row's own enabled checkbox, independent of the
    add/edit form."""
    panel = _get_timers_panel(request)
    instance = _resolve_timer_instance(request, panel)
    data = await request.post()
    logger.debug("web_ui timers %s enable id=%s enabled=%s", panel.id, data.get("id"), data.get("enabled"))
    try:
        instance.set_enabled(int(data.get("id")), data.get("enabled") == "true")
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(text=str(exc))
    return _timers_response(request, panel)
