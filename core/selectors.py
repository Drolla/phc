"""Device/endpoint selector resolution: expand a list of
"<device-glob>/<endpoint-glob>" patterns into the concrete (qualified_id,
endpoint_key) pairs they match, against a system's full flat device tree.
Shared by any extension/module that needs to pick a subset of devices'
endpoints (currently extensions.logdb; see extensions/logdb/extension.py)."""

import fnmatch

from core.config import ConfigError
from core.device import Device


def resolve_selectors(patterns: list[str], flat: dict[str, Device]) -> list[tuple[str, str]]:
    """Expand selector patterns into a sorted, deduplicated list of
    (qualified_id, endpoint_key) pairs, matched against every readable
    endpoint of every device in `flat` (the full flat tree, not just
    roots). "*" alone is shorthand for "*/*" (device-glob/endpoint-glob);
    any other pattern must contain exactly one "/". fnmatch's "*" is not
    "."-aware, so e.g. "house.*/*" matches every descendant under house at
    any nesting depth, not just direct children -- intentional."""
    device_endpoint_globs = []
    for pattern in patterns:
        if pattern == "*":
            device_endpoint_globs.append(("*", "*"))
            continue
        if pattern.count("/") != 1:
            raise ConfigError(
                f"invalid selector pattern {pattern!r}, expected "
                f"'<device-glob>/<endpoint-glob>' or bare '*'")
        device_glob, _, endpoint_glob = pattern.partition("/")
        device_endpoint_globs.append((device_glob, endpoint_glob))

    pairs = set()
    for qualified_id, device in flat.items():
        for endpoint_key, endpoint in device.endpoints.items():
            if not endpoint.readable:
                continue
            if any(fnmatch.fnmatchcase(qualified_id, dg) and fnmatch.fnmatchcase(endpoint_key, eg)
                   for dg, eg in device_endpoint_globs):
                pairs.add((qualified_id, endpoint_key))
    return sorted(pairs)
