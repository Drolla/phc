"""Pure inference of which UI widget kind best represents an Endpoint.

From its existing generic metadata
(readable/writable/value_type/values/unit/min/max) -- no per-device or
per-endpoint-kind registration. This is the ONLY place that decision
is made; phc/extensions/web_ui/templates/_macros.html's render_widget
macro branches on the result but never re-derives it."""

from phc.core.device import Device
from phc.core.endpoint import Endpoint

WIDGET_KINDS = ("toggle", "dropdown", "slider", "number", "text", "label")


def infer_widget_kind(endpoint: Endpoint) -> str:
    """Pick a widget kind for `endpoint` from its own metadata alone."""
    if not endpoint.writable:
        return "label"
    if endpoint.value_type == "bool":
        return "toggle"
    if endpoint.values:
        return "dropdown"
    if endpoint.value_type in ("int", "float"):
        return "slider" if endpoint.min is not None and endpoint.max is not None else "number"
    return "text"


def describe_endpoint(qualified_id: str, endpoint: Endpoint, device: Device | None = None) -> dict:
    """The one JSON-serializable shape shared by every render path.

    The JSON read API (GET /api/tree) and every Jinja2 render (full
    page and single /widget/{device}/{endpoint} fragment alike) -- a
    single source of truth for "what a widget needs to know".

    `device` supplies the health of the device this endpoint belongs to
    (see phc.core.health). Without it a widget shows the last value the
    device successfully reported, indefinitely and indistinguishably from
    a live one -- which for a UI someone glances at to decide whether the
    house is fine is the worst possible failure mode. Optional only so a
    caller holding a bare Endpoint can still describe it; such a widget
    reports healthy, since it has nothing to say otherwise."""
    healthy = device.health.healthy if device is not None else True
    return {
        "device": qualified_id,
        "endpoint": endpoint.key,
        "label": endpoint.name or endpoint.description or endpoint.key,
        "widget": infer_widget_kind(endpoint),
        "value": endpoint.get(),
        "text": endpoint.to_text(),
        "readable": endpoint.readable,
        "writable": endpoint.writable,
        "value_type": endpoint.value_type,
        "unit": endpoint.unit,
        "min": endpoint.min,
        "max": endpoint.max,
        "values": ({str(k): v for k, v in endpoint.values.items()} if endpoint.values else None),
        "update_time": endpoint.get_update_time(),
        # Whether the owning device's last I/O attempt succeeded, and how
        # long since this endpoint last produced an actual reading (None
        # if it never has) -- see phc.core.endpoint.Endpoint.get_age.
        "healthy": healthy,
        "age": endpoint.get_age(),
    }


def describe_device(device: Device, visible_pairs: set[tuple[str, str]]) -> dict | None:
    """Recursive device-tree description, pruned to only visible branches.

    A device with none of its own endpoints in `visible_pairs` and no
    descendant with any is omitted entirely (returns None, filtered
    out by its parent/caller). Needed once different pages/sections
    have different, narrower selectors -- without pruning, every
    section would repeat large empty group headers for devices outside
    its own selection."""
    children = [described for described in
                (describe_device(child, visible_pairs) for child in device.children.values())
                if described is not None]
    endpoints = [describe_endpoint(device.qualified_id, ep, device) for ep in device.endpoints.values()
                 if (device.qualified_id, ep.key) in visible_pairs]
    if not endpoints and not children:
        return None
    return {
        "id": device.id,
        "qualified_id": device.qualified_id,
        "name": device.name,
        "endpoints": endpoints,
        "children": children,
    }
