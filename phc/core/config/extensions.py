"""Building the `extensions:` section: one configured instance per
`extensions.<name>.<instance>:` entry, each merged against its
extension.yaml and handed to that package's own configure().
"""

import importlib

from phc.core.config.descriptors import ExtensionDescriptor, _load_extension_descriptor
from phc.core.device import Device
from phc.core.errors import ConfigError
from phc.core.registry import extension_package


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
    instance's params are merged against phc/extensions/<name>/extension.yaml,
    then handed to that package's configure(params, flat, instance_key,
    extensions_registry) entry point (instance_key =
    "<extension_name>.<instance_name>", used both as the registry key below
    and as the sticky-log subscriber id -- see
    phc.core.endpoint.Endpoint.subscribe_log). extensions_registry is this same
    `registry` dict, passed by reference and still growing as later
    instances are configured -- an extension that only resolves a
    cross-extension reference lazily (e.g. at HTTP-request time, well after
    load_system() has returned, as phc.extensions.web_ui's graph panels do) sees
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
        module = importlib.import_module(f"{extension_package(ext_name)}.extension")
        configure = getattr(module, "configure", None)
        if configure is None:
            raise ConfigError(f"extension {ext_name!r} has no configure() entry point")
        for instance_name, instance_config in instances.items():
            key = f"{ext_name}.{instance_name}"
            params = _merge_extension_params(descriptor, instance_config or {}, key)
            registry[key] = configure(params, flat, key, registry)
    return registry
