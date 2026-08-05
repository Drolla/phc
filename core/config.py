"""System YAML loading: parses a system config file into a System (devices,
tasks, and scheduler settings), merging each device's params/endpoints
against its module's declared module.yaml along the way."""

import copy
import importlib
import re
import time
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
    system YAML can pull in child YAML files (see _include_constructor), and
    <<: !include <relative-path>, which merges the included mapping's keys
    into the surrounding mapping -- the surrounding mapping's own keys win,
    the same precedence an ordinary `<<: *anchor` merge already has (see
    construct_mapping below). Only a single !include as the merge value is
    supported (not a sequence mixing !include with *anchor merges)."""

    def construct_mapping(self, node, deep=False):
        if not isinstance(node, yaml.MappingNode):
            return super().construct_mapping(node, deep=deep)
        # Pull any `<<: !include ...` pairs out of the node's own value list
        # before handing the rest to the normal PyYAML construction (which
        # still runs its own merge-key handling, e.g. `<<: *anchor`, on
        # whatever's left) -- PyYAML's own merge machinery works purely at
        # the node level (splicing raw (key, value) node pairs together
        # before any construction happens), so it can't merge in an
        # !include's *constructed* dict; this constructs each !include
        # target first and merges the resulting dicts by hand instead.
        included = {}
        kept = []
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge" and value_node.tag == "!include":
                target = self.construct_object(value_node, deep=True)
                if not isinstance(target, dict):
                    raise ConfigError(
                        f"'<<: !include' target must be a mapping, got "
                        f"{type(target).__name__} ({value_node.start_mark})")
                included.update(target)
            else:
                kept.append((key_node, value_node))
        node.value = kept
        result = super().construct_mapping(node, deep=deep)
        if not included:
            return result
        included.update(result)
        return included


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


def _flatten_list_entries(raw_entries: list) -> list:
    """Splice a `- !include <path>` item whose target resolves to a list
    into the surrounding list, instead of nesting it as one list-of-lists
    element. See docs/configuration.md#splitting-configuration-across-files.
    Used by every top-level/nested list this module builds: `devices:`,
    a host device's `children:`, `task_specs:`, `tasks:`."""
    flat = []
    for entry in raw_entries:
        if isinstance(entry, list):
            flat.extend(entry)
        else:
            flat.append(entry)
    return flat


# Keys recognized on an endpoint spec, at every stage before a module's own
# `endpoint_parameters:` names are folded into Endpoint.params (see
# _merge_endpoints). `params:` itself is deliberately absent -- a protocol
# value (e.g. zway's command_group/address) is written as an ordinary
# top-level field, declared per-module via endpoint_parameters:, not nested;
# this also means a plain dict.update is enough to overlay one endpoint spec
# onto another (see _overlay_endpoint_spec) without a separate deep-merge
# rule for params.
_ENDPOINT_ENTRY_KEYS = {"key", "kind", "readable", "writable", "name", "description",
                        "type", "unit", "values", "log_aggregation", "min", "max",
                        "default", "format", "endpoint_profile", "read_transform",
                        "write_transform", "history"}

# Keys allowed on a `history: {size: ..., interval: ...}` long-form mapping.
# `size` is required; `interval` is optional (defaults to the owning
# device's resolved update interval -- see _collect_history_records).
_HISTORY_ENTRY_KEYS = {"size", "interval"}

# Metadata keys allowed on a device_profiles entry alongside its required
# `endpoints:` list -- brand/type/product/description are documentation only,
# never consumed at runtime (see ModuleDescriptor.device_profiles).
_DEVICE_PROFILE_KEYS = {"brand", "type", "product", "description", "endpoints"}

# Keys _build_device recognizes on a device entry, and modules.<name>:
# recognizes on a module entry -- `params:` is deliberately absent from
# both: a declared device/module parameter (module.yaml's `parameters:`) is
# an ordinary top-level field, the same choice already made for endpoint
# parameters above. A typo'd key here fails open elsewhere in the stack --
# e.g. a misspelled "device_profil:" silently yields a device with zero
# endpoints, and devices/zway/device.py's setup() documents that a
# misconfigured endpoint "permanently reports None, never raises" -- so an
# unrecognized key is rejected here rather than silently ignored.
_DEVICE_ENTRY_KEYS = {"id", "module", "name", "endpoints", "update", "children",
                      "device_profile"}
# device_profiles/endpoint_profiles here let a system config extend a
# module's profile library with its own entries (see
# _build_effective_module) -- e.g. for a household-specific fixture that
# isn't a real shared product and so doesn't belong in the module's own
# module.yaml.
_MODULES_ENTRY_KEYS = {"update", "device_profiles", "endpoint_profiles"}


