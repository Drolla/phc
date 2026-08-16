"""Parameter resolution: merging a device instance's declared parameters
against its module, and the top-level `modules.<name>:` section that lets
every device of one module type share configuration.

Owns the scope (`module` vs `device`) and override (`allowed`/`required`/
`none`) rules, and the profile-library overlay a system config may add on
top of a module's own.
"""

import copy

from phc.core.config.descriptors import (ModuleDescriptor, _DEVICE_ENTRY_KEYS,
                                          _MODULES_ENTRY_KEYS, _parse_profile_library)
from phc.core.errors import ConfigError


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
