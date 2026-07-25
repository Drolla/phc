"""Panel kind registry: a section's content is a list of panels, each
dispatched by `kind` (default "devices"). Local to extensions/web_ui --
core/registry.py is not involved, since only this extension's own
configure() (see extension.py) ever dispatches on panel kind; nothing in
core needs to know panels exist.

v1 ships only DevicesPanel, rendering the selector-matched device/endpoint
subtree via extensions.web_ui.widgets. A future non-device-tied panel
(e.g. "kind: graph", a time-series chart over a set of endpoints) is added
the same way: subclass Panel, decorate with @register_panel_kind("graph"),
and add the matching branch to templates/_macros.html's render_panel
macro -- not implemented here, this module only documents the extension
point."""

from core.config import ConfigError
from core.device import Device
from core.selectors import resolve_selectors

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
        return _panel_kinds[kind]
    except KeyError:
        raise ConfigError(
            f"web_ui panel: unknown kind {kind!r}; available: {sorted(_panel_kinds)}") from None


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