def _parse_profile_library(owner_label: str, raw_endpoint_profiles: dict | None,
                            raw_device_profiles: dict | None, endpoint_param_names: set,
                            extra_endpoint_specs=()) -> tuple[dict, dict]:
    """Parse+validate one endpoint_profiles/device_profiles mapping pair --
    shared by ModuleDescriptor.__init__ (a module.yaml's own library) and
    _build_effective_module (a system YAML's modules.<name>.device_profiles/
    endpoint_profiles overlay). `owner_label` (e.g. "module 'zway'" or
    "modules.zway") only names the source in error messages.
    `extra_endpoint_specs` are endpoint specs validated alongside the two
    libraries but not returned -- only ModuleDescriptor passes its own
    unconditional `endpoints:` here, folding them into the same
    key-validation pass. Returns (endpoint_profiles, device_profiles) as
    plain dicts. Does not check name collisions against a second source, or
    the endpoints:/device_profiles mutual-exclusion rule -- both are
    specific to which two sources are being combined and stay with the
    caller."""
    endpoint_profiles = raw_endpoint_profiles or {}
    device_profiles = {}
    for profile_name, profile_raw in (raw_device_profiles or {}).items():
        if not isinstance(profile_raw, dict):
            raise ConfigError(
                f"{owner_label}: device_profiles[{profile_name!r}] must be a mapping "
                f"with brand/type/product/description/endpoints -- a bare list of endpoint "
                f"specs is the old device_profiles shape and is no longer supported")
        unknown = set(profile_raw) - _DEVICE_PROFILE_KEYS
        if unknown:
            raise ConfigError(
                f"{owner_label}: device_profiles[{profile_name!r}] has unrecognized "
                f"key(s) {sorted(unknown)}")
        if "endpoints" not in profile_raw:
            raise ConfigError(
                f"{owner_label}: device_profiles[{profile_name!r}] is missing required "
                f"key 'endpoints'")
        device_profiles[profile_name] = dict(profile_raw)
    allowed_endpoint_keys = _ENDPOINT_ENTRY_KEYS | endpoint_param_names
    for spec in (*extra_endpoint_specs, *endpoint_profiles.values(),
                 *(ep for profile in device_profiles.values() for ep in profile["endpoints"])):
        unknown = set(spec) - allowed_endpoint_keys
        if unknown:
            raise ConfigError(
                f"{owner_label}: endpoint spec {spec.get('key')!r} has unrecognized "
                f"key(s) {sorted(unknown)}")
    return endpoint_profiles, device_profiles


class ModuleDescriptor:
    """Parsed module.yaml for one device module. See
    docs/developer/writing-a-device-module.md for the full module.yaml
    schema (parameters, endpoint_parameters, endpoint_profiles/
    device_profiles, {param} templating).

    device_profiles is mutually exclusive with a non-empty `endpoints:` on
    the SAME module: `endpoints:` is unconditional (every device of the
    module gets them, e.g. meteoswiss's six), while a device_profiles entry
    is opt-in -- mixing the two would make one of "module endpoints" or
    "profile endpoints" the base and the other the overlay depending on
    call order, which is not something a reader of the YAML could tell.

    A system config can extend this library too, under modules.<name> in
    the system YAML, without touching this module's own module.yaml -- see
    _build_effective_module, which merges those onto a copy of this
    descriptor (this cached instance itself is never mutated)."""

    def __init__(self, name: str, raw: dict):
        self.name = name
        self.description = raw.get("description", "")
        self.parameters = raw.get("parameters") or []
        param_names = {p["name"] for p in self.parameters}
        # "params" itself is reserved even though it's no longer a device/
        # modules entry key -- a device param literally named "params"
        # would be indistinguishable from the old nested-dict spelling.
        reserved = _DEVICE_ENTRY_KEYS | _MODULES_ENTRY_KEYS | {"params"}
        collision = param_names & reserved
        if collision:
            raise ConfigError(
                f"module {name!r}: parameters name(s) {sorted(collision)} collide "
                f"with a reserved device/modules entry key")
        self.endpoint_parameters = raw.get("endpoint_parameters") or []
        self.endpoint_param_names = {p["name"] for p in self.endpoint_parameters}
        endpoint_reserved = _ENDPOINT_ENTRY_KEYS | {"params"}
        endpoint_collision = self.endpoint_param_names & endpoint_reserved
        if endpoint_collision:
            raise ConfigError(
                f"module {name!r}: endpoint_parameters name(s) {sorted(endpoint_collision)} "
                f"collide with a reserved endpoint field name")
        self.endpoints = raw.get("endpoints") or []
        self.update = raw.get("update", None)
        self.endpoint_profiles, self.device_profiles = _parse_profile_library(
            f"module {name!r}", raw.get("endpoint_profiles"), raw.get("device_profiles"),
            self.endpoint_param_names, extra_endpoint_specs=self.endpoints)
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
    - device_param_defaults: `scope: device` parameter values supplied
      directly under modules.<name>, which become a *default* for every
      device of this module, still overridable per device -- new in this
      scheme.
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
    # A declared param is an ordinary top-level field here, same as on a
    # device entry -- the _MODULES_ENTRY_KEYS are reserved at this level
    # (device_profiles/endpoint_profiles are handled separately by
    # _build_effective_module), so everything else is a parameter value.
    module_config_params = {k: v for k, v in module_entry.items()
                             if k not in _MODULES_ENTRY_KEYS}
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
                    f"(override: none) but modules.{module.name} sets it")
            value = module_config_params.pop(name)
            if scope == "module":
                module_params[name] = value
            else:
                device_param_defaults[name] = value
        elif scope == "module":
            if override == "required":
                raise ConfigError(
                    f"module {module.name!r}: parameter {name!r} is required but not supplied "
                    f"under modules.{module.name}")
            module_params[name] = spec.get("default")
        # scope == "device" and not set here: no entry in
        # device_param_defaults -- _merge_params falls back to spec["default"].

    if module_config_params:
        unknown = ", ".join(repr(k) for k in sorted(module_config_params))
        raise ConfigError(
            f"modules.{module.name}: unrecognized key(s) {unknown} -- not a reserved "
            f"modules.<name> key ({', '.join(sorted(_MODULES_ENTRY_KEYS))}) and not a "
            f"parameter declared by module {module.name!r}")

    return _ModuleConfig(module_params, device_param_defaults, update)


