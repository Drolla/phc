"""System YAML loading: parses a system config file into a System (devices,
tasks, and scheduler settings), merging each device's params/endpoints
against its module's declared module.yaml along the way."""

import copy
import importlib
import re
from pathlib import Path

import yaml

from core.device import Device
from core.endpoint import LOG_AGGREGATIONS, VALUE_TYPES, Endpoint
from core.intervals import parse_duration, parse_time
from core.logging_setup import configure_logging
from core.registry import (discover_extensions, discover_modules, get_device_class,
                            get_endpoint_class, get_task_kind_class)
from core import scripting
from core.task import Condition, ExprCondition, Task, resolve_endpoint_ref

_DEVICES_DIR = Path(__file__).resolve().parent.parent / "devices"
_EXTENSIONS_DIR = Path(__file__).resolve().parent.parent / "extensions"


class ConfigError(Exception):
    """Raised for any invalid or inconsistent system YAML."""


_include_stack: list[Path] = []


class _IncludeLoader(yaml.SafeLoader):
    """SafeLoader that also understands !include <relative-path>, so a
    system YAML can pull in child YAML files (see _include_constructor)."""


def _include_constructor(loader: yaml.SafeLoader, node: yaml.Node):
    """Replace an !include node with the parsed contents of the referenced
    file, resolved relative to the including file's own directory (so
    child-of-child includes keep resolving correctly regardless of the root
    file's location or the process's cwd). Raises ConfigError on a missing
    file or a circular include chain."""
    include_rel = loader.construct_scalar(node)
    base_dir = Path(loader.name).resolve().parent
    include_path = (base_dir / include_rel).resolve()
    if not include_path.is_file():
        raise ConfigError(f"!include: no such file: {include_path} (from {loader.name})")
    if include_path in _include_stack:
        chain = " -> ".join(str(p) for p in (*_include_stack, include_path))
        raise ConfigError(f"!include: circular include: {chain}")
    _include_stack.append(include_path)
    try:
        with open(include_path, "r") as f:
            return yaml.load(f, Loader=_IncludeLoader)
    finally:
        _include_stack.pop()


_IncludeLoader.add_constructor("!include", _include_constructor)


class ModuleDescriptor:
    """Parsed module.yaml for one device module.

    endpoint_profiles/device_profiles (see _expand_endpoint_specs) are an
    optional reusable-endpoint library a module can ship: an endpoint
    profile is a full endpoint spec (the same shape a user writes by hand)
    with `{param}` templates in its `parameters:` values, and a device
    profile is a named list of {key, profile} entries. A device opts in via
    `profile:` (whole device) or an endpoint's own `profile:` (single
    endpoint) -- writing endpoints out fully explicitly, with no `profile:`
    anywhere, is unaffected either way.

    device_profiles is mutually exclusive with a non-empty `endpoints:` on
    the SAME module: `endpoints:` is unconditional (every device of the
    module gets them, e.g. meteoswiss's six), while a device_profiles entry
    is opt-in -- mixing the two would make one of "module endpoints" or
    "profile endpoints" the base and the other the overlay depending on
    call order, which is not something a reader of the YAML could tell."""

    def __init__(self, name: str, raw: dict):
        self.name = name
        self.description = raw.get("description", "")
        self.parameters = raw.get("parameters") or []
        self.endpoints = raw.get("endpoints") or []
        self.update = raw.get("update", None)
        self.endpoint_profiles = raw.get("endpoint_profiles") or {}
        self.device_profiles = raw.get("device_profiles") or {}
        if self.device_profiles and self.endpoints:
            raise ConfigError(
                f"module {name!r}: device_profiles and a non-empty endpoints: are mutually "
                f"exclusive -- endpoints: is unconditional (every device gets them) while "
                f"device_profiles is opt-in (only a device that sets profile: gets them), so "
                f"combining them leaves no well-defined base/overlay order")


_module_descriptors: dict[str, ModuleDescriptor] = {}


def _load_module_descriptor(module_name: str) -> ModuleDescriptor:
    """Return the cached ModuleDescriptor for `module_name`, loading and
    parsing its module.yaml on first use."""
    if module_name in _module_descriptors:
        return _module_descriptors[module_name]
    module_yaml_path = _DEVICES_DIR / module_name / "module.yaml"
    if not module_yaml_path.exists():
        raise ConfigError(f"module {module_name!r} has no module.yaml at {module_yaml_path}")
    with open(module_yaml_path, "r") as f:
        raw = yaml.safe_load(f) or {}
    descriptor = ModuleDescriptor(module_name, raw)
    _module_descriptors[module_name] = descriptor
    return descriptor


# Sentinel distinguishing "modules.<name>.update was not set" from a stored
# None ("this module's devices never poll by default") -- plain dict.get()
# can't tell those apart, and the distinction matters the same way it
# already does for a device's own `update: null` (see _resolve_interval).
_UNSET = object()


