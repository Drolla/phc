"""Pure inference of how an Endpoint is best presented in the UI.

From its existing generic metadata
(readable/writable/value_type/values/unit/min/max) -- no per-device or
per-endpoint-kind registration. This is the ONLY place that decision
is made; callers branch on the result but never re-derive it."""

import os
import re
from collections import Counter

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


def _endpoint_label(endpoint: Endpoint) -> str:
    """An endpoint's display label: `name`, else `description`, else its key."""
    return endpoint.name or endpoint.description or _humanize(endpoint.key)


def _device_label(device: Device) -> str:
    """A device's display label; only an id fallback is humanized."""
    return _humanize(device.name) if device.name == device.id else device.name


def graph_series_titles(series: list[tuple[Device, Endpoint]]) -> list[str]:
    """Labels for a graph panel's (device, endpoint) series.

    Resolved against each other, since a series has no device heading.
    Rules: see docs/web-ui.md."""
    shown = Counter(id(device) for device, _ in series)
    titled = []  # (title, whether it already names the device)
    for device, endpoint in series:
        label = _endpoint_label(endpoint)
        siblings = [_endpoint_label(e) for e in device.endpoints.values()]
        if len(siblings) > 1 and siblings.count(label) == 1:
            titled.append((label, False))
        elif len(siblings) == 1 or shown[id(device)] == 1:
            titled.append((_device_label(device), True))
        else:
            titled.append((f"{_device_label(device)} {_humanize(endpoint.key)}", True))

    counts = Counter(title for title, _ in titled)
    titled = [(f"{_device_label(device)} {title}", True) if counts[title] > 1 and not named
              else (title, named) for (title, named), (device, _) in zip(titled, series, strict=True)]
    titles = [title for title, _ in titled]

    units = {endpoint.unit for _, endpoint in series}
    if not any(named for _, named in titled) and len(units) == 1 and None not in units:
        titles = _drop_shared_words(titles)
    return titles


def _drop_shared_words(titles: list[str]) -> list[str]:
    """Strip leading/trailing words common to every (distinct) title.

    Unchanged if that would leave a title empty."""
    words = [title.split() for title in titles]
    lead = len(os.path.commonprefix(words))
    trail = len(os.path.commonprefix([w[lead:][::-1] for w in words]))
    trimmed = [" ".join(w[lead:len(w) - trail]) for w in words]
    return trimmed if all(trimmed) else titles


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
    label = _endpoint_label(endpoint)
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
