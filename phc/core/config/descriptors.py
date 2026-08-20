"""Parsed module.yaml/extension.yaml descriptors, and the key sets that
define what a config entry may contain.

A device module and an extension are each a Python package plus a
declarative descriptor; this module is what reads those descriptors and
validates their internal consistency (profile libraries, reserved
parameter names). It is the bottom of this package's dependency order --
everything else builds on the descriptors it produces.
"""

import importlib.resources

import yaml

from phc.core.errors import ConfigError
from phc.core.registry import (_device_modules, _extension_packages,
                                extension_package, module_package)

# Descriptor lookup goes through importlib.resources, not a path derived
# from this file's own location: a module.yaml/extension.yaml is package
# DATA (declared in pyproject.toml's [tool.setuptools.package-data]), and
# resolving it relative to a source file only happens to work while the
# package sits in a source checkout. importlib.resources asks the import
# system where the package actually is, which keeps working for an
# installed wheel -- and for a device module living in someone else's
# distribution entirely, whose package the registry reports.


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
                        "write_transform", "history", "on_invalid"}


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
# endpoints, and phc/devices/zway/device.py's setup() documents that a
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


def _read_descriptor_yaml(package: str, name: str, filename: str, kind: str) -> dict:
    """Read and parse one packaged descriptor (a device module's
    module.yaml or an extension's extension.yaml) from `package`, located
    through the import system rather than the filesystem.

    `package` comes from the registry (see phc.core.registry's
    module_package()/extension_package()), i.e. from wherever the plugin's
    code actually lives -- so a module shipped by another distribution
    finds its descriptor next to its own code rather than under
    phc.devices.

    Raises ConfigError if the package or the descriptor within it doesn't
    exist. ModuleNotFoundError is translated rather than propagated: to a
    user, naming a module that doesn't exist is a config mistake like any
    other, not an import failure."""
    try:
        resource = importlib.resources.files(package) / filename
    except (ModuleNotFoundError, TypeError):
        raise ConfigError(f"{kind} {name!r} is not installed (no {package} package)") from None
    if not resource.is_file():
        raise ConfigError(f"{kind} {name!r} has no {filename} (looked in {package})")
    return yaml.safe_load(resource.read_text(encoding="utf-8")) or {}


def _load_module_descriptor(module_name: str) -> ModuleDescriptor:
    """Return the cached ModuleDescriptor for `module_name`, loading and
    parsing its module.yaml on first use.

    The package to read it from is whichever one registered the module's
    Device subclass, so a bundled module and a third-party one are handled
    identically. An unregistered name means no discovered plugin claims it
    -- reported as the config error it is, naming what is available."""
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
    its extension.yaml on first use. The package to read it from comes from
    the registry (see _load_module_descriptor for the same reasoning)."""
    if name in _extension_descriptors:
        return _extension_descriptors[name]
    try:
        package = extension_package(name)
    except KeyError:
        raise ConfigError(
            f"unknown extension {name!r}; discovered extensions: "
            f"{sorted(_extension_packages)}") from None
    raw = _read_descriptor_yaml(package, name, "extension.yaml", "extension")
    descriptor = ExtensionDescriptor(name, raw)
    _extension_descriptors[name] = descriptor
    return descriptor