class _ModuleConfig:
    """Resolved top-level `modules.<name>:` entry for one module type:

    - module_params: `scope: module` parameter values (one shared value for
      every device of this module type) -- same as the old
      _resolve_module_params() return value.
    - device_param_defaults: `scope: device` parameter values supplied under
      modules.<name>.params, which become a *default* for every device of
      this module, still overridable per device -- new in this scheme.
    - update: this module's default update interval (falls between a
      device's own `update:` and module.yaml's `update:`), or the _UNSET
      sentinel if modules.<name>.update was not set.

    Computed once per module name per load_system() call (see
    _build_device's resolved_module_params_cache) -- unlike
    _module_descriptors, this cannot be a cross-call global cache since
    modules_config is specific to the one system YAML being loaded."""

    def __init__(self, module_params: dict, device_param_defaults: dict, update=_UNSET):
        self.module_params = module_params
        self.device_param_defaults = device_param_defaults
        self.update = update


def _resolve_module_config(module: ModuleDescriptor, modules_config: dict) -> _ModuleConfig:
    """Resolve one module type's top-level `modules.<name>:` entry (see
    _ModuleConfig). `scope: module` params keep their original semantics
    exactly: settable only here, `override: none` rejects a value set here,
    `override: required` must be supplied here. `scope: device` params set
    here become per-module defaults instead -- `override: required` is
    satisfied by a module-level value, and `override: none` still rejects
    one being set here at all. Raises ConfigError on an unrecognized
    parameter (i.e. not declared by the module at any scope)."""
    module_entry = modules_config.get(module.name) or {}
    module_config_params = dict(module_entry.get("params") or {})
    update = module_entry.get("update", _UNSET)

    declared = {p["name"]: p for p in module.parameters}

    module_params = {}
    device_param_defaults = {}
    for name, spec in declared.items():
        scope = spec.get("scope", "device")
        override = spec.get("override", "allowed")
        if name in module_config_params:
            if override == "none":
                raise ConfigError(
                    f"module {module.name!r}: parameter {name!r} is not overridable "
                    f"(override: none) but modules.{module.name}.params sets it")
            value = module_config_params.pop(name)
            if scope == "module":
                module_params[name] = value
            else:
                device_param_defaults[name] = value
        elif scope == "module":
            if override == "required":
                raise ConfigError(
                    f"module {module.name!r}: parameter {name!r} is required but not supplied "
                    f"in modules.{module.name}.params")
            module_params[name] = spec.get("default")
        # scope == "device" and not set here: no entry in
        # device_param_defaults -- _merge_params falls back to spec["default"].

    if module_config_params:
        unknown = ", ".join(repr(k) for k in module_config_params)
        raise ConfigError(f"module {module.name!r}: unrecognized parameter(s) {unknown}")

    return _ModuleConfig(module_params, device_param_defaults, update)


class ExtensionDescriptor:
    """Parsed extension.yaml for one extension package. Structurally like
    ModuleDescriptor, but every extension instance (e.g. one named logdb
    entry) is merged independently against the same descriptor -- there is
    no module/device scope split since extensions aren't devices."""

    def __init__(self, name: str, raw: dict):
        self.name = name
        self.description = raw.get("description", "")
        self.parameters = raw.get("parameters") or []


_extension_descriptors: dict[str, ExtensionDescriptor] = {}


def _load_extension_descriptor(name: str) -> ExtensionDescriptor:
    """Return the cached ExtensionDescriptor for `name`, loading and parsing
    its extension.yaml on first use."""
    if name in _extension_descriptors:
        return _extension_descriptors[name]
    extension_yaml_path = _EXTENSIONS_DIR / name / "extension.yaml"
    if not extension_yaml_path.exists():
        raise ConfigError(f"extension {name!r} has no extension.yaml at {extension_yaml_path}")
    with open(extension_yaml_path, "r") as f:
        raw = yaml.safe_load(f) or {}
    descriptor = ExtensionDescriptor(name, raw)
    _extension_descriptors[name] = descriptor
    return descriptor


def _merge_extension_params(descriptor: ExtensionDescriptor, instance_config: dict,
                             instance_label: str) -> dict:
    """Merge one extension instance's params against its extension.yaml's
    declared parameters, honoring each parameter's `override` rule and
    `default` -- same validation shape as _merge_params, but there is no
    module/device scope split to handle. Raises ConfigError on an
    unrecognized, missing-but-required, or not-overridable parameter,
    naming `instance_label` (e.g. "logdb.house_log") in the message."""
    instance_config = dict(instance_config or {})
    declared = {p["name"]: p for p in descriptor.parameters}

    merged = {}
    for name, spec in declared.items():
        override = spec.get("override", "allowed")
        if name in instance_config:
            if override == "none":
                raise ConfigError(
                    f"extension instance {instance_label!r}: parameter {name!r} is not "
                    f"overridable (override: none) but instance config sets it")
            merged[name] = instance_config.pop(name)
        elif override == "required":
            raise ConfigError(
                f"extension instance {instance_label!r}: parameter {name!r} is required "
                f"but not supplied")
        else:
            merged[name] = spec.get("default")

    if instance_config:
        unknown = ", ".join(repr(k) for k in instance_config)
        raise ConfigError(f"extension instance {instance_label!r}: unrecognized parameter(s) {unknown}")

    return merged


