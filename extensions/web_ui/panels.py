"""Panel kind registry: a section's content is a list of panels, each
dispatched by `kind` (default "devices"). Local to extensions/web_ui --
core/registry.py is not involved, since only this extension's own
configure() (see extension.py) ever dispatches on panel kind; nothing in
core needs to know panels exist.

v1 ships DevicesPanel (the selector-matched device/endpoint subtree,
rendered via extensions.web_ui.widgets) and GraphPanel (a time-series
chart backed by a named extensions.logdb instance -- see
extensions/web_ui/server.py's handle_graph_data and
extensions/logdb/logdb.py's LogDb.get_decimated). A new panel kind is
added the same way: subclass Panel, decorate with
@register_panel_kind("..."), and add the matching branch to
templates/_macros.html's render_panel macro."""

import logging

from core.config import ConfigError
from core.device import Device
from core.intervals import parse_duration
from core.selectors import resolve_selectors

logger = logging.getLogger("phc.web_ui")

_panel_kinds: dict[str, type["Panel"]] = {}


def register_panel_kind(kind: str):
    """Class decorator: registers a Panel subclass under a `kind` name, so
    a section's `panels:` entries can reference it via `kind: <kind>`."""
    def decorator(cls):
        _panel_kinds[kind] = cls
        return cls
    return decorator


def get_panel_kind_class(kind: str) -> type["Panel"]:
    try:
        cls = _panel_kinds[kind]
    except KeyError:
        raise ConfigError(
            f"web_ui panel: unknown kind {kind!r}; available: {sorted(_panel_kinds)}") from None
    logger.debug("web_ui panel kind %r -> %s", kind, cls.__name__)
    return cls


class Panel:
    """Base class for one section's content block."""

    kind: str = "generic"

    def describe(self) -> dict:
        """Data consumed by templates/_macros.html's render_panel macro."""
        raise NotImplementedError


@register_panel_kind("devices")
class DevicesPanel(Panel):
    """The default (and, in v1, only) panel kind: every endpoint matched by
    `selectors` (same "<device-glob>/<endpoint-glob>" syntax as
    extensions.logdb), rendered as widgets inferred from their own
    metadata (see extensions.web_ui.widgets)."""

    kind = "devices"

    def __init__(self, selectors: list[str], flat: dict[str, Device]):
        self.pairs = resolve_selectors(selectors, flat)

    def describe(self) -> dict:
        return {"kind": self.kind, "pairs": self.pairs}


def _parse_decimation(decimation_spec: list[dict]) -> list[tuple[float, int]]:
    """Parse a `decimation:` list of {older_than, factor} mappings (see
    GraphPanel) into the (age_seconds, factor) tuples LogDb.get_decimated()
    expects."""
    tiers = []
    for entry in decimation_spec:
        if not isinstance(entry, dict) or "older_than" not in entry or "factor" not in entry:
            raise ConfigError(
                f"web_ui graph panel: invalid decimation entry {entry!r}; "
                f"expected {{older_than: <duration>, factor: <int>}}")
        factor = entry["factor"]
        if not isinstance(factor, int) or factor < 1:
            raise ConfigError(f"web_ui graph panel: decimation factor must be a positive integer, got {factor!r}")
        tiers.append((parse_duration(entry["older_than"]), factor))
    return tiers


@register_panel_kind("graph")
class GraphPanel(Panel):
    """A time-series chart over one or more endpoints, backed by a named
    extensions.logdb instance (`logdb_instance`, e.g. "logdb.house_log").
    The referenced instance is *not* resolved here: extensions.web_ui's own
    configure() may run before that logdb instance has been configured (see
    core.config._load_extensions), so resolution is deferred to request
    time, in extensions/web_ui/server.py's handle_graph_data -- an
    unresolvable logdb_instance therefore surfaces as a 404 there, not a
    ConfigError at load time."""

    kind = "graph"

    def __init__(self, flat: dict[str, Device], id: str, logdb_instance: str, selectors: list[str],
                 title: str | None = None, unit: str | None = None, window: str = "24h",
                 decimation: list[dict] | None = None):
        if not id:
            raise ConfigError("web_ui graph panel: 'id' must not be empty")
        self.id = id
        self.logdb_instance = logdb_instance
        self.pairs = resolve_selectors(selectors, flat)
        # Matches extensions/logdb/extension.py's own label convention
        # exactly ("<qualified_id>/<endpoint_key>"), since these labels are
        # looked up directly against the referenced LogDb instance's columns.
        self.labels = [f"{qid}/{key}" for qid, key in self.pairs]
        self.series_titles = [
            flat[qid].endpoint(key).name or flat[qid].endpoint(key).description or f"{qid}/{key}"
            for qid, key in self.pairs]
        self.title = title or id
        self.unit = unit
        self.window = parse_duration(window)
        self.decimation = _parse_decimation(decimation or [])

    def describe(self) -> dict:
        return {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "unit": self.unit,
            "window": self.window,
            "series_titles": self.series_titles,
        }
