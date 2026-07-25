"""aiohttp.web application for extensions.web_ui: server-rendered (Jinja2)
device pages/sections/panels + a JSON read API + a write endpoint + a
per-widget HTML fragment endpoint used by HTMX for polling refresh.
build_app() only constructs the Application/routes (safe with no running
loop); extension.py's WebUiInstance.on_start()/on_stop() own the actual
AppRunner/TCPSite lifecycle, driven by core.scheduler.Scheduler's
start_hooks/stop_hooks."""

import asyncio
from pathlib import Path

import jinja2
from aiohttp import web

from core.device import Device
from core.selectors import resolve_selectors
from extensions.web_ui.panels import Panel
from extensions.web_ui.widgets import describe_device, describe_endpoint

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


def build_app(devices: dict[str, Device], pages: list, refresh_interval: float,
              extensions_registry: dict) -> web.Application:
    app = web.Application()
    app[DEVICES] = devices
    app[PAGES] = pages
    app[PAGES_BY_ID] = {page.id: page for page in pages}
    # The whole readable tree, independent of any page/section's own
    # (narrower) selectors -- used by GET /api/tree, a generic read API.
    app[ALL_PAIRS] = set(resolve_selectors(["*"], devices))
    app[REFRESH_INTERVAL] = refresh_interval
    app[EXTENSIONS_REGISTRY] = extensions_registry
    app[JINJA_ENV] = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(["html"]),
    )

    app.router.add_get("/", handle_index)
    app.router.add_get("/page/{page_id}", handle_page)
    app.router.add_get("/api/tree", handle_api_tree)
    app.router.add_get("/widget/{device}/{endpoint}", handle_widget)
    app.router.add_post("/api/set", handle_api_set)
    app.router.add_static("/static/", _STATIC_DIR, name="static")
    return app


def _roots(devices: dict[str, Device]) -> list[Device]:
    return [device for qualified_id, device in devices.items() if "." not in qualified_id]


def _render_panel_data(panel: Panel, devices: dict[str, Device]) -> dict:
    """Panel.describe() gives the raw (kind, resolved-selection) shape from
    configure()-time; this turns it into what the "devices" branch of
    templates/_macros.html's render_panel macro actually needs -- a pruned
    device tree scoped to the panel's own matched pairs. A future panel
    kind (e.g. "graph") would add its own branch here."""
    described = panel.describe()
    if described["kind"] == "devices":
        pairs = set(described["pairs"])
        tree = [d for d in (describe_device(root, pairs) for root in _roots(devices)) if d is not None]
        return {"kind": "devices", "devices": tree}
    return described


def _describe_section(section, devices: dict[str, Device]) -> dict:
    return {
        "id": section.id,
        "title": section.title,
        "collapsed": section.collapsed,
        "panels": [_render_panel_data(panel, devices) for panel in section.panels],
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
    sections = [_describe_section(section, devices) for section in page.sections]
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
    template = request.app[JINJA_ENV].get_template("_widget_only.html")
    html = template.render(ep=ep, refresh_interval=request.app[REFRESH_INTERVAL])
    return web.Response(text=html, content_type="text/html")


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

    try:
        await asyncio.to_thread(device.set_text, text, endpoint_key)
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(text=str(exc))
    return web.Response(status=204)