def _load_extensions(raw: dict, flat: dict[str, Device]) -> dict[str, object]:
    """Build every extensions.<name>.<instance>: entry in the system YAML.
    raw['extensions'] is {extension_name: {instance_name: params, ...}, ...}
    -- an extension may have zero, one, or several named instances (e.g.
    multiple independently-configured/scheduled logdb files). Each
    instance's params are merged against extensions/<name>/extension.yaml,
    then handed to that package's configure(params, flat, instance_key,
    extensions_registry) entry point (instance_key =
    "<extension_name>.<instance_name>", used both as the registry key below
    and as the sticky-log subscriber id -- see
    core.endpoint.Endpoint.subscribe_log). extensions_registry is this same
    `registry` dict, passed by reference and still growing as later
    instances are configured -- an extension that only resolves a
    cross-extension reference lazily (e.g. at HTTP-request time, well after
    load_system() has returned, as extensions.web_ui's graph panels do) sees
    the complete registry regardless of declaration order; one resolved
    eagerly, during another extension's own configure() call, only sees
    instances configured earlier in raw['extensions']'s iteration order. The
    returned object is registered under instance_key so a hand-authored task
    action (kind: log_db, instance: "logdb.house_log") can look it up, and
    -- if it exposes an on_tick(devices) method -- is auto-collected as a
    Scheduler tick hook (see load_system()). Absence of extensions: (or of
    a given extension's key) means nothing of that kind is active --
    mirrors devices:/tasks: only building what's explicitly declared."""
    registry: dict[str, object] = {}
    for ext_name, instances in (raw.get("extensions") or {}).items():
        if not isinstance(instances, dict):
            raise ConfigError(f"extensions.{ext_name}: expected a mapping of instance name -> params")
        descriptor = _load_extension_descriptor(ext_name)
        module = importlib.import_module(f"extensions.{ext_name}.extension")
        configure = getattr(module, "configure", None)
        if configure is None:
            raise ConfigError(f"extension {ext_name!r} has no configure() entry point")
        for instance_name, instance_config in instances.items():
            key = f"{ext_name}.{instance_name}"
            params = _merge_extension_params(descriptor, instance_config or {}, key)
            registry[key] = configure(params, flat, key, registry)
    return registry


def _merge_params(module: ModuleDescriptor, instance_params: dict, device_id: str,
                   resolved_module_params: dict | None = None,
                   module_param_defaults: dict | None = None) -> dict:
    """Merge one device instance's `params` against its module's declared
    parameters. `scope: module` params come from the already-resolved
    `resolved_module_params` (_ModuleConfig.module_params). `scope: device`
    params are resolved device `params.<name>` -> `module_param_defaults`
    (_ModuleConfig.device_param_defaults, a per-module default from
    modules.<name>.params -- never popped, since it is shared/cached across
    every device of this module) -> module.yaml's own `default:`, with
    `override: required` satisfied by either the device or the module-level
    default. Raises ConfigError on an unrecognized, missing-but-required, or
    not-overridable parameter."""
    instance_params = dict(instance_params or {})
    resolved_module_params = resolved_module_params or {}
    module_param_defaults = module_param_defaults or {}
    declared = {p["name"]: p for p in module.parameters}

    merged = {}
    for name, spec in declared.items():
        if spec.get("scope", "device") == "module":
            if name in instance_params:
                raise ConfigError(
                    f"device {device_id!r}: parameter {name!r} is module-scoped "
                    f"(scope: module) and cannot be set in this device's params; "
                    f"set it under modules.{module.name}.params instead")
            merged[name] = resolved_module_params.get(name, spec.get("default"))
            continue

        override = spec.get("override", "allowed")
        if name in instance_params:
            if override == "none":
                raise ConfigError(
                    f"device {device_id!r}: parameter {name!r} is not overridable "
                    f"(override: none) but instance config sets it")
            merged[name] = instance_params.pop(name)
        elif name in module_param_defaults:
            # override: none for a param set under modules.<name>.params was
            # already rejected once, in _resolve_module_config, before any
            # device gets here.
            merged[name] = module_param_defaults[name]
        elif override == "required":
            raise ConfigError(
                f"device {device_id!r}: parameter {name!r} is required but not supplied")
        else:
            merged[name] = spec.get("default")

    if instance_params:
        unknown = ", ".join(repr(k) for k in instance_params)
        raise ConfigError(f"device {device_id!r}: unrecognized parameter(s) {unknown}")

    return merged