def _build_effective_module(module: ModuleDescriptor, modules_config: dict) -> ModuleDescriptor:
    """Return the ModuleDescriptor `_expand_endpoint_specs` should actually
    resolve device_profile:/endpoint_profile: against for this module:
    `module` itself, unless modules.<name> supplies its own
    device_profiles/endpoint_profiles, in which case those are merged in on
    a COPY (module-scoped, exactly like a module's own module.yaml library
    -- a system-supplied profile for module X is invisible to a device of
    any other module).

    Never mutates `module` in place: `module` is the process-global,
    never-invalidated object cached by _load_module_descriptor (shared
    across every load_system() call in the process), so mutating its
    .device_profiles/.endpoint_profiles would leak one system config's
    profiles into another's use of the same module. The common case (no
    system-level profiles for this module) returns `module` unchanged --
    no copy, no cost.

    A system-supplied profile name colliding with one module.yaml already
    declares is a ConfigError, not a silent override -- consistent with
    this module's general refusal to let config ambiguously shadow itself
    (see e.g. the parameter-name collision checks in ModuleDescriptor,
    or the duplicate-device-id check in _build_device). The
    device_profiles-vs-nonempty-endpoints: mutual exclusivity that
    ModuleDescriptor.__init__ already enforces for module.yaml alone (see
    its docstring) is extended here to also cover a system-supplied
    device_profiles against the module's own `endpoints:` -- same
    base/overlay ambiguity, regardless of which side the device_profiles
    came from."""
    module_entry = modules_config.get(module.name) or {}
    raw_endpoint_profiles = module_entry.get("endpoint_profiles")
    raw_device_profiles = module_entry.get("device_profiles")
    if not raw_endpoint_profiles and not raw_device_profiles:
        return module

    system_endpoint_profiles, system_device_profiles = _parse_profile_library(
        f"modules.{module.name}", raw_endpoint_profiles, raw_device_profiles,
        module.endpoint_param_names)

    endpoint_collision = set(system_endpoint_profiles) & set(module.endpoint_profiles)
    if endpoint_collision:
        raise ConfigError(
            f"modules.{module.name}: endpoint_profiles name(s) {sorted(endpoint_collision)} "
            f"already declared by module {module.name!r} in module.yaml")
    device_collision = set(system_device_profiles) & set(module.device_profiles)
    if device_collision:
        raise ConfigError(
            f"modules.{module.name}: device_profiles name(s) {sorted(device_collision)} "
            f"already declared by module {module.name!r} in module.yaml")
    if module.endpoints and system_device_profiles:
        raise ConfigError(
            f"modules.{module.name}: device_profiles and this module's non-empty module.yaml "
            f"endpoints: are mutually exclusive, the same as within module.yaml itself -- "
            f"endpoints: is unconditional (every device gets them) while device_profiles is "
            f"opt-in, so combining them leaves no well-defined base/overlay order")

    effective = copy.copy(module)
    effective.endpoint_profiles = {**module.endpoint_profiles, **system_endpoint_profiles}
    effective.device_profiles = {**module.device_profiles, **system_device_profiles}
    return effective


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
    """Merge one device instance's declared-parameter fields (already
    separated from its other entry keys by the caller -- see _build_device)
    against its module's declared parameters. `scope: module` params come
    from the already-resolved `resolved_module_params`
    (_ModuleConfig.module_params). `scope: device` params are resolved
    device-entry field -> `module_param_defaults`
    (_ModuleConfig.device_param_defaults, a per-module default set directly
    under modules.<name> -- never popped, since it is shared/cached across
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
                    f"(scope: module) and cannot be set on this device; "
                    f"set it under modules.{module.name} instead")
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
            # override: none for a param set directly under modules.<name>
            # was already rejected once, in _resolve_module_config, before
            # any device gets here.
            merged[name] = module_param_defaults[name]
        elif override == "required":
            raise ConfigError(
                f"device {device_id!r}: parameter {name!r} is required but not supplied")
        else:
            merged[name] = spec.get("default")

    if instance_params:
        unknown = ", ".join(repr(k) for k in sorted(instance_params))
        raise ConfigError(
            f"device {device_id!r}: unrecognized key(s) {unknown} -- not a device entry key "
            f"({', '.join(sorted(_DEVICE_ENTRY_KEYS))}) and not a parameter declared by "
            f"module {module.name!r}")

    return merged


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
    """Shallow-overlay `over` onto `base` (a plain dict.update). A declared
    endpoint parameter (e.g. zway's command_group/address) is an ordinary
    top-level field at this stage -- not folded into `params:` until
    _merge_endpoints, once every spec is fully resolved -- so tweaking just
    one of them (e.g. `endpoints: [{key: battery, address: "16.0"}]`) only
    replaces that one key; a sibling like command_group, untouched by
    `over`, survives automatically. No field needs special-case merging."""
    return {**base, **over}


def _substitute_templates(value, params: dict, device_id: str, endpoint_key: str, field: str = ""):
    """Recursively substitute `{param}` templates in every string found in
    `value` (an endpoint spec, or a nested dict/value within one, e.g.
    `params:`/`values:`) from the device's already-resolved `params` (e.g.
    `address: "{node}.0.1"` -> "11.0.1" when params["node"] == 11). Dicts are
    walked key-by-key; non-string, non-dict values (int, bool, list, None)
    pass through untouched. Raises ConfigError if a template references a
    param that is missing or None -- a forgotten `node:` would otherwise
    format to the literal string "7.None" via plain str.format_map, which
    devices/zway/device.py's setup() accepts as a real (if wrong) value_id
    and then reports None forever with no error -- or if the template
    itself is malformed."""
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
    """Substitute `{param}` templates throughout one endpoint spec's fields
    (a declared endpoint parameter like address, description, unit,
    values:, ...) from the device's resolved `params` -- see
    _substitute_templates. Runs on every endpoint of every device regardless
    of whether it came from a profile, a hand-written instance override, or
    a module's own unconditional `endpoints:` -- a spec with no `{...}`
    anywhere passes through unchanged, so this is a no-op for every module
    that declares no templates. Skips fields in _TEMPLATE_EXCLUDED_FIELDS
    (key, kind, type, ...), which are structural metadata rather than
    display/protocol data."""
    endpoint_key = spec.get("key", "")
    return {
        field: value if field in _TEMPLATE_EXCLUDED_FIELDS
        else _substitute_templates(value, params, device_id, endpoint_key, field=field)
        for field, value in spec.items()
    }


def _expand_endpoint_specs(module: ModuleDescriptor, entry: dict, device_id: str) -> list:
    """Expand a device entry's `endpoints:` (and its own top-level
    `device_profile:`, if any) against the module's endpoint_profiles/
    device_profiles library (see ModuleDescriptor). Returns a plain list of
    endpoint dicts in the same shape a user writes by hand, ready for
    _merge_endpoints -- which needs no awareness that profiles exist.
    `{param}` templating is a separate, later step (see
    _substitute_endpoint_spec, called from _merge_endpoints) -- this
    function only resolves which endpoints exist and how they overlay, not
    their template values.

    A device that uses no `device_profile:` anywhere (neither its own
    top-level `device_profile:` nor any endpoint's `endpoint_profile:`) gets
    its `endpoints:` list back completely unchanged -- this identity case is
    what guarantees writing endpoints fully explicitly keeps working exactly
    as before.

    Resolution order: the device's own `device_profile:` (if set) expands
    into a base endpoints list first, in the profile's `endpoints:` order;
    the device's own `endpoints:` then overlays that list by `key` (via
    _overlay_endpoint_spec, so e.g. an `address:` tweak doesn't clobber a
    profile-derived command_group) and appends any key the profile didn't
    already provide. An `endpoints:` entry may set its own
    `endpoint_profile:` too (with or without a device-level
    `device_profile:`), for a single profile-derived endpoint on a device
    that otherwise writes everything out by hand. Final order:
    device-profile order, then extra/overlaid instance endpoints in
    `endpoints:` list order."""
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
    """Build this device instance's Endpoint objects: start from the module's
    declared endpoints, overlay any instance-level overrides (by `key`) and
    append instance-only endpoints not declared by the module. Returns
    (endpoints, seeds), where `seeds` are (Endpoint, default_value) pairs to
    apply once the device is constructed. `instance_endpoints` is normally
    the device's raw `endpoints:` list, already expanded against any
    device_profile:/endpoint_profile: by _expand_endpoint_specs -- this
    function itself has no awareness that profiles exist. `params` is the
    device's already-resolved params, used only to substitute `{param}`
    templates (see _substitute_endpoint_spec) in the fully-merged specs --
    templating is independent of whether an endpoint came from a profile.
    `intervals_map` (the system YAML's top-level `intervals:`) is only used
    to resolve a named `history.interval` -- see _parse_history_spec."""
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
        history_size, history_interval = 0, None
        if "history" in spec:
            if value_type == "str":
                raise ConfigError(
                    f"device {device_id!r}: endpoint {spec['key']!r} declares history: but "
                    f"has type 'str' -- history only aggregates numeric values")
            history_size, history_interval = _parse_history_spec(
                spec["history"], intervals_map or {}, device_id, spec["key"])
        cls = get_endpoint_class(spec.get("kind"))
        # Declared endpoint parameters (e.g. zway's command_group/address)
        # are ordinary top-level fields on `spec` all the way through
        # profile expansion, instance overlay, and {param} templating (see
        # _overlay_endpoint_spec/_substitute_endpoint_spec) -- folded into
        # Endpoint.params only here, once the spec is fully resolved.
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
            )
        except ValueError as exc:
            raise ConfigError(f"device {device_id!r}: {exc}") from None
        endpoints.append(ep)
        if "default" in spec:
            seeds.append((ep, spec["default"]))

    return endpoints, seeds


def _parse_interval_value(value, intervals_map: dict) -> float:
    """Resolve one duration value: a name is looked up in `intervals_map`
    (the system YAML's top-level `intervals:`) before being parsed as a
    duration string (see core.intervals.parse_duration). Shared by
    _resolve_interval (a device's `update:`) and _parse_history_spec (a
    history's `interval:`), so the two named-interval lookups can't drift."""
    if isinstance(value, str) and value in intervals_map:
        value = intervals_map[value]
    return parse_duration(value)


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
    return _parse_interval_value(value, intervals_map)


def _parse_history_spec(raw, intervals_map: dict, device_id: str, endpoint_key: str
                         ) -> tuple[int, float | None]:
    """Parse an endpoint spec's `history:` field into (size, interval),
    where interval is a duration in seconds or None (meaning "default to
    the owning device's resolved update interval" -- see
    _collect_history_records). Accepts either a bare positive int
    (shorthand for {size: N}) or a {size, interval} mapping; `interval`
    accepts a name from `intervals_map` before being parsed as a duration,
    exactly like a device's own `update:` (see _parse_interval_value).
    Raises ConfigError for anything that cannot work: an unrecognized
    mapping key, a missing/non-integer/non-positive size, or a malformed
    interval. No upper bound on size -- see docs/scripting.md for the
    memory/CPU cost this implies for a very large history."""
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


def _build_device(entry: dict, intervals_map: dict, parent_qualified_id: str | None,
                   flat: dict[str, Device], modules_config: dict,
                   module_config_cache: dict[str, "_ModuleConfig"],
                   effective_module_cache: dict[str, ModuleDescriptor]) -> Device:
    """Recursively build one `devices:` YAML entry (and its children) into a
    Device tree, registering every device by qualified id in `flat` as it
    goes. Raises ConfigError on a duplicate qualified id or an unrecognized
    entry key. Which keys count as "device entry keys" vs. "params" depends
    on the module (a declared parameter is an ordinary top-level field, see
    ModuleDescriptor), so unlike `_DEVICE_PROFILE_KEYS`-style checks this
    can't validate `entry`'s keys until after the module is loaded --
    _merge_params raises on whatever's left in `instance_params` once its
    own declared names are picked out."""
    device_id = entry["id"]
    module_name = entry["module"]
    module = _load_module_descriptor(module_name)
    device_cls = get_device_class(module_name)

    if module_name not in module_config_cache:
        module_config_cache[module_name] = _resolve_module_config(module, modules_config)
    module_config = module_config_cache[module_name]

    if module_name not in effective_module_cache:
        effective_module_cache[module_name] = _build_effective_module(module, modules_config)
    effective_module = effective_module_cache[module_name]

    instance_params = {k: v for k, v in entry.items() if k not in _DEVICE_ENTRY_KEYS}
    params = _merge_params(module, instance_params, device_id,
                           module_config.module_params, module_config.device_param_defaults)
    endpoint_specs = _expand_endpoint_specs(effective_module, entry, device_id)
    endpoints, seeds = _merge_endpoints(module, endpoint_specs, device_id, params, intervals_map)
    update_interval = _resolve_interval(module, entry, intervals_map, module_config.update)

    qualified_id = f"{parent_qualified_id}.{device_id}" if parent_qualified_id else device_id

    children = [
        _build_device(child_entry, intervals_map, qualified_id, flat, modules_config,
                      module_config_cache, effective_module_cache)
        for child_entry in _flatten_list_entries(entry.get("children", []))
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


def _subscribe_referenced_endpoints(paths: set[str], flat: dict[str, Device], task_tag: str,
                                     sticky_endpoints: set, context: str) -> None:
    """Validate and sticky-subscribe every "device.endpoint" path an expr or
    script references -- shared by `condition: {expr}`, a `script` action's
    `code`, and a `set` action's `expr`, so the three surfaces can't drift
    in how they validate/subscribe referenced endpoints. `context` (e.g.
    "condition"/"action") only changes the ConfigError wording below."""
    for ref in paths:
        device_id, endpoint_key = resolve_endpoint_ref(ref)
        if device_id not in flat:
            raise ConfigError(f"task {task_tag!r}: {context} device {device_id!r} not found")
        endpoint = flat[device_id].endpoint(endpoint_key)
        endpoint.subscribe_log(task_tag)
        sticky_endpoints.add(endpoint)


def _build_condition(spec: dict | None, flat: dict[str, Device], task_tag: str,
                      sticky_endpoints: set) -> Condition | ExprCondition | None:
    """Build a task's `condition:` YAML entry into a Condition or
    ExprCondition, or None if absent. Requires exactly one of `device` (the
    {device, changed, value} shorthand -- see Condition's docstring for how
    `changed`/`value` combine) or `expr` (a restricted-Python boolean
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
        _subscribe_referenced_endpoints(set(refs.values()) | compiled.referenced_paths,
                                         flat, task_tag, sticky_endpoints, context="condition")
        return ExprCondition(compiled=compiled, refs=refs, task_tag=task_tag, flat=flat)

    device_id, endpoint_key = resolve_endpoint_ref(spec["device"])
    if device_id not in flat:
        raise ConfigError(f"task {task_tag!r}: condition device {device_id!r} not found")
    return Condition(device_id=device_id, endpoint_key=endpoint_key,
                      changed=spec.get("changed"), value=spec.get("value"))


def _build_action(spec: dict, flat: dict[str, Device], task_tag: str, tasks: list[Task],
                   extensions: dict[str, object], sticky_endpoints: set,
                   task_specs: dict[str, dict] | None = None):
    """Build one `action:`/`actions[]` YAML entry into an Action instance,
    dispatching on `kind` (via the task-kind registry). Every kind is built
    the same way: device_id/endpoint_key are resolved from `device:` when
    given (allow_bare=True -- an action's device may omit the endpoint,
    e.g. "meteo-bern" rather than "meteo-bern.temperature", the Action
    subclass then falling back to whole-device get()/set() semantics;
    Conditions, above, intentionally do NOT allow this), None otherwise --
    and flat/tasks/extensions/task_tag/sticky_endpoints/task_specs are
    passed to every kind unconditionally, so a kind with no single device
    target (create_task, kill_task, script, and every extension action) can
    act on the task list itself, look up a named extension instance (see
    core.config._load_extensions) or a named `task_specs:` template (see
    core.task.CreateTaskAction), or (kind: script, or kind: set with an
    `expr:`) run against the shared rule namespace (see
    core.task._build_rule_namespace), while an ordinary device-oriented
    kind simply never touches them. Raises ConfigError on a
    missing/unregistered kind, a given device that doesn't exist, a spec
    missing one of that kind's own required constructor arguments (e.g.
    create_task's `specs`/`template`), or -- for kind: script, or kind: set
    with an `expr:` -- a code/expr block that violates the sandbox
    whitelist. A device-oriented kind (e.g. set, toggle) whose `device:` is
    missing builds without error here -- it fails at that action's own
    perform() instead, the first time it fires."""
    kind = spec.get("kind")
    if kind is None:
        raise ConfigError(f"task {task_tag!r}: action requires a 'kind'")
    try:
        action_cls = get_task_kind_class(kind)
    except KeyError as exc:
        raise ConfigError(f"task {task_tag!r}: {exc}") from None

    extra = {k: v for k, v in spec.items() if k not in ("kind", "device")}

    device_id = endpoint_key = None
    if "device" in spec:
        device_id, endpoint_key = resolve_endpoint_ref(spec["device"], allow_bare=True)
        if device_id not in flat:
            raise ConfigError(f"task {task_tag!r}: action device {device_id!r} not found")

    try:
        action = action_cls(device_id=device_id, endpoint_key=endpoint_key, flat=flat,
                             tasks=tasks, extensions=extensions, task_tag=task_tag,
                             sticky_endpoints=sticky_endpoints, task_specs=task_specs, **extra)
    except (TypeError, ValueError, scripting.ScriptError) as exc:
        raise ConfigError(f"task {task_tag!r}: invalid {kind!r} action: {exc}") from None

    # script always compiles code; set only compiles when it's given expr:
    # instead of a literal value: -- both need the same referenced-endpoint
    # validation/sticky-subscription (see _build_condition's expr handling,
    # above) before the config finishes loading.
    if kind == "script" or (kind == "set" and action.compiled is not None):
        _subscribe_referenced_endpoints(
            set(action.refs.values()) | action.compiled.referenced_paths,
            flat, task_tag, sticky_endpoints, context="action")
    return action


def _build_task(entry: dict, flat: dict[str, Device], tasks: list[Task],
                 extensions: dict[str, object], sticky_endpoints: set,
                 task_specs: dict[str, dict] | None = None) -> Task:
    """Build one `tasks:` YAML entry (or a `create_task`/script action's
    nested spec, or a `task_specs:` entry once instantiated by a
    `template:` reference) into a Task. See docs/configuration.md#tasks
    for the `condition`/`time`/`repeat`/`min_interval` semantics and the
    due_time defaulting matrix."""
    tag = entry["tag"]
    repeat_spec = entry.get("repeat")
    repeat_seconds = parse_duration(repeat_spec) if repeat_spec is not None else None
    min_interval_spec = entry.get("min_interval", 0)
    min_interval = parse_duration(min_interval_spec) if min_interval_spec else 0.0

    condition = _build_condition(entry.get("condition"), flat, tag, sticky_endpoints)

    time_spec = entry.get("time")
    if time_spec is None:
        if repeat_seconds is None and condition is not None:
            due_time = None
        else:
            due_time = parse_time("+0s", repeat=repeat_seconds if repeat_seconds else None)
    else:
        due_time = parse_time(str(time_spec), repeat=repeat_seconds if repeat_seconds else None)

    has_action = "action" in entry
    has_actions = "actions" in entry
    if has_action == has_actions:
        raise ConfigError(f"task {tag!r}: specify exactly one of 'action' or 'actions'")

    if has_actions:
        action_specs = entry["actions"]
        if not isinstance(action_specs, list) or not action_specs:
            raise ConfigError(f"task {tag!r}: 'actions' must be a non-empty list")
        actions = [_build_action(spec, flat, tag, tasks, extensions, sticky_endpoints, task_specs)
                   for spec in action_specs]
    else:
        actions = [_build_action(entry["action"], flat, tag, tasks, extensions,
                                  sticky_endpoints, task_specs)]

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


class _HistoryRecord:
    """One endpoint's history-sampling schedule: the Endpoint to sample,
    its resolved sampling interval (seconds), and the wall-clock time it is
    next due. Built once by _collect_history_records() and driven by
    _make_history_tick_hook()'s tick hook -- a plain __slots__ record (see
    scripting.Compiled's __slots__ for the same pattern), since this is
    internal bookkeeping, not part of any public API."""

    __slots__ = ("endpoint", "interval", "next_due")

    def __init__(self, endpoint: Endpoint, interval: float):
        self.endpoint = endpoint
        self.interval = interval
        # 0.0 so the very first tick hook invocation is immediately due --
        # matching Device._last_run's float("-inf") (see core.device),
        # which makes a freshly built device due on the scheduler's first
        # tick.
        self.next_due = 0.0


def _collect_history_records(flat: dict[str, Device]) -> list[_HistoryRecord]:
    """Build one _HistoryRecord for every endpoint (of every device in
    `flat`) that declared `history:`. An endpoint's own history_interval
    (declared `interval:`) wins; otherwise the owning device's resolved
    update_interval is used, so declaring just `history: N` gives "one
    sample per poll" for free. Raises ConfigError for an endpoint on a
    device with no update: interval and no explicit history interval --
    there would be no cadence to sample it on. Called once, after `roots`
    is built (see load_system), which is the first point every device's
    resolved update_interval is known."""
    records = []
    for qualified_id, device in flat.items():
        for endpoint in device.endpoints.values():
            if endpoint.history_size == 0:
                continue
            interval = endpoint.history_interval
            if interval is None:
                interval = device.update_interval
            if interval is None:
                raise ConfigError(
                    f"device {qualified_id!r}: endpoint {endpoint.key!r} declares "
                    f"history: but the device has no update: interval to sample on "
                    f"-- give the history an explicit interval "
                    f"(history: {{size: {endpoint.history_size}, interval: 5m}}) or "
                    f"set update: on the device")
            records.append(_HistoryRecord(endpoint, interval))
    return records


def _make_history_tick_hook(records: list[_HistoryRecord]):
    """Build a tick hook (see Scheduler pass 4) that samples every
    history-declaring endpoint's CURRENT committed value into its buffer,
    once per its resolved interval -- the history counterpart to
    _make_sticky_tick_hook, but on a per-endpoint cadence rather than every
    tick. Unlike _make_sticky_tick_hook's sticky_endpoints set, `records`
    is fixed at load time and never grows at runtime: unlike a task, a
    device (and hence its endpoints' history declarations) cannot be
    created after startup, so there is no live-set-aliasing trick needed
    here.

    One time.time() call per hook invocation (not per record), then a
    linear scan -- negligible even for many records. A record's next_due
    only advances when Endpoint.record_history() actually appended a
    sample (it skips None/non-numeric/NaN, see that method), so a device
    that hasn't been polled yet, or whose last read failed, is retried
    every tick instead of losing a whole interval. Deliberately not
    Task.mark_run()'s whole-multiples catch-up scheme: a burst is
    structurally impossible here (this hook runs at most once per tick,
    appending at most one sample), and a stable sampling grid has no value
    for a smoothing buffer -- so a plain `next_due = now + interval` is
    enough."""
    def _hook(devices: dict[str, Device]) -> None:
        now = time.time()
        for record in records:
            if now < record.next_due:
                continue
            if record.endpoint.record_history():
                record.next_due = now + record.interval
    return _hook


class System:
    """A fully-loaded system: the device tree, its flat id index, tasks, and
    scheduler settings, as built by load_system()."""

    def __init__(self, heartbeat: float, roots: list[Device], devices: dict[str, Device],
                 tasks: list[Task] | None = None, max_workers: int | None = None,
                 fetch_timeout: float | None = None, tick_hooks: list | None = None,
                 start_hooks: list | None = None, stop_hooks: list | None = None,
                 extensions: dict[str, object] | None = None):
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
        # The _load_extensions registry, keyed by "<extension_name>.<instance_name>"
        # -- exposed so a caller (e.g. phc.py's --debug-portal-port) can check
        # what the system YAML already configured before adding its own
        # extension instance on top of it.
        self.extensions = extensions or {}

    def scheduled_devices(self) -> dict[str, Device]:
        """Return the subset of `devices` that have an update_interval (i.e.
        are auto-polled by the Scheduler)."""
        return {qid: d for qid, d in self.devices.items() if d.update_interval is not None}


def load_system(path: str | Path, log_levels_override: dict | None = None) -> System:
    """Load and build a complete System from a system YAML file at `path`.

    Configures logging and discovers device modules/extensions as a side
    effect, then builds the device tree, resolves each device's
    params/endpoints/interval against its module, builds every configured
    extension instance (see _load_extensions), and builds tasks. Once the
    resulting System is assembled, calls on_bind(system) on every extension
    instance that defines it, so an instance needing the fully-built System
    (e.g. its tasks:, unavailable at that instance's own configure()-time)
    can bind to it before the Scheduler starts ticking.

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
    # an unknown module name; _resolve_module_config validates its params;
    # _build_effective_module validates any modules.<name>.device_profiles/
    # endpoint_profiles overlay.
    flat: dict[str, Device] = {}
    module_config_cache: dict[str, _ModuleConfig] = {}
    effective_module_cache: dict[str, ModuleDescriptor] = {}
    for module_name in modules_config:
        module = _load_module_descriptor(module_name)
        module_config_cache[module_name] = _resolve_module_config(module, modules_config)
        effective_module_cache[module_name] = _build_effective_module(module, modules_config)

    roots = [
        _build_device(entry, intervals_map, None, flat, modules_config, module_config_cache,
                      effective_module_cache)
        for entry in _flatten_list_entries(raw.get("devices", []))
    ]

    # Every device's update_interval is resolved by now, so this is the
    # first point an endpoint's history sampling cadence (its own
    # interval:, or else its device's update:) can be determined -- see
    # _collect_history_records.
    history_records = _collect_history_records(flat)

    extensions_registry = _load_extensions(raw, flat)
    tick_hooks = [obj.on_tick for obj in extensions_registry.values() if hasattr(obj, "on_tick")]
    if history_records:
        tick_hooks.append(_make_history_tick_hook(history_records))
    # One-time async lifecycle hooks (see core.scheduler.Scheduler's
    # start_hooks/stop_hooks) -- same auto-collection idea as tick_hooks
    # above, but for an extension instance that needs to start/stop a
    # long-lived resource (e.g. extensions.web_ui's aiohttp server) once,
    # rather than every tick.
    start_hooks = [obj.on_start for obj in extensions_registry.values() if hasattr(obj, "on_start")]
    stop_hooks = [obj.on_stop for obj in extensions_registry.values() if hasattr(obj, "on_stop")]

    # task_specs: entries are name -> raw dict, resolved lazily by a
    # `create_task` action's `template:` (see core.task.CreateTaskAction) --
    # not built into Task objects here, matching the existing laziness of a
    # create_task action's own literal `specs:` (see _build_task). Only a
    # duplicate-tag check happens eagerly, to catch a copy-paste mistake at
    # load time rather than only once some template is actually used.
    task_specs: dict[str, dict] = {}
    for entry in _flatten_list_entries(raw.get("task_specs", [])):
        spec_tag = entry["tag"]
        if spec_tag in task_specs:
            raise ConfigError(f"task_specs: duplicate tag {spec_tag!r}")
        task_specs[spec_tag] = entry

    tasks: list[Task] = []
    sticky_endpoints: set = set()
    for entry in _flatten_list_entries(raw.get("tasks", [])):
        tasks.append(_build_task(entry, flat, tasks, extensions_registry, sticky_endpoints, task_specs))
    if sticky_endpoints:
        tick_hooks.append(_make_sticky_tick_hook(sticky_endpoints))

    tags = [t.tag for t in tasks]
    duplicates = {t for t in tags if tags.count(t) > 1}
    if duplicates:
        raise ConfigError(f"duplicate task tag(s): {sorted(duplicates)}")

    system = System(heartbeat=heartbeat, roots=roots, devices=flat, tasks=tasks,
                     max_workers=max_workers, fetch_timeout=fetch_timeout, tick_hooks=tick_hooks,
                     start_hooks=start_hooks, stop_hooks=stop_hooks, extensions=extensions_registry)

    # Extra one-time hook, auto-collected like tick_hooks/start_hooks/
    # stop_hooks above, for an extension instance that needs to see the
    # fully-built System (e.g. its tasks: -- not yet built when this
    # instance's own configure() ran, see _load_extensions) before the
    # Scheduler starts ticking. Unlike start_hooks, this runs synchronously
    # here, at load time -- no running event loop is required or assumed.
    for obj in extensions_registry.values():
        if hasattr(obj, "on_bind"):
            obj.on_bind(system)

    return system
