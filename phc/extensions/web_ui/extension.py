"""web_ui extension wiring: parses an extensions.web_ui.<instance>'s
page/section/panel layout against the live device tree (or, absent
`pages:`, synthesizes a single flat page from the top-level `selectors:`
shorthand), builds an aiohttp.web server (server.py) that renders it via
Jinja2 and serves a per-widget HTML fragment/write endpoint for HTMX. See
phc.core.config._load_extensions for configure()'s call site, and
phc.core.scheduler.Scheduler for how on_start/on_stop get invoked
(start_hooks/stop_hooks -- see phc/core/scheduler.py)."""

import logging

from aiohttp import web

from phc.core.config import ConfigError
from phc.core.device import Device
from phc.core.intervals import parse_duration
from phc.extensions.web_ui.panels import DevicesPanel, Panel, get_panel_kind_class
from phc.extensions.web_ui.server import build_app

logger = logging.getLogger("phc.web_ui")


class Page:
    """One top-level page (its own URL, `/page/{id}`), holding an ordered
    list of Sections."""

    def __init__(self, id: str, title: str, sections: list["Section"]):
        self.id = id
        self.title = title
        self.sections = sections


class Section:
    """One collapsible group within a page (native <details>, folded by
    default unless collapsed=False), holding an ordered list of Panels."""

    def __init__(self, id: str, title: str, collapsed: bool, panels: list[Panel]):
        self.id = id
        self.title = title
        self.collapsed = collapsed
        self.panels = panels


def _build_panel(panel_spec: dict, flat: dict[str, Device], instance_key: str, label: str) -> Panel:
    kind = panel_spec.get("kind", "devices")
    panel_cls = get_panel_kind_class(kind)
    extra = {k: v for k, v in panel_spec.items() if k != "kind"}
    try:
        return panel_cls(flat=flat, **extra)
    except TypeError as exc:
        raise ConfigError(
            f"web_ui instance {instance_key!r}: {label}: invalid {kind!r} panel: {exc}") from None


def _build_section(section_spec: dict, flat: dict[str, Device], instance_key: str, page_id: str) -> Section:
    section_id = section_spec.get("id")
    if not section_id:
        raise ConfigError(f"web_ui instance {instance_key!r}: page {page_id!r}: section missing required 'id'")
    label = f"page {page_id!r} section {section_id!r}"
    title = section_spec.get("title", section_id)
    collapsed = bool(section_spec.get("collapsed", True))

    has_selectors = "selectors" in section_spec
    has_panels = "panels" in section_spec
    if has_selectors == has_panels:
        raise ConfigError(
            f"web_ui instance {instance_key!r}: {label}: specify exactly one of 'selectors' or 'panels'")

    if has_selectors:
        panels = [DevicesPanel(section_spec["selectors"], flat)]
    else:
        panels_spec = section_spec["panels"]
        if not isinstance(panels_spec, list) or not panels_spec:
            raise ConfigError(f"web_ui instance {instance_key!r}: {label}: 'panels' must be a non-empty list")
        panels = [_build_panel(spec, flat, instance_key, label) for spec in panels_spec]

    return Section(id=section_id, title=title, collapsed=collapsed, panels=panels)


def _build_page(page_spec: dict, flat: dict[str, Device], instance_key: str) -> Page:
    page_id = page_spec.get("id")
    if not page_id:
        raise ConfigError(f"web_ui instance {instance_key!r}: page missing required 'id'")
    title = page_spec.get("title", page_id)

    sections_spec = page_spec.get("sections")
    if not isinstance(sections_spec, list) or not sections_spec:
        raise ConfigError(
            f"web_ui instance {instance_key!r}: page {page_id!r}: 'sections' must be a non-empty list")
    sections = [_build_section(spec, flat, instance_key, page_id) for spec in sections_spec]

    section_ids = [s.id for s in sections]
    duplicates = {i for i in section_ids if section_ids.count(i) > 1}
    if duplicates:
        raise ConfigError(
            f"web_ui instance {instance_key!r}: page {page_id!r}: duplicate section id(s): {sorted(duplicates)}")

    return Page(id=page_id, title=title, sections=sections)