# Matches a bare {name} template field (no format-spec/conversion syntax --
# these templates only ever need to substitute a plain param value, e.g.
# "{node}.0.1") inside a profile-derived endpoint's `parameters:` string
# value. Used by _substitute_params to find which params a template
# references, before calling str.format_map on it.
_TEMPLATE_FIELD_RE = re.compile(r"\{(\w+)\}")


def _overlay_endpoint_spec(base: dict, over: dict) -> dict:
    """Shallow-overlay `over` onto `base` (a plain dict.update) except for
    `parameters`, which is deep-merged one level (over's parameters win
    per-key; a base parameter not present in over is kept) rather than
    replaced wholesale. This is what lets e.g. `endpoints: [{key: battery,
    parameters: {value_id: "16.0"}}]` tweak just one parameter of a
    profile- or module-declared endpoint without losing its other
    parameters -- a plain dict.update would silently drop a sibling
    parameter like command_group, and devices/zway's setup() documents
    that an endpoint missing command_group permanently reports None, never
    raises. Every other field (type, values, writable, ...) replaces
    wholesale; only `parameters` is merged."""
    merged = dict(base)
    merged.update(over)
    if "parameters" in base or "parameters" in over:
        merged["parameters"] = {**base.get("parameters", {}), **over.get("parameters", {})}
    return merged


def _substitute_params(parameters: dict, params: dict, device_id: str, endpoint_key: str) -> dict:
    """Substitute `{param}` templates in a profile-derived endpoint's
    `parameters:` string values from the device's already-resolved `params`
    (e.g. `value_id: "{node}.0.1"` -> "11.0.1" when params["node"] == 11).
    Only `str` values are substituted; anything else (int, bool, list, ...)
    passes through untouched. Raises ConfigError if a template references a
    param that is missing or None -- a forgotten `node:` would otherwise
    format to the literal string "7.None" via plain str.format_map, which
    devices/zway/device.py's setup() accepts as a real (if wrong) value_id
    and then reports None forever with no error -- or if the template
    itself is malformed."""
    substituted = {}
    for key, value in parameters.items():
        if not isinstance(value, str):
            substituted[key] = value
            continue
        missing = [name for name in _TEMPLATE_FIELD_RE.findall(value) if params.get(name) is None]
        if missing:
            raise ConfigError(
                f"device {device_id!r}: endpoint {endpoint_key!r} parameter {key!r} template "
                f"{value!r} needs param(s) {missing}, which are unset")
        try:
            substituted[key] = value.format_map(params)
        except (KeyError, IndexError, ValueError) as exc:
            raise ConfigError(f"device {device_id!r}: endpoint {endpoint_key!r} parameter "
                              f"{key!r} template {value!r} is invalid: {exc}") from None
    return substituted


def _expand_endpoint_specs(module: ModuleDescriptor, entry: dict, params: dict,
                            device_id: str) -> list:
    """Expand a device entry's `endpoints:` (and its own top-level
    `profile:`, if any) against the module's endpoint_profiles/
    device_profiles library (see ModuleDescriptor), substituting `{param}`
    templates in profile-derived `parameters:` values from the device's
    resolved `params`. Returns a plain list of endpoint dicts in the same
    shape a user writes by hand, ready for _merge_endpoints -- which needs
    no awareness that profiles exist.

    A device that uses no `profile:` anywhere (neither its own top-level
    `profile:` nor any endpoint's) gets its `endpoints:` list back
    completely unchanged -- this identity case is what guarantees writing
    endpoints fully explicitly keeps working exactly as before.

    Resolution order: the device's own `profile:` (if set) expands into a
    base endpoints list first, in device_profiles order; the device's own
    `endpoints:` then overlays that list by `key` (via
    _overlay_endpoint_spec, so a `parameters:` tweak doesn't clobber a
    profile-derived command_group/value_id) and appends any key the
    profile didn't already provide. An `endpoints:` entry may set its own
    `profile:` too (with or without a device-level `profile:`), for a
    single profile-derived endpoint on a device that otherwise writes
    everything out by hand. Final order: device-profile order, then
    extra/overlaid instance endpoints in `endpoints:` list order."""
    instance_endpoints = entry.get("endpoints") or []
    device_profile_name = entry.get("profile")

    if device_profile_name is None and not any("profile" in spec for spec in instance_endpoints):
        return instance_endpoints   # no profile: anywhere -- identity, the escape hatch

    def _expand_one(spec: dict) -> dict:
        profile_name = spec.get("profile")
        if profile_name is None:
            return spec
        if profile_name not in module.endpoint_profiles:
            raise ConfigError(
                f"device {device_id!r}: endpoint {spec.get('key')!r} references unknown "
                f"profile {profile_name!r} (module {module.name!r} has "
                f"endpoint_profiles {sorted(module.endpoint_profiles)})")
        profile_spec = copy.deepcopy(module.endpoint_profiles[profile_name])
        overlay = {k: v for k, v in spec.items() if k != "profile"}
        expanded = _overlay_endpoint_spec(profile_spec, overlay)
        expanded["parameters"] = _substitute_params(
            expanded.get("parameters") or {}, params, device_id, expanded.get("key", ""))
        return expanded

    base_specs = []
    if device_profile_name is not None:
        if device_profile_name not in module.device_profiles:
            raise ConfigError(
                f"device {device_id!r}: profile {device_profile_name!r} is not declared by "
                f"module {module.name!r} (has device_profiles {sorted(module.device_profiles)})")
        for profile_entry in copy.deepcopy(module.device_profiles[device_profile_name]):
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


