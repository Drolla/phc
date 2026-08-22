"""Endpoint construction: expanding profiles and building Endpoint objects.

Overlays instance overrides, substitutes `{param}` templates, and parses
the interval/history fields that go with them.

The stage between a resolved parameter set (see params.py) and a real
Device (see devices.py). Every `intervals_map` parameter below is the
system YAML's top-level `intervals:` section, used to resolve a named
duration before parsing it.
"""

import copy
import re

from phc.core.config.descriptors import _ENDPOINT_ENTRY_KEYS, _HISTORY_ENTRY_KEYS, ModuleDescriptor
from phc.core.config.params import _UNSET
from phc.core.endpoint import LOG_AGGREGATIONS, ON_INVALID_MODES, VALUE_TYPES, Endpoint
from phc.core.errors import ConfigError
from phc.core.intervals import parse_duration
from phc.core.registry import get_endpoint_class

# Matches a bare {name} template field (no format-spec/conversion syntax --
# these templates only ever need to substitute a plain param value, e.g.
# "{node}.0.1") inside an endpoint spec's string fields. Used by
# _substitute_templates to find which params a template references, before
# calling str.format_map on it.
_TEMPLATE_FIELD_RE = re.compile(r"\{(\w+)\}")


# Endpoint-spec fields that are structural/identity metadata rather than
# display or protocol data -- never templated even though some are strings,
# since substituting `key` (used for merge-by-key lookups before this point)
# or `kind`/`type`/`log_aggregation`/`device_profile`/`endpoint_profile`
# would be meaningless or actively wrong.
_TEMPLATE_EXCLUDED_FIELDS = {"key", "kind", "type", "log_aggregation",
                             "device_profile", "endpoint_profile"}


def _overlay_endpoint_spec(base: dict, over: dict) -> dict:
    """Shallow-overlay `over` onto `base` (a plain dict.update).

    A declared endpoint parameter (e.g. zway's command_group/address) is
    an ordinary top-level field at this stage -- not folded into
    `params:` until _merge_endpoints, once every spec is fully resolved
    -- so tweaking just one of them (e.g. `endpoints: [{key: battery,
    address: "16.0"}]`) only replaces that one key; a sibling like
    command_group, untouched by `over`, survives automatically. No field
    needs special-case merging."""
    return {**base, **over}


def _substitute_templates(value, params: dict, device_id: str, endpoint_key: str, field: str = ""):
    """Recursively substitute `{param}` templates in `value`'s strings.

    `value` is an endpoint spec, or a nested dict/value within one (e.g.
    `params:`/`values:`), substituted from the device's already-resolved
    `params` (e.g. `address: "{node}.0.1"` -> "11.0.1" when
    params["node"] == 11). Raises ConfigError if a template references a
    param that is missing or None -- a forgotten `node:` would otherwise
    format to the literal string "7.None" via plain str.format_map, which
    phc/devices/zway/device.py's setup() accepts as a real (if wrong)
    value_id and then reports None forever with no error -- or if the
    template itself is malformed."""
    if isinstance(value, dict):
        return {k: _substitute_templates(v, params, device_id, endpoint_key, field=k)
                for k, v in value.items()}
    if not isinstance(value, str):
        return value
    missing = [name for name in _TEMPLATE_FIELD_RE.findall(value) if params.get(name) is None]
    if missing:
        raise ConfigError(
            f"device {device_id!r}: endpoint {endpoint_key!r} field {field!r} template "
            f"{value!r} needs param(s) {missing}, which are unset")
    try:
        return value.format_map(params)
    except (KeyError, IndexError, ValueError) as exc:
        raise ConfigError(f"device {device_id!r}: endpoint {endpoint_key!r} field {field!r} "
                          f"template {value!r} is invalid: {exc}") from None


def _substitute_endpoint_spec(spec: dict, params: dict, device_id: str) -> dict:
    """Substitute `{param}` templates throughout one endpoint spec's fields.

    A declared endpoint parameter like address, description, unit,
    values:, etc., from the device's resolved `params` -- see
    _substitute_templates. Runs on every endpoint of every device
    regardless of whether it came from a profile, a hand-written instance
    override, or a module's own unconditional `endpoints:` -- a spec with
    no `{...}` anywhere passes through unchanged, so this is a no-op for
    every module that declares no templates. Skips fields in
    _TEMPLATE_EXCLUDED_FIELDS (see comment above)."""
    endpoint_key = spec.get("key", "")
    return {
        field: value if field in _TEMPLATE_EXCLUDED_FIELDS
        else _substitute_templates(value, params, device_id, endpoint_key, field=field)
        for field, value in spec.items()
    }


