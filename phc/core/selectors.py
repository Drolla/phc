"""Device/endpoint selector resolution.

Expands a list of "<device-glob>/<endpoint-glob>" patterns into the
concrete (qualified_id, endpoint_key) pairs they match, against a
system's full flat device tree."""

import fnmatch

from phc.core.device import Device
from phc.core.errors import ConfigError


def resolve_selectors(patterns: list[str], flat: dict[str, Device]) -> list[tuple[str, str]]:
    """Expand selector patterns into a sorted, deduplicated list of pairs.

    (qualified_id, endpoint_key) pairs, matched against every readable
    endpoint of every device in `flat` (the full flat tree, not just
    roots). "*" alone is shorthand for "*/*" (device-glob/endpoint-glob);
    any other pattern must contain exactly one "/". fnmatch's "*" is not
    "."-aware, so e.g. "house.*/*" matches every descendant under house
    at any nesting depth, not just direct children -- intentional.

    A pattern starting with "!" (e.g. "!*/battery") excludes what it
    matches from the result, wherever it sits in the list; a list of only
    exclusions matches nothing."""
    include_globs = []
    exclude_globs = []
    for pattern in patterns:
        target = exclude_globs if pattern.startswith("!") else include_globs
        body = pattern[1:] if pattern.startswith("!") else pattern
        if body == "*" and target is include_globs:
            target.append(("*", "*"))
            continue
        if body.count("/") != 1:
            raise ConfigError(
                f"invalid selector pattern {pattern!r}, expected "
                f"'<device-glob>/<endpoint-glob>' or bare '*'")
        device_glob, _, endpoint_glob = body.partition("/")
        target.append((device_glob, endpoint_glob))

    def matches(globs, qualified_id, endpoint_key):
        return any(fnmatch.fnmatchcase(qualified_id, dg) and fnmatch.fnmatchcase(endpoint_key, eg)
                   for dg, eg in globs)

    pairs = set()
    for qualified_id, device in flat.items():
        for endpoint_key, endpoint in device.endpoints.items():
            if not endpoint.readable:
                continue
            if (matches(include_globs, qualified_id, endpoint_key)
                    and not matches(exclude_globs, qualified_id, endpoint_key)):
                pairs.add((qualified_id, endpoint_key))
    return sorted(pairs)