def _merge_endpoints(module: ModuleDescriptor, instance_endpoints: list,
                      device_id: str) -> tuple[list[Endpoint], list[tuple[Endpoint, object]]]:
    """Build this device instance's Endpoint objects: start from the module's
    declared endpoints, overlay any instance-level overrides (by `key`) and
    append instance-only endpoints not declared by the module. Returns
    (endpoints, seeds), where `seeds` are (Endpoint, default_value) pairs to
    apply once the device is constructed. `instance_endpoints` is normally
    the device's raw `endpoints:` list, already expanded against any
    profile: by _expand_endpoint_specs -- this function itself has no
    awareness that profiles exist."""
    for spec in instance_endpoints or []:
        unknown = set(spec) - _ENDPOINT_ENTRY_KEYS
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
        cls = get_endpoint_class(spec.get("kind"))
        try:
            ep = cls(
                spec["key"],
                readable=spec.get("readable", True),
                writable=spec.get("writable", False),
                parameters=spec.get("parameters"),
                description=spec.get("description", ""),
                value_type=value_type,
                unit=spec.get("unit"),
                values=spec.get("values"),
                log_aggregation=log_aggregation,
                min=spec.get("min"),
                max=spec.get("max"),
            )
        except ValueError as exc:
            raise ConfigError(f"device {device_id!r}: {exc}") from None
        endpoints.append(ep)
        if "default" in spec:
            seeds.append((ep, spec["default"]))

    return endpoints, seeds


def _resolve_interval(module: ModuleDescriptor, instance_entry: dict, intervals_map: dict,
                       module_update=_UNSET) -> float | None:
    """Resolve a device's update interval: the device's own `update:` ->
    `module_update` (_ModuleConfig.update, from modules.<name>.update) ->
    the module's own `update:` default. A named value is looked up in
    `intervals_map` (the system YAML's top-level `intervals:`) before being
    parsed as a duration. Returns None if unset (the device is then never
    auto-polled) -- an explicit `update: null` at any of the three levels
    means exactly that, distinct from the key being absent there."""
    if "update" in instance_entry:
        value = instance_entry["update"]
    elif module_update is not _UNSET:
        value = module_update
    else:
        value = module.update
    if value is None:
        return None
    if isinstance(value, str) and value in intervals_map:
        value = intervals_map[value]
    return parse_duration(value)


# Keys _build_device/_merge_endpoints recognize on a device entry / endpoint
# entry. A typo'd key here fails open elsewhere in the stack -- e.g. a
# misspelled "profil:" silently yields a device with zero endpoints, and
# devices/zway/device.py's setup() documents that a misconfigured endpoint
# "permanently reports None, never raises" -- so an unrecognized key is
# rejected here rather than silently ignored.
_DEVICE_ENTRY_KEYS = {"id", "module", "name", "params", "endpoints", "update", "children", "profile"}
_ENDPOINT_ENTRY_KEYS = {"key", "kind", "readable", "writable", "parameters", "description",
                        "type", "unit", "values", "log_aggregation", "min", "max", "default",
                        "profile"}
_MODULES_ENTRY_KEYS = {"params", "update"}