def _expand_endpoint_specs(module: ModuleDescriptor, entry: dict, device_id: str) -> list:
    """Expand a device entry's `endpoints:` against its module's profiles.

    Also expands the entry's own top-level `device_profile:`, if any
    (see ModuleDescriptor's endpoint_profiles/device_profiles library).
    Returns a plain list of endpoint dicts, ready for _merge_endpoints,
    which needs no awareness that profiles exist. `{param}` templating
    is a later, separate step (_substitute_endpoint_spec, called from
    _merge_endpoints).

    A device with no `device_profile:`/`endpoint_profile:` anywhere gets
    its `endpoints:` list back unchanged. Otherwise: the device's own
    `device_profile:` (if set) expands into a base list first, in the
    profile's order; `endpoints:` then overlays that by `key` (so e.g. an
    `address:` tweak doesn't clobber a profile-derived command_group) and
    appends any key the profile didn't provide. An `endpoints:` entry may
    set its own `endpoint_profile:` too, for a single profile-derived
    endpoint on an otherwise hand-written device."""
    instance_endpoints = entry.get("endpoints") or []
    device_profile_name = entry.get("device_profile")

    if device_profile_name is None and not any(
            "endpoint_profile" in spec for spec in instance_endpoints):
        return instance_endpoints   # no device_profile:/endpoint_profile: -- identity

    def _expand_one(spec: dict) -> dict:
        profile_name = spec.get("endpoint_profile")
        if profile_name is None:
            return spec
        if profile_name not in module.endpoint_profiles:
            raise ConfigError(
                f"device {device_id!r}: endpoint {spec.get('key')!r} references unknown "
                f"profile {profile_name!r} (module {module.name!r} has "
                f"endpoint_profiles {sorted(module.endpoint_profiles)})")
        profile_spec = copy.deepcopy(module.endpoint_profiles[profile_name])
        overlay = {k: v for k, v in spec.items() if k != "endpoint_profile"}
        return _overlay_endpoint_spec(profile_spec, overlay)

    base_specs = []
    if device_profile_name is not None:
        if device_profile_name not in module.device_profiles:
            raise ConfigError(
                f"device {device_id!r}: profile {device_profile_name!r} is not declared by "
                f"module {module.name!r} (has device_profiles {sorted(module.device_profiles)})")
        profile_endpoints = module.device_profiles[device_profile_name]["endpoints"]
        for profile_entry in copy.deepcopy(profile_endpoints):
            base_specs.append(_expand_one(profile_entry))

    by_key = {spec["key"]: spec for spec in base_specs}
    order = list(by_key)
    for spec in instance_endpoints:
        key = spec["key"]
        expanded = _expand_one(spec)
        if key in by_key:
            by_key[key] = _overlay_endpoint_spec(by_key[key], expanded)
        else:
            by_key[key] = expanded
            order.append(key)

    return [by_key[key] for key in order]


