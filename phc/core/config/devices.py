"""Building the device tree: one `devices:` entry (and its children) into
a Device, pulling together the descriptor, parameter, and endpoint layers.
"""

from phc.core.config.descriptors import _DEVICE_ENTRY_KEYS, ModuleDescriptor, _load_module_descriptor
from phc.core.config.endpoints import _expand_endpoint_specs, _merge_endpoints, _resolve_interval
from phc.core.config.params import (_ModuleConfig, _build_effective_module, _merge_params,
                                     _resolve_module_config)
from phc.core.config.yamlio import _flatten_list_entries
from phc.core.device import Device
from phc.core.errors import ConfigError
from phc.core.registry import get_device_class


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