def _build_device(entry: dict, intervals_map: dict, parent_qualified_id: str | None,
                   flat: dict[str, Device], modules_config: dict,
                   module_config_cache: dict[str, "_ModuleConfig"]) -> Device:
    """Recursively build one `devices:` YAML entry (and its children) into a
    Device tree, registering every device by qualified id in `flat` as it
    goes. Raises ConfigError on a duplicate qualified id or an unrecognized
    entry key."""
    device_id = entry["id"]
    module_name = entry["module"]
    unknown = set(entry) - _DEVICE_ENTRY_KEYS
    if unknown:
        raise ConfigError(f"device {device_id!r}: unrecognized key(s) {sorted(unknown)}")
    module = _load_module_descriptor(module_name)
    device_cls = get_device_class(module_name)

    if module_name not in module_config_cache:
        module_config_cache[module_name] = _resolve_module_config(module, modules_config)
    module_config = module_config_cache[module_name]

    params = _merge_params(module, entry.get("params", {}), device_id,
                           module_config.module_params, module_config.device_param_defaults)
    endpoint_specs = _expand_endpoint_specs(module, entry, params, device_id)
    endpoints, seeds = _merge_endpoints(module, endpoint_specs, device_id)
    update_interval = _resolve_interval(module, entry, intervals_map, module_config.update)

    qualified_id = f"{parent_qualified_id}.{device_id}" if parent_qualified_id else device_id

    children = [
        _build_device(child_entry, intervals_map, qualified_id, flat, modules_config,
                      module_config_cache)
        for child_entry in entry.get("children", [])
    ]

    device = device_cls(
        device_id,
        name=entry.get("name", ""),
        params=params,
        endpoints=endpoints,
        children=children,
        update_interval=update_interval,
        parent_qualified_id=parent_qualified_id,
    )

    for ep, default_value in seeds:
        ep.set(default_value)
        ep.update_state()

    if qualified_id in flat:
        raise ConfigError(f"duplicate device id: {qualified_id!r}")
    flat[qualified_id] = device

    return device


def _build_condition(spec: dict | None, flat: dict[str, Device], task_tag: str,
                      sticky_endpoints: set) -> Condition | ExprCondition | None:
    """Build a task's `condition:` YAML entry into a Condition or
    ExprCondition, or None if absent. Requires exactly one of `device` (the
    {device, changed} shorthand) or `expr` (a restricted-Python boolean
    expression, see core.scripting). Raises ConfigError if a referenced
    device doesn't exist or `expr` violates the sandbox whitelist.

    For `expr`, every endpoint it references -- via `refs:` or inline as a
    string literal (`state("house.motion1.state")`, see
    scripting.Compiled.referenced_paths) -- is subscribed for sticky log
    tracking under `task_tag` and added to `sticky_endpoints`, so
    load_system()'s tick hook keeps its sticky() window advancing every
    tick regardless of when/whether this condition is evaluated."""
    if spec is None:
        return None
    has_device = "device" in spec
    has_expr = "expr" in spec
    if has_device == has_expr:
        raise ConfigError(f"task {task_tag!r}: condition requires exactly one of 'device' or 'expr'")

    if has_expr:
        try:
            compiled = scripting.compile_expression(spec["expr"])
        except scripting.ScriptError as exc:
            raise ConfigError(f"task {task_tag!r}: {exc}") from None
        refs = spec.get("refs", {})
        for ref in set(refs.values()) | compiled.referenced_paths:
            device_id, endpoint_key = resolve_endpoint_ref(ref)
            if device_id not in flat:
                raise ConfigError(f"task {task_tag!r}: condition device {device_id!r} not found")
            endpoint = flat[device_id].endpoint(endpoint_key)
            endpoint.subscribe_log(task_tag)
            sticky_endpoints.add(endpoint)
        return ExprCondition(compiled=compiled, refs=refs, task_tag=task_tag, flat=flat)

    device_id, endpoint_key = resolve_endpoint_ref(spec["device"])
    if device_id not in flat:
        raise ConfigError(f"task {task_tag!r}: condition device {device_id!r} not found")
    return Condition(device_id=device_id, endpoint_key=endpoint_key, changed=spec.get("changed", True))


def _build_action(spec: dict, flat: dict[str, Device], task_tag: str, tasks: list[Task],
                   extensions: dict[str, object], sticky_endpoints: set):
    """Build one `action:`/`actions[]` YAML entry into an Action instance,
    dispatching on `kind` (via the task-kind registry). An action kind with
    `requires_device = False` (e.g. create_task, script, log_db) has no
    single target device/endpoint -- it's built from
    `flat`/`tasks`/`extensions`/`task_tag`/`sticky_endpoints` instead, so it
    can act on the task list itself, look up a named extension instance
    (see core.config._load_extensions), or (kind: script) run against the
    shared rule namespace (see core.task._build_rule_namespace). Raises
    ConfigError on a missing/unregistered kind, an action device that
    doesn't exist, a spec missing one of that kind's own required
    constructor arguments (e.g. create_task's `specs`), or -- for kind:
    script -- a `code:` block that violates the sandbox whitelist."""
    kind = spec.get("kind")
    if kind is None:
        raise ConfigError(f"task {task_tag!r}: action requires a 'kind'")
    try:
        action_cls = get_task_kind_class(kind)
    except KeyError as exc:
        raise ConfigError(f"task {task_tag!r}: {exc}") from None

    extra = {k: v for k, v in spec.items() if k not in ("kind", "device")}

    if not getattr(action_cls, "requires_device", True):
        try:
            action = action_cls(flat=flat, tasks=tasks, extensions=extensions, task_tag=task_tag,
                                 sticky_endpoints=sticky_endpoints, **extra)
        except (TypeError, scripting.ScriptError) as exc:
            raise ConfigError(f"task {task_tag!r}: invalid {kind!r} action: {exc}") from None
        if kind == "script":
            # See _build_condition's expr handling, above -- same
            # referenced-path validation/sticky-subscription, for a script
            # action's code instead of a condition's expr.
            for ref in set(action.refs.values()) | action.compiled.referenced_paths:
                device_id, endpoint_key = resolve_endpoint_ref(ref)
                if device_id not in flat:
                    raise ConfigError(f"task {task_tag!r}: action device {device_id!r} not found")
                endpoint = flat[device_id].endpoint(endpoint_key)
                endpoint.subscribe_log(task_tag)
                sticky_endpoints.add(endpoint)
        return action

    # allow_bare=True: an action's device may omit the endpoint (e.g.
    # "meteo-bern" rather than "meteo-bern.temperature") -- the Action
    # subclass then falls back to whole-device get()/set() semantics.
    # Conditions (_build_condition, above) intentionally do NOT allow this.
    device_id, endpoint_key = resolve_endpoint_ref(spec["device"], allow_bare=True)
    if device_id not in flat:
        raise ConfigError(f"task {task_tag!r}: action device {device_id!r} not found")
    return action_cls(device_id=device_id, endpoint_key=endpoint_key, **extra)


