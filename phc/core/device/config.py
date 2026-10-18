"""Device config: the `modules:` and `devices:` sections into a Device tree.

Owns module.yaml descriptors (ModuleDescriptor), parameter scope/override
resolution including the top-level `modules.<name>:` section, and
building each `devices:` entry and its children. Endpoint specs are
handed to phc.core.endpoint.config.
"""

import copy

from phc.core.device import Device
from phc.core.endpoint.config import (
    _ENDPOINT_ENTRY_KEYS,
    _UNSET,
    _expand_endpoint_specs,
    _merge_endpoints,
    _resolve_interval,
)
from phc.core.errors import ConfigError
from phc.core.registry import _device_modules, _read_descriptor_yaml, get_device_class, module_package

# Top-level system YAML keys this module parses (see load_devices).
CONFIG_KEYS = ("modules", "devices")


# Metadata keys allowed on a device_profiles entry alongside its required
# `endpoints:` list -- brand/type/product/description are documentation only,
# never consumed at runtime (see ModuleDescriptor.device_profiles).
_DEVICE_PROFILE_KEYS = {"brand", "type", "product", "description", "endpoints"}


# Keys _build_device recognizes on a device entry, and modules.<name>:
# recognizes on a module entry -- `params:` is deliberately absent from
# both, the same choice already made for endpoint parameters above. A
# typo'd key here fails open elsewhere (e.g. a misspelled "device_profil:"
# silently yields a device with zero endpoints, see
# phc/devices/zway/device.py's setup()), so an unrecognized key is
# rejected here rather than silently ignored.
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
    """Parse+validate one endpoint_profiles/device_profiles mapping pair.

    Shared by ModuleDescriptor.__init__ (a module.yaml's own library) and
    _build_effective_module (a system YAML's modules.<name>.device_profiles/
    endpoint_profiles overlay). `owner_label` (e.g. "module 'zway'" or
    "modules.zway") only names the source in error messages.
    `extra_endpoint_specs` are endpoint specs validated alongside the two
    libraries but not returned -- only ModuleDescriptor passes its own
    unconditional `endpoints:` here, folding them into the same
    key-validation pass. Does not check name collisions against a second
    source, or the endpoints:/device_profiles mutual-exclusion rule --
    both are specific to which two sources are being combined and stay
    with the caller."""
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


def _check_no_reserved_names(module_name: str, entry_kind: str, names: set,
                              reserved: set, reserved_desc: str) -> None:
    """Raise ConfigError if any of `names` collides with `reserved`.

    Shared by ModuleDescriptor's parameters:/endpoint_parameters:
    collision checks."""
    collision = names & reserved
    if collision:
        raise ConfigError(
            f"module {module_name!r}: {entry_kind} name(s) {sorted(collision)} "
            f"collide with a reserved {reserved_desc}")


