"""Pure inference of how an Endpoint is best presented in the UI.

From its existing generic metadata
(readable/writable/value_type/values/unit/min/max) -- no per-device or
per-endpoint-kind registration. This is the ONLY place that decision
is made; callers branch on the result but never re-derive it."""

import re

from phc.core.device import Device
from phc.core.endpoint import Endpoint

WIDGET_KINDS = ("toggle", "dropdown", "slider", "number", "text", "label")
SERIES_KINDS = ("binary", "numeric")
# How decimation may collapse a graph series' samples -- see infer_series_aggregation.
BINARY_AGGREGATIONS = ("max", "min", "avg")


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


def infer_series_kind(endpoint: Endpoint) -> str:
    """Pick a graph-series rendering kind for `endpoint` from its metadata alone.

    "binary" means the logged column only ever holds 0.0/1.0, so it is
    drawn as its own stacked on/off lane rather than sharing a numeric
    y-axis with unrelated magnitudes."""
    return "binary" if endpoint.value_type == "bool" else "numeric"


def infer_series_aggregation(endpoint: Endpoint, series_kind: str) -> str:
    """Pick how decimation should collapse this series' samples.

    Numeric is always averaged. Binary defers to the endpoint's own
    `aggregation` ("max" by default), so decimation keeps answering the
    same question ("was it ever on?") that produced the logged samples.

    Takes the resolved `series_kind` rather than re-deriving it, so a
    panel forcing lanes via `binary_lanes` gets matching decimation."""
    return endpoint.aggregation if series_kind == "binary" else "avg"


def _humanize(token: str) -> str:
    """"motion_cellar" -> "Motion Cellar"; an identifier as display text.

    Leaves a word with an existing capital alone, so an acronym (COP,
    DHW) isn't flattened to title case."""
    words = []
    for word in re.split(r"[_\-\s]+", token.strip()):
        if not word:
            continue
        words.append(word if any(c.isupper() for c in word)
                     else word[0].upper() + word[1:])
    return " ".join(words)


def series_title(device: Device, endpoint: Endpoint, qualified_id: str) -> str:
    """The label for one graph series: a name a person would recognize.

    Prefers the endpoint's own `name`/`description`. Otherwise falls back
    to the device name, appending the endpoint key only when the device
    has more than one endpoint."""
    if endpoint.name or endpoint.description:
        return endpoint.name or endpoint.description
    device_label = device.name or device.id or qualified_id
    if len(device.endpoints) <= 1:
        return _humanize(device_label)
    return f"{_humanize(device_label)} {_humanize(endpoint.key)}"


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
    label = endpoint.name or endpoint.description or endpoint.key
    return {
        "device": qualified_id,
        "endpoint": endpoint.key,
        "label": label,
        # Blank unless it adds something beyond `label`
        "description": endpoint.description if endpoint.description != label else "",
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