def _build_task(entry: dict, flat: dict[str, Device], tasks: list[Task],
                 extensions: dict[str, object], sticky_endpoints: set) -> Task:
    """Build one `tasks:` YAML entry (or a `create_task`/script action's
    nested spec) into a Task. Requires exactly one of `condition` or `time`
    to determine how the task fires, and exactly one of `action`/`actions`.
    `min_interval` (optional, default 0: no cooldown) is parsed the same way
    as `repeat` -- see Task's class docstring."""
    tag = entry["tag"]
    repeat_spec = entry.get("repeat", 0)
    repeat_seconds = parse_duration(repeat_spec) if repeat_spec else 0.0
    min_interval_spec = entry.get("min_interval", 0)
    min_interval = parse_duration(min_interval_spec) if min_interval_spec else 0.0

    condition = _build_condition(entry.get("condition"), flat, tag, sticky_endpoints)

    if condition is not None:
        due_time = float("-inf")
    else:
        time_spec = entry.get("time")
        if time_spec is None:
            raise ConfigError(f"task {tag!r}: 'time' is required unless a 'condition' is given")
        due_time = parse_time(str(time_spec), repeat=repeat_seconds or None)

    has_action = "action" in entry
    has_actions = "actions" in entry
    if has_action == has_actions:
        raise ConfigError(f"task {tag!r}: specify exactly one of 'action' or 'actions'")

    if has_actions:
        action_specs = entry["actions"]
        if not isinstance(action_specs, list) or not action_specs:
            raise ConfigError(f"task {tag!r}: 'actions' must be a non-empty list")
        actions = [_build_action(spec, flat, tag, tasks, extensions, sticky_endpoints)
                   for spec in action_specs]
    else:
        actions = [_build_action(entry["action"], flat, tag, tasks, extensions, sticky_endpoints)]

    return Task(
        tag,
        description=entry.get("description", ""),
        due_time=due_time,
        repeat=repeat_seconds,
        min_interval=min_interval,
        condition=condition,
        actions=actions,
    )


def _make_sticky_tick_hook(sticky_endpoints: set):
    """Build a tick hook (see Scheduler pass 4) that advances every rule-
    referenced endpoint's sticky min/max window each tick -- the same
    mechanism extensions.logdb uses for its own subscriptions (see
    LogDbInstance.on_tick), but for condition.expr's/kind:script's
    sticky()/reset_sticky() (see core.task._build_rule_namespace). Closes
    over the SAME set object _build_condition/_build_action populate, so a
    task created later at runtime (via create_task) that adds to it is
    picked up automatically on the next tick -- no re-registration needed."""
    def _hook(devices: dict[str, Device]) -> None:
        for endpoint in sticky_endpoints:
            endpoint.update_log_value()
    return _hook


class System:
    """A fully-loaded system: the device tree, its flat id index, tasks, and
    scheduler settings, as built by load_system()."""

    def __init__(self, heartbeat: float, roots: list[Device], devices: dict[str, Device],
                 tasks: list[Task] | None = None, max_workers: int | None = None,
                 fetch_timeout: float | None = None, tick_hooks: list | None = None,
                 start_hooks: list | None = None, stop_hooks: list | None = None):
        self.heartbeat = heartbeat
        self.roots = roots
        self.devices = devices
        self.tasks = tasks or []
        # Concurrency controls for the Scheduler's device I/O (see Scheduler):
        # max_workers bounds the thread pool; fetch_timeout (seconds) bounds how
        # long a tick waits on any one device before moving on. Both optional.
        self.max_workers = max_workers
        self.fetch_timeout = fetch_timeout
        # Per-tick callables auto-collected from configured extension
        # instances that expose an on_tick(devices) method (see
        # _load_extensions/load_system) -- passed straight to
        # Scheduler(tick_hooks=...).
        self.tick_hooks = tick_hooks or []
        # One-time async lifecycle callables auto-collected from configured
        # extension instances that expose on_start(devices)/on_stop(devices)
        # (see _load_extensions/load_system) -- passed straight to
        # Scheduler(start_hooks=..., stop_hooks=...).
        self.start_hooks = start_hooks or []
        self.stop_hooks = stop_hooks or []

    def scheduled_devices(self) -> dict[str, Device]:
        """Return the subset of `devices` that have an update_interval (i.e.
        are auto-polled by the Scheduler)."""
        return {qid: d for qid, d in self.devices.items() if d.update_interval is not None}