class ModuleDescriptor:
    """Parsed module.yaml for one device module.

    See docs/developer/writing-a-device-module.md for the full module.yaml
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
        # "params" is reserved too (see phc.core.endpoint.config._ENDPOINT_ENTRY_KEYS): a
        # param literally named "params" would be confusable with a nested
        # params: block, which this schema doesn't have.
        _check_no_reserved_names(
            name, "parameters", {p["name"] for p in self.parameters},
            _DEVICE_ENTRY_KEYS | _MODULES_ENTRY_KEYS | {"params"}, "device/modules entry key")
        self.endpoint_parameters = raw.get("endpoint_parameters") or []
        self.endpoint_param_names = {p["name"] for p in self.endpoint_parameters}
        _check_no_reserved_names(
            name, "endpoint_parameters", self.endpoint_param_names,
            _ENDPOINT_ENTRY_KEYS | {"params"}, "endpoint field name")
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
    """Return the cached ModuleDescriptor for `module_name`.

    Loads and parses its module.yaml on first use. The package to read it
    from is whichever one registered the module's Device subclass
    (bundled or third-party alike, see
    phc.core.registry._read_descriptor_yaml). An unregistered name means
    no discovered plugin claims it -- reported as the config error it is,
    naming what is available."""
    if module_name in _module_descriptors:
        return _module_descriptors[module_name]
    try:
        package = module_package(module_name)
    except KeyError:
        raise ConfigError(
            f"unknown device module {module_name!r}; discovered modules: "
            f"{sorted(_device_modules)}") from None
    raw = _read_descriptor_yaml(package, module_name, "module.yaml", "module")
    descriptor = ModuleDescriptor(module_name, raw)
    _module_descriptors[module_name] = descriptor
    return descriptor


class _ModuleConfig:
    """Resolved top-level `modules.<name>:` entry for one module type:

    - module_params: `scope: module` parameter values (one shared value for
      every device of this module type).
    - device_param_defaults: `scope: device` parameter values supplied
      directly under modules.<name>, which become a *default* for every
      device of this module, still overridable per device.
    - update: this module's default update interval (falls between a
      device's own `update:` and module.yaml's `update:`), or the _UNSET
      sentinel if modules.<name>.update was not set.

    Computed once per module name per load_system() call -- unlike
    _module_descriptors, this cannot be a cross-call global cache since
    modules_config is specific to the one system YAML being loaded."""

    def __init__(self, module_params: dict, device_param_defaults: dict, update=_UNSET):
        self.module_params = module_params
        self.device_param_defaults = device_param_defaults
        self.update = update


def _resolve_module_config(module: ModuleDescriptor, modules_config: dict) -> _ModuleConfig:
    """Resolve one module type's top-level `modules.<name>:` entry.

    See _ModuleConfig. `scope: module` params keep their original
    semantics exactly: settable only here, `override: none` rejects a
    value set here, `override: required` must be supplied here. For
    `scope: device` params (see _ModuleConfig.device_param_defaults),
    `override: required` is satisfied by a module-level value, and
    `override: none` still rejects one being set here at all. Raises
    ConfigError on an unrecognized parameter (i.e. not declared by the
    module at any scope)."""
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
    """Return the effective ModuleDescriptor for `_expand_endpoint_specs`.

    `module` itself, unless modules.<name> supplies its own
    device_profiles/endpoint_profiles, merged in on a COPY --
    module-scoped, so a system-supplied profile for module X stays
    invisible to any other module's devices. The common case (no
    system-level profiles) returns `module` unchanged, no copy.

    Never mutates `module` in place: it is the process-global object
    cached by _load_module_descriptor, shared across every load_system()
    call, so mutating it would leak one system config's profiles into
    another's use of the same module.

    A system-supplied profile name colliding with one module.yaml already
    declares is a ConfigError, not a silent override -- as is a
    system-supplied device_profiles against the module's own non-empty
    `endpoints:` (same base/overlay ambiguity ModuleDescriptor.__init__
    already rejects for module.yaml alone)."""
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


def _merge_params(module: ModuleDescriptor, instance_params: dict, device_id: str,
                   resolved_module_params: dict | None = None,
                   module_param_defaults: dict | None = None) -> dict:
    """Merge one device instance's params against its module's declared ones.

    Instance params are already separated from the entry's other keys by
    the caller (see _build_device). `scope: module` params come from the
    already-resolved `resolved_module_params` (_ModuleConfig.module_params).
    `scope: device` params are resolved device-entry field ->
    `module_param_defaults` (_ModuleConfig.device_param_defaults -- never
    popped here, unlike instance_params, since it is shared/cached across
    every device of this module) -> module.yaml's own `default:`, with
    `override: required` satisfied by either the device or the
    module-level default. Raises ConfigError on an unrecognized,
    missing-but-required, or not-overridable parameter."""
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


def _build_device(entry: dict, intervals_map: dict, parent_qualified_id: str | None,
                   flat: dict[str, Device], modules_config: dict,
                   module_config_cache: dict[str, "_ModuleConfig"],
                   effective_module_cache: dict[str, ModuleDescriptor],
                   context: dict | None = None) -> Device:
    """Recursively build one `devices:` YAML entry into a Device tree.

    Registers every device (and its children) by qualified id in `flat`
    as it goes. Raises ConfigError on a duplicate qualified id or an
    unrecognized entry key. Which keys count as "device entry keys" vs.
    "params" depends on the module (a declared parameter is an ordinary
    top-level field, see ModuleDescriptor), so `entry`'s keys can't be
    validated until the module is loaded -- _merge_params raises on
    whatever's left in `instance_params` once its own declared names are
    picked out.

    `context` is the one scratch dict shared by every device of this system
    (see Device.context) -- created once per load_system() call and passed
    down unchanged, including into children."""
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
                      module_config_cache, effective_module_cache, context)
        for child_entry in entry.get("children") or []
    ]

    device = device_cls(
        device_id,
        name=entry.get("name", ""),
        params=params,
        endpoints=endpoints,
        children=children,
        update_interval=update_interval,
        parent_qualified_id=parent_qualified_id,
        context=context,
    )

    for ep, default_value in seeds:
        ep.set(default_value)
        ep.update_state()

    if qualified_id in flat:
        raise ConfigError(f"duplicate device id: {qualified_id!r}")
    flat[qualified_id] = device

    return device


def load_devices(raw: dict, intervals_map: dict) -> tuple[list[Device], dict[str, Device]]:
    """Build the `modules:`/`devices:` sections into (roots, flat).

    `flat` indexes every device, children included, by qualified id."""
    modules_config = raw.get("modules") or {}
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

    # One scratch dict per loaded system, handed to every device built from
    # it (see Device.context): where a module keeps state shared between its
    # own instances, scoped to this System rather than to the process.
    device_context: dict = {}
    roots = [
        _build_device(entry, intervals_map, None, flat, modules_config, module_config_cache,
                      effective_module_cache, device_context)
        for entry in raw.get("devices") or []
    ]
    return roots, flat
