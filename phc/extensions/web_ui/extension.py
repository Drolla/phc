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

from phc.core.device import Device
from phc.core.errors import ConfigError
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


def _reject_duplicates(ids: list[str], what: str, instance_key: str, context: str = "") -> None:
    """Raise if `ids` repeats. A page, section or panel id is an address --
    a URL, or the key a fragment/CRUD route is looked up by -- so a
    duplicate does not merely look untidy, it makes one of the two
    unreachable."""
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        where = f"{context}: " if context else ""
        raise ConfigError(
            f"web_ui instance {instance_key!r}: {where}duplicate {what} id(s): {duplicates}")


def _reject_duplicate_panel_ids(pages: list["Page"], instance_key: str) -> None:
    """Check every panel kind that has an id, rather than naming the kinds
    that currently have one.

    A panel id addresses that panel's own route (GET /api/graph/{id}, the
    /timers/{panel_id} set), and the app indexes panels by it -- so a
    duplicate silently shadows one panel with another. This used to be two
    hand-written checks, one per addressable kind, which meant a new kind
    with an id had to remember to add a third. Grouping by kind closes
    that: ids only have to be unique within a kind, since each kind is
    indexed separately."""
    by_kind: dict[str, list[str]] = {}
    for page in pages:
        for section in page.sections:
            for panel in section.panels:
                panel_id = getattr(panel, "id", None)
                if panel_id is not None:
                    by_kind.setdefault(panel.kind, []).append(panel_id)
    for kind, ids in by_kind.items():
        _reject_duplicates(ids, f"{kind} panel", instance_key)


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
        raise ConfigError(
            f"web_ui instance {instance_key!r}: page {page_id!r}: section missing required 'id'")
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

    _reject_duplicates([s.id for s in sections], "section", instance_key,
                        context=f"page {page_id!r}")

    return Page(id=page_id, title=title, sections=sections)


def configure(params: dict, flat: dict[str, Device], instance_key: str,
              extensions_registry: dict | None = None) -> "WebUiInstance":
    pages_spec = params.get("pages")
    if pages_spec:
        if not isinstance(pages_spec, list):
            raise ConfigError(f"web_ui instance {instance_key!r}: 'pages' must be a list")
        pages = [_build_page(spec, flat, instance_key) for spec in pages_spec]
        _reject_duplicates([p.id for p in pages], "page", instance_key)
        _reject_duplicate_panel_ids(pages, instance_key)
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
        self._pages = pages
        self._extensions_registry = extensions_registry
        self._app = build_app(devices, pages, refresh_interval, extensions_registry)
        self._runner: web.AppRunner | None = None

    def on_bind(self, system) -> None:
        """Check every panel's reference to another extension instance now
        that all of them exist -- see phc.core.config.load_system's on_bind
        pass.

        A `kind: graph` panel names an extensions.logdb instance and a
        `kind: timers` panel an extensions.timer instance. Neither can be
        resolved at configure()-time, since the referenced instance may be
        declared later in the file; they are therefore looked up per
        request, where an unresolvable name could only surface as a 404 in
        a browser -- a typo in a config file reported as a runtime
        symptom, and only if someone happened to open that page.

        Here the registry is complete, so the same lookup becomes a
        ConfigError at startup, naming the panel and what is available."""
        for page in self._pages:
            for section in page.sections:
                for panel in section.panels:
                    self._check_panel_reference(panel)

    def _check_panel_reference(self, panel) -> None:
        """Validate one panel's cross-extension reference, if it has one.

        Checks the exact attribute path the request handler will use, not
        merely that the name resolves -- `store` alone would accept an
        extensions.recovery instance, which has a `store` of its own that
        is not a queryable history. So this asks for what a graph actually
        needs (`store.get_decimated`), which distinguishes "no such
        instance" from "real instance, wrong kind of extension"."""
        if panel.kind == "graph":
            reference, path, kind = panel.logdb_instance, ("store", "get_decimated"), "logdb"
        elif panel.kind == "timers":
            reference, path, kind = panel.timer_instance, ("list_timers",), "timer"
        else:
            return

        instance = self._extensions_registry.get(reference)
        if instance is None:
            raise ConfigError(
                f"web_ui instance {self._instance_key!r}: {panel.kind} panel {panel.id!r} "
                f"references unknown extension instance {reference!r}; configured: "
                f"{sorted(self._extensions_registry)}")

        target = instance
        for attribute in path:
            target = getattr(target, attribute, None)
            if target is None:
                raise ConfigError(
                    f"web_ui instance {self._instance_key!r}: {panel.kind} panel "
                    f"{panel.id!r} references {reference!r}, which is not a usable "
                    f"{kind} instance (no {'.'.join(path)})")

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