def load_system(path: str | Path, log_levels_override: dict | None = None) -> System:
    """Load and build a complete System from a system YAML file at `path`.

    Configures logging and discovers device modules/extensions as a side
    effect, then builds the device tree, resolves each device's
    params/endpoints/interval against its module, builds every configured
    extension instance (see _load_extensions), and builds tasks.
    `log_levels_override` (e.g. from CLI flags) is merged into every stream
    destination's `levels:` -- see core.logging_setup.configure_logging.
    """
    _include_stack.clear()
    with open(path, "r") as f:
        raw = yaml.load(f, Loader=_IncludeLoader) or {}

    if "log_levels" in raw:
        raise ConfigError(
            "log_levels: is no longer a separate top-level key -- put each destination's "
            "'levels:' under log: instead, e.g. "
            "log: [{dest: stdout, levels: {default: INFO, scheduler: DEBUG}}]")
    try:
        configure_logging(raw.get("log"), log_levels_override, Path(path).resolve().parent)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    discover_modules()
    discover_extensions()

    intervals_map = raw.get("intervals") or {}
    modules_config = raw.get("modules") or {}
    for module_name, module_entry in modules_config.items():
        unknown = set(module_entry or {}) - _MODULES_ENTRY_KEYS
        if unknown:
            raise ConfigError(f"modules.{module_name}: unrecognized key(s) {sorted(unknown)}")
    heartbeat = parse_duration(raw.get("heartbeat", "1s"))

    max_workers = raw.get("max_workers")
    if max_workers is not None:
        max_workers = int(max_workers)
    fetch_timeout_raw = raw.get("fetch_timeout")
    fetch_timeout = parse_duration(fetch_timeout_raw) if fetch_timeout_raw is not None else None

    # Validate every modules.<name>: entry up front, regardless of whether a
    # device actually uses that module -- otherwise a typo'd module name
    # (e.g. "zwya" instead of "zway") is silently ignored here and only
    # surfaces later, confusingly, as "parameter 'base_url' is required" on
    # the first zway device. _load_module_descriptor raises ConfigError for
    # an unknown module name; _resolve_module_config validates its params.
    flat: dict[str, Device] = {}
    module_config_cache: dict[str, _ModuleConfig] = {}
    for module_name in modules_config:
        module = _load_module_descriptor(module_name)
        module_config_cache[module_name] = _resolve_module_config(module, modules_config)

    roots = [
        _build_device(entry, intervals_map, None, flat, modules_config, module_config_cache)
        for entry in raw.get("devices", [])
    ]

    extensions_registry = _load_extensions(raw, flat)
    tick_hooks = [obj.on_tick for obj in extensions_registry.values() if hasattr(obj, "on_tick")]
    # One-time async lifecycle hooks (see core.scheduler.Scheduler's
    # start_hooks/stop_hooks) -- same auto-collection idea as tick_hooks
    # above, but for an extension instance that needs to start/stop a
    # long-lived resource (e.g. extensions.web_ui's aiohttp server) once,
    # rather than every tick.
    start_hooks = [obj.on_start for obj in extensions_registry.values() if hasattr(obj, "on_start")]
    stop_hooks = [obj.on_stop for obj in extensions_registry.values() if hasattr(obj, "on_stop")]

    tasks: list[Task] = []
    sticky_endpoints: set = set()
    for entry in raw.get("tasks", []):
        tasks.append(_build_task(entry, flat, tasks, extensions_registry, sticky_endpoints))
    if sticky_endpoints:
        tick_hooks.append(_make_sticky_tick_hook(sticky_endpoints))

    tags = [t.tag for t in tasks]
    duplicates = {t for t in tags if tags.count(t) > 1}
    if duplicates:
        raise ConfigError(f"duplicate task tag(s): {sorted(duplicates)}")

    return System(heartbeat=heartbeat, roots=roots, devices=flat, tasks=tasks,
                  max_workers=max_workers, fetch_timeout=fetch_timeout, tick_hooks=tick_hooks,
                  start_hooks=start_hooks, stop_hooks=stop_hooks)