def _merge_endpoints(module: ModuleDescriptor, instance_endpoints: list, device_id: str,
                      params: dict, intervals_map: dict | None = None
                      ) -> tuple[list[Endpoint], list[tuple[Endpoint, object]]]:
    """Build this device instance's Endpoint objects.

    Starts from the module's declared endpoints, overlays any
    instance-level overrides (by `key`), and appends instance-only
    endpoints the module didn't declare. Returns (endpoints, seeds),
    where `seeds` are (Endpoint, default_value) pairs to apply once the
    device is constructed.

    `instance_endpoints` is normally the device's `endpoints:` list,
    already expanded against any profile by _expand_endpoint_specs --
    this function has no awareness that profiles exist. `params`
    substitutes `{param}` templates in the merged specs
    (_substitute_endpoint_spec). `intervals_map` only resolves a named
    `history.interval`."""
    allowed_keys = _ENDPOINT_ENTRY_KEYS | module.endpoint_param_names
    for spec in instance_endpoints or []:
        unknown = set(spec) - allowed_keys
        if unknown:
            raise ConfigError(f"device {device_id!r}: endpoint {spec.get('key')!r} has "
                              f"unrecognized key(s) {sorted(unknown)}")
    instance_by_key = {e["key"]: e for e in (instance_endpoints or [])}
    module_keys = [e["key"] for e in module.endpoints]

    merged_specs = []
    for spec in module.endpoints:
        key = spec["key"]
        merged = _overlay_endpoint_spec(spec, instance_by_key[key]) if key in instance_by_key \
            else dict(spec)
        merged_specs.append(merged)

    for key, spec in instance_by_key.items():
        if key not in module_keys:
            merged_specs.append(dict(spec))

    endpoints = []
    seeds = []
    for spec in merged_specs:
        spec = _substitute_endpoint_spec(spec, params, device_id)
        value_type = spec.get("type")
        if value_type is not None and value_type not in VALUE_TYPES:
            raise ConfigError(
                f"device {device_id!r}: endpoint {spec['key']!r} has invalid type "
                f"{value_type!r}, expected one of {VALUE_TYPES}")
        log_aggregation = spec.get("log_aggregation", "max")
        if log_aggregation not in LOG_AGGREGATIONS:
            raise ConfigError(
                f"device {device_id!r}: endpoint {spec['key']!r} has invalid log_aggregation "
                f"{log_aggregation!r}, expected one of {LOG_AGGREGATIONS}")
        on_invalid = spec.get("on_invalid", "pass")
        if on_invalid not in ON_INVALID_MODES:
            raise ConfigError(
                f"device {device_id!r}: endpoint {spec['key']!r} has invalid on_invalid "
                f"{on_invalid!r}, expected one of {ON_INVALID_MODES}")
        history_size, history_interval = 0, None
        if "history" in spec:
            if value_type == "str":
                raise ConfigError(
                    f"device {device_id!r}: endpoint {spec['key']!r} declares history: but "
                    f"has type 'str' -- history only aggregates numeric values")
            history_size, history_interval = _parse_history_spec(
                spec["history"], intervals_map or {}, device_id, spec["key"])
        cls = get_endpoint_class(spec.get("kind"))
        # Declared endpoint parameters become Endpoint.params only here,
        # once the spec is fully resolved (see _overlay_endpoint_spec).
        endpoint_params = {name: spec[name] for name in module.endpoint_param_names
                           if name in spec}
        try:
            ep = cls(
                spec["key"],
                readable=spec.get("readable", True),
                writable=spec.get("writable", False),
                params=endpoint_params or None,
                description=spec.get("description", ""),
                name=spec.get("name", ""),
                value_type=value_type,
                unit=spec.get("unit"),
                values=spec.get("values"),
                log_aggregation=log_aggregation,
                min=spec.get("min"),
                max=spec.get("max"),
                format=spec.get("format"),
                read_transform=spec.get("read_transform"),
                write_transform=spec.get("write_transform"),
                history=history_size,
                history_interval=history_interval,
                on_invalid=on_invalid,
            )
        except ValueError as exc:
            raise ConfigError(f"device {device_id!r}: {exc}") from None
        endpoints.append(ep)
        if "default" in spec:
            seeds.append((ep, spec["default"]))

    return endpoints, seeds


def _parse_interval_value(value, intervals_map: dict) -> float:
    """Resolve one duration value via phc.core.intervals.parse_duration.

    Looks `value` up in `intervals_map` first if it names a known
    interval. Shared by _resolve_interval and _parse_history_spec, so
    the two named-interval lookups can't drift."""
    if isinstance(value, str) and value in intervals_map:
        value = intervals_map[value]
    return parse_duration(value)


def _resolve_interval(module: ModuleDescriptor, instance_entry: dict, intervals_map: dict,
                       module_update=_UNSET) -> float | None:
    """Resolve a device's update interval.

    Priority: the device's own `update:` -> `module_update`
    (_ModuleConfig.update, from modules.<name>.update) -> the module's
    own `update:` default. Returns None if unset (the device is then
    never auto-polled) -- an explicit `update: null` at any of the three
    levels means exactly that, distinct from the key being absent."""
    if "update" in instance_entry:
        value = instance_entry["update"]
    elif module_update is not _UNSET:
        value = module_update
    else:
        value = module.update
    if value is None:
        return None
    return _parse_interval_value(value, intervals_map)


def _parse_history_spec(raw, intervals_map: dict, device_id: str, endpoint_key: str
                         ) -> tuple[int, float | None]:
    """Parse an endpoint spec's `history:` field into (size, interval).

    `interval` is a duration in seconds or None (meaning "default to the
    owning device's resolved update interval", see
    _collect_history_records). Accepts either a bare positive int
    (shorthand for {size: N}) or a {size, interval} mapping. No upper
    bound on size -- see docs/scripting.md for the memory/CPU cost this
    implies for a very large history."""
    if isinstance(raw, dict):
        unknown = set(raw) - _HISTORY_ENTRY_KEYS
        if unknown:
            raise ConfigError(
                f"device {device_id!r}: endpoint {endpoint_key!r} history has "
                f"unrecognized key(s) {sorted(unknown)}")
        if "size" not in raw:
            raise ConfigError(
                f"device {device_id!r}: endpoint {endpoint_key!r} history requires 'size'")
        size = raw["size"]
        interval_raw = raw.get("interval")
    else:
        size = raw
        interval_raw = None

    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        raise ConfigError(
            f"device {device_id!r}: endpoint {endpoint_key!r} history size must be "
            f"a positive int, got {size!r}")

    interval = None
    if interval_raw is not None:
        try:
            interval = _parse_interval_value(interval_raw, intervals_map)
        except ValueError as exc:
            raise ConfigError(
                f"device {device_id!r}: endpoint {endpoint_key!r} history interval "
                f"is invalid: {exc}") from None

    return size, interval