def configure(params: dict, flat: dict[str, Device], instance_key: str,
              extensions_registry: dict | None = None) -> "WebUiInstance":
    pages_spec = params.get("pages")
    if pages_spec:
        if not isinstance(pages_spec, list):
            raise ConfigError(f"web_ui instance {instance_key!r}: 'pages' must be a list")
        pages = [_build_page(spec, flat, instance_key) for spec in pages_spec]
        page_ids = [p.id for p in pages]
        duplicates = {i for i in page_ids if page_ids.count(i) > 1}
        if duplicates:
            raise ConfigError(f"web_ui instance {instance_key!r}: duplicate page id(s): {sorted(duplicates)}")

        graph_ids = [panel.id for page in pages for section in page.sections
                     for panel in section.panels if panel.kind == "graph"]
        duplicates = {i for i in graph_ids if graph_ids.count(i) > 1}
        if duplicates:
            raise ConfigError(
                f"web_ui instance {instance_key!r}: duplicate graph panel id(s): {sorted(duplicates)}")

        timer_panel_ids = [panel.id for page in pages for section in page.sections
                            for panel in section.panels if panel.kind == "timers"]
        duplicates = {i for i in timer_panel_ids if timer_panel_ids.count(i) > 1}
        if duplicates:
            raise ConfigError(
                f"web_ui instance {instance_key!r}: duplicate timers panel id(s): {sorted(duplicates)}")
    else:
        # No `pages:` at all: single-page shorthand off the top-level
        # `selectors:` param -- one always-expanded, untitled section
        # showing everything matched (default "*": everything).
        panel = DevicesPanel(params.get("selectors") or ["*"], flat)
        pages = [Page(id="home", title="Home",
                       sections=[Section(id="all", title="", collapsed=False, panels=[panel])])]

    return WebUiInstance(
        devices=flat,
        pages=pages,
        host=params["host"],
        port=int(params["port"]),
        shutdown_timeout=float(params["shutdown_timeout"]),
        refresh_interval=parse_duration(params["refresh_interval"]),
        instance_key=instance_key,
        extensions_registry=extensions_registry if extensions_registry is not None else {},
    )


class WebUiInstance:
    """One configured web_ui instance. build_app()/the Jinja2 Environment
    (safe with no running loop) are constructed in __init__, at
    configure()-time; the actual AppRunner/TCPSite (which DO require a
    running loop) are created in on_start(), invoked by the Scheduler on
    its own loop -- see phc.core.scheduler.Scheduler.start_hooks/stop_hooks."""

    def __init__(self, devices: dict[str, Device], pages: list[Page], host: str, port: int,
                 shutdown_timeout: float, refresh_interval: float, instance_key: str,
                 extensions_registry: dict):
        self._host = host
        self._port = port
        self._shutdown_timeout = shutdown_timeout
        self._instance_key = instance_key
        # `extensions_registry` is the live, still-growing dict from
        # phc.core.config._load_extensions -- held onto (not snapshotted) so a
        # `kind: graph` panel's `logdb_instance` reference (see
        # phc/extensions/web_ui/server.py's handle_graph_data) can resolve
        # against extensions declared *after* this one, since by the time
        # HTTP requests are served, load_system() has long since returned
        # and the registry is complete.
        self._app = build_app(devices, pages, refresh_interval, extensions_registry)
        self._runner: web.AppRunner | None = None

    async def on_start(self, devices: dict[str, Device]) -> None:
        self._runner = web.AppRunner(self._app, shutdown_timeout=self._shutdown_timeout)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("%s listening on http://%s:%d", self._instance_key, self._host, self._port)

    async def on_stop(self, devices: dict[str, Device]) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        logger.info("%s stopped", self._instance_key)
