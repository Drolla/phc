"""Tests for core.config: system YAML loading, param/endpoint merging, and validation."""

import logging
from pathlib import Path

import pytest

from core.config import (ConfigError, ExtensionDescriptor, ModuleDescriptor, _build_effective_module,
                          _collect_history_records, _expand_endpoint_specs, _load_extensions,
                          _make_history_tick_hook, _merge_endpoints, _merge_extension_params,
                          _merge_params, _parse_history_spec, _resolve_interval,
                          _resolve_module_config, _substitute_endpoint_spec, load_system)
from core.device import Device
from core.endpoint import Endpoint

_EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def test_merge_params_uses_default_when_no_override():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "station", "override": "allowed", "default": "BER"},
        ]
    })
    merged = _merge_params(module, {}, "dev")
    assert merged == {"station": "BER"}


def test_merge_params_instance_overrides_allowed():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "station", "override": "allowed", "default": "BER"},
        ]
    })
    merged = _merge_params(module, {"station": "ZRH"}, "dev")
    assert merged == {"station": "ZRH"}


def test_merge_params_none_override_rejects_instance_value():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "data_url", "override": "none", "default": "http://x"},
        ]
    })
    with pytest.raises(ConfigError):
        _merge_params(module, {"data_url": "http://evil"}, "dev")


def test_merge_params_required_missing_raises():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "station", "override": "required"},
        ]
    })
    with pytest.raises(ConfigError):
        _merge_params(module, {}, "dev")


def test_merge_params_required_supplied_ok():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "station", "override": "required"},
        ]
    })
    merged = _merge_params(module, {"station": "BER"}, "dev")
    assert merged == {"station": "BER"}


def test_merge_params_unknown_param_raises():
    module = ModuleDescriptor("m", {"parameters": []})
    with pytest.raises(ConfigError):
        _merge_params(module, {"bogus": 1}, "dev")


def test_merge_params_module_scope_uses_resolved_value():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "cache_time", "override": "allowed", "scope": "module", "default": "10m"},
        ]
    })
    merged = _merge_params(module, {}, "dev", resolved_module_params={"cache_time": "5m"})
    assert merged == {"cache_time": "5m"}


def test_merge_params_module_scope_falls_back_to_default_when_not_in_resolved_dict():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "cache_time", "override": "allowed", "scope": "module", "default": "10m"},
        ]
    })
    merged = _merge_params(module, {}, "dev", resolved_module_params={})
    assert merged == {"cache_time": "10m"}


def test_merge_params_module_scope_rejects_device_level_value():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "cache_time", "override": "allowed", "scope": "module", "default": "10m"},
        ]
    })
    with pytest.raises(ConfigError):
        _merge_params(module, {"cache_time": "1m"}, "dev", resolved_module_params={"cache_time": "5m"})


def test_resolve_module_config_uses_default_when_absent():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "cache_time", "override": "allowed", "scope": "module", "default": "10m"},
        ]
    })
    assert _resolve_module_config(module, {}).module_params == {"cache_time": "10m"}


def test_resolve_module_config_supplied_value_used():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "cache_time", "override": "allowed", "scope": "module", "default": "10m"},
        ]
    })
    modules_config = {"m": {"cache_time": "5m"}}
    assert _resolve_module_config(module, modules_config).module_params == {"cache_time": "5m"}


def test_resolve_module_config_required_missing_raises():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "region", "override": "required", "scope": "module"},
        ]
    })
    with pytest.raises(ConfigError):
        _resolve_module_config(module, {})


def test_resolve_module_config_required_supplied_ok():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "region", "override": "required", "scope": "module"},
        ]
    })
    modules_config = {"m": {"region": "CH"}}
    assert _resolve_module_config(module, modules_config).module_params == {"region": "CH"}


def test_resolve_module_config_none_override_rejects_supplied_value():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "data_url", "override": "none", "scope": "module", "default": "http://x"},
        ]
    })
    modules_config = {"m": {"data_url": "http://evil"}}
    with pytest.raises(ConfigError):
        _resolve_module_config(module, modules_config)


def test_resolve_module_config_unknown_param_raises():
    module = ModuleDescriptor("m", {"parameters": []})
    modules_config = {"m": {"bogus": 1}}
    with pytest.raises(ConfigError):
        _resolve_module_config(module, modules_config)


def test_resolve_module_config_device_scope_param_becomes_default():
    # Inverted from the old scheme: a device-scoped param set under
    # modules.<name>.params is no longer rejected -- it becomes a per-module
    # default (device_param_defaults), still overridable per device.
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "station", "override": "allowed", "scope": "device", "default": "BER"},
        ]
    })
    modules_config = {"m": {"station": "ZRH"}}
    config = _resolve_module_config(module, modules_config)
    assert config.module_params == {}
    assert config.device_param_defaults == {"station": "ZRH"}


def test_resolve_module_config_device_scope_none_override_still_rejects_module_level_value():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "station", "override": "none", "scope": "device", "default": "BER"},
        ]
    })
    modules_config = {"m": {"station": "ZRH"}}
    with pytest.raises(ConfigError):
        _resolve_module_config(module, modules_config)


def test_resolve_module_config_device_scope_required_satisfied_by_module_level_value():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "base_url", "override": "required", "scope": "device"},
        ]
    })
    modules_config = {"m": {"base_url": "http://host"}}
    config = _resolve_module_config(module, modules_config)
    assert config.device_param_defaults == {"base_url": "http://host"}


def test_resolve_module_config_ignores_unrelated_modules_config_when_no_module_scope_params():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "station", "override": "required", "scope": "device"},
        ]
    })
    config = _resolve_module_config(module, {"other": {"x": 1}})
    assert config.module_params == {}
    assert config.device_param_defaults == {}


def test_merge_endpoints_defaults_log_aggregation_to_max():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "state"}],
    })
    endpoints, _ = _merge_endpoints(module, [], "dev", {})
    assert endpoints[0].log_aggregation == "max"


def test_merge_endpoints_accepts_explicit_log_aggregation():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "state", "log_aggregation": "min"}],
    })
    endpoints, _ = _merge_endpoints(module, [], "dev", {})
    assert endpoints[0].log_aggregation == "min"


def test_merge_endpoints_rejects_invalid_log_aggregation():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "state", "log_aggregation": "bogus"}],
    })
    with pytest.raises(ConfigError):
        _merge_endpoints(module, [], "dev", {})


def test_merge_endpoints_static_module_with_instance_default_override():
    module = ModuleDescriptor("m", {
        "endpoints": [
            {"key": "temperature", "readable": True, "writable": False},
        ]
    })
    endpoints, seeds = _merge_endpoints(module, [], "dev", {})
    assert len(endpoints) == 1
    assert endpoints[0].key == "temperature"
    assert endpoints[0].writable is False
    assert seeds == []


def test_merge_endpoints_dynamic_module_instance_adds_new_keys():
    module = ModuleDescriptor("m", {"endpoints": []})
    endpoints, seeds = _merge_endpoints(
        module,
        [{"key": "power", "writable": True, "default": "off"},
         {"key": "brightness", "writable": True, "default": 0}],
        "dev", {},
    )
    keys = {e.key for e in endpoints}
    assert keys == {"power", "brightness"}
    assert len(seeds) == 2


def test_merge_endpoints_instance_overlay_replaces_only_the_given_key():
    # A declared endpoint parameter is an ordinary top-level field, so
    # overlaying one instance-level field (scale) leaves a sibling
    # (column) untouched -- see _overlay_endpoint_spec.
    module = ModuleDescriptor("m", {
        "endpoint_parameters": [{"name": "column"}, {"name": "scale"}],
        "endpoints": [
            {"key": "temperature", "column": "tre200s0", "scale": "C"},
        ]
    })
    endpoints, _ = _merge_endpoints(
        module, [{"key": "temperature", "scale": "F"}], "dev", {},
    )
    assert endpoints[0].params == {"column": "tre200s0", "scale": "F"}


def test_merge_endpoints_declared_endpoint_param_lands_in_params():
    module = ModuleDescriptor("m", {
        "endpoint_parameters": [{"name": "command_group"}, {"name": "address"}],
        "endpoints": [],
    })
    endpoints, _ = _merge_endpoints(
        module, [{"key": "state", "command_group": "SwitchBinary", "address": "7.1"}], "dev", {},
    )
    assert endpoints[0].params == {"command_group": "SwitchBinary", "address": "7.1"}


def test_merge_endpoints_rejects_params_key_on_endpoint():
    # params: nesting was replaced by declared top-level endpoint
    # parameters -- params: itself is no longer a recognized endpoint key.
    module = ModuleDescriptor("m", {
        "endpoint_parameters": [{"name": "command_group"}],
        "endpoints": [],
    })
    with pytest.raises(ConfigError):
        _merge_endpoints(module, [{"key": "state", "params": {"command_group": "SwitchBinary"}}],
                          "dev", {})


def test_module_descriptor_rejects_endpoint_parameters_colliding_with_core_field():
    with pytest.raises(ConfigError):
        ModuleDescriptor("m", {"endpoint_parameters": [{"name": "format"}]})


def test_module_descriptor_rejects_endpoint_parameters_named_params():
    with pytest.raises(ConfigError):
        ModuleDescriptor("m", {"endpoint_parameters": [{"name": "params"}]})


def test_merge_endpoints_parses_type_unit_values():
    module = ModuleDescriptor("m", {
        "endpoints": [
            {"key": "state", "type": "int", "unit": "%", "values": {0: "off", 1: "on"}},
        ]
    })
    endpoints, _ = _merge_endpoints(module, [], "dev", {})
    assert endpoints[0].value_type == "int"
    assert endpoints[0].unit == "%"
    assert endpoints[0].values == {0: "off", 1: "on"}


def test_merge_endpoints_passes_through_explicit_format():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "temperature", "type": "float", "format": ".2f"}],
    })
    endpoints, _ = _merge_endpoints(module, [], "dev", {})
    assert endpoints[0].format == ".2f"


def test_merge_endpoints_float_endpoint_defaults_format_to_one_decimal():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "temperature", "type": "float"}],
    })
    endpoints, _ = _merge_endpoints(module, [], "dev", {})
    assert endpoints[0].format == ".1f"


def test_merge_endpoints_rejects_invalid_type():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "state", "type": "bogus"}],
    })
    with pytest.raises(ConfigError):
        _merge_endpoints(module, [], "dev", {})


def test_merge_endpoints_parses_min_max_from_module():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "brightness", "type": "int", "min": 0, "max": 100}],
    })
    endpoints, _ = _merge_endpoints(module, [], "dev", {})
    assert endpoints[0].min == 0
    assert endpoints[0].max == 100


def test_merge_endpoints_instance_overrides_module_min_max():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "brightness", "type": "int", "min": 0, "max": 100}],
    })
    endpoints, _ = _merge_endpoints(
        module, [{"key": "brightness", "max": 50}], "dev", {},
    )
    assert endpoints[0].min == 0
    assert endpoints[0].max == 50


def test_merge_endpoints_rejects_min_greater_than_max():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "brightness", "type": "int", "min": 100, "max": 0}],
    })
    with pytest.raises(ConfigError):
        _merge_endpoints(module, [], "dev", {})


# ---------- read_transform / write_transform ----------

def test_merge_endpoints_parses_read_and_write_transform():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "temp", "type": "float", "read_transform": "value - 1.5",
                       "write_transform": "value + 1.5"}],
    })
    endpoints, _ = _merge_endpoints(module, [], "dev", {})
    ep = endpoints[0]
    ep.set_raw(20.0)
    ep.update_state()
    assert ep.get() == 18.5
    assert ep.to_raw(18.5) == 20.0


def test_merge_endpoints_rejects_invalid_read_transform():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "temp", "type": "float", "read_transform": "value +"}],
    })
    with pytest.raises(ConfigError):
        _merge_endpoints(module, [], "dev", {})


def test_merge_endpoints_rejects_invalid_write_transform():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "temp", "type": "float", "write_transform": "__import__('os')"}],
    })
    with pytest.raises(ConfigError):
        _merge_endpoints(module, [], "dev", {})


def test_merge_endpoints_instance_overrides_module_read_transform():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "temp", "type": "float", "read_transform": "value - 1.5"}],
    })
    endpoints, _ = _merge_endpoints(
        module, [{"key": "temp", "read_transform": "value - 2.0"}], "dev", {},
    )
    endpoints[0].set_raw(20.0)
    endpoints[0].update_state()
    assert endpoints[0].get() == 18.0


# ---------- {param} template substitution (_substitute_endpoint_spec) ----------

def test_substitute_endpoint_spec_fills_params_template():
    spec = {"key": "temp", "params": {"command_group": "SensorMultilevel", "address": "{node}.0.1"}}
    substituted = _substitute_endpoint_spec(spec, {"node": 11}, "dev")
    assert substituted["params"]["address"] == "11.0.1"


def test_substitute_endpoint_spec_supports_multiple_param_names_in_one_field():
    spec = {"key": "state", "params": {"address": "{node}.{channel}"}}
    substituted = _substitute_endpoint_spec(spec, {"node": 7, "channel": 2}, "dev")
    assert substituted["params"]["address"] == "7.2"


def test_substitute_endpoint_spec_substitutes_fields_outside_params():
    spec = {"key": "temp", "description": "Node {node} temperature", "unit": "deg{node}"}
    substituted = _substitute_endpoint_spec(spec, {"node": 11}, "dev")
    assert substituted["description"] == "Node 11 temperature"
    assert substituted["unit"] == "deg11"


def test_substitute_endpoint_spec_leaves_excluded_fields_untouched():
    # key/kind/type/log_aggregation/device_profile/endpoint_profile are
    # structural metadata, never templated, even if string-valued.
    spec = {"key": "{node}", "kind": "generic", "type": "float", "log_aggregation": "max"}
    substituted = _substitute_endpoint_spec(spec, {}, "dev")
    assert substituted["key"] == "{node}"
    assert substituted["kind"] == "generic"


def test_substitute_endpoint_spec_rejects_missing_param():
    spec = {"key": "temp", "params": {"address": "{node}.0.1"}}
    with pytest.raises(ConfigError):
        _substitute_endpoint_spec(spec, {"node": None}, "dev")


def test_substitute_endpoint_spec_passes_through_non_string_values():
    spec = {"key": "brightness", "min": 0, "max": 100, "writable": True, "values": None}
    substituted = _substitute_endpoint_spec(spec, {}, "dev")
    assert substituted["min"] == 0
    assert substituted["max"] == 100
    assert substituted["writable"] is True
    assert substituted["values"] is None


def test_merge_endpoints_substitutes_templates_on_hand_written_endpoint():
    # Templating is not tied to device_profile:/endpoint_profile: usage -- a
    # fully hand-written endpoint (no profile anywhere) still gets its
    # {param} templates resolved from the device's own params.
    module = ModuleDescriptor("m", {"endpoint_parameters": [{"name": "column"}], "endpoints": []})
    endpoints, _ = _merge_endpoints(
        module, [{"key": "temperature", "column": "col_{station}"}], "dev",
        {"station": "BER"},
    )
    assert endpoints[0].params["column"] == "col_BER"


def test_merge_endpoints_substitutes_templates_on_any_modules_own_endpoints():
    # No special-casing for zway -- any module's unconditional endpoints:
    # get the same treatment.
    module = ModuleDescriptor("meteoswisslike", {
        "endpoint_parameters": [{"name": "column"}],
        "endpoints": [{"key": "temperature", "column": "{station}_temp"}],
    })
    endpoints, _ = _merge_endpoints(module, [], "dev", {"station": "ZRH"})
    assert endpoints[0].params["column"] == "ZRH_temp"


def test_merge_endpoints_substitutes_description_field():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "temp", "description": "Sensor at node {node}"}],
    })
    endpoints, _ = _merge_endpoints(module, [], "dev", {"node": 11})
    assert endpoints[0].description == "Sensor at node 11"


def test_merge_endpoints_rejects_unset_template_param():
    module = ModuleDescriptor("m", {
        "endpoint_parameters": [{"name": "address"}],
        "endpoints": [{"key": "temp", "address": "{node}.0.1"}],
    })
    with pytest.raises(ConfigError):
        _merge_endpoints(module, [], "dev", {"node": None})


# ---------- endpoint/device profiles ----------
#
# _expand_endpoint_specs only resolves *which* endpoints exist and how they
# overlay (by key) -- {param} templates are substituted later, by
# _merge_endpoints/_substitute_endpoint_spec (see the tests above), so specs
# returned here still carry their raw, unsubstituted templates. An
# endpoint_profile never carries `address` (that's a product's wiring, not
# the command group's) -- only a device_profiles entry's own `endpoints:`
# does, which is why _profile_module's "temperature"/"battery" profiles
# below have no address at all.

def _profile_module():
    return ModuleDescriptor("zwaylike", {
        "endpoint_parameters": [
            {"name": "command_group"},
            {"name": "address"},
        ],
        "endpoints": [],
        "endpoint_profiles": {
            "temperature": {"type": "float", "unit": "°C", "command_group": "SensorMultilevel"},
            "battery": {"type": "int", "unit": "%", "command_group": "Battery"},
        },
        "device_profiles": {
            "multisensor_t": {
                "brand": "Everspring",
                "endpoints": [
                    {"key": "temp", "endpoint_profile": "temperature", "address": "{node}.0.1"},
                    {"key": "battery", "endpoint_profile": "battery", "address": "{node}"},
                ],
            },
        },
    })


def test_module_descriptor_rejects_device_profiles_with_nonempty_endpoints():
    with pytest.raises(ConfigError):
        ModuleDescriptor("m", {
            "endpoints": [{"key": "state"}],
            "device_profiles": {"x": {"endpoints": [{"key": "state", "endpoint_profile": "y"}]}},
        })


def test_module_descriptor_rejects_device_profile_missing_endpoints_key():
    with pytest.raises(ConfigError):
        ModuleDescriptor("m", {"device_profiles": {"x": {"brand": "Acme"}}})


def test_module_descriptor_rejects_device_profile_unknown_metadata_key():
    with pytest.raises(ConfigError):
        ModuleDescriptor("m", {
            "device_profiles": {"x": {"endpoints": [], "bogus": "y"}},
        })


def test_module_descriptor_rejects_device_profile_old_list_shape():
    # device_profiles used to be a bare list of endpoint specs -- that shape
    # is no longer accepted, with a message pointing at the new one.
    with pytest.raises(ConfigError):
        ModuleDescriptor("m", {"device_profiles": {"x": [{"key": "state"}]}})


def test_expand_endpoint_specs_is_identity_when_no_profile_anywhere():
    module = _profile_module()
    entry = {"id": "dev", "module": "zwaylike",
             "endpoints": [{"key": "state", "command_group": "SwitchBinary", "address": "7.1"}]}
    specs = _expand_endpoint_specs(module, entry, "dev")
    assert specs is entry["endpoints"]


def test_expand_endpoint_specs_device_profile_expands_by_key():
    module = _profile_module()
    entry = {"id": "multi_liv", "module": "zwaylike", "device_profile": "multisensor_t"}
    specs = _expand_endpoint_specs(module, entry, "multi_liv")
    by_key = {s["key"]: s for s in specs}
    assert by_key["temp"]["command_group"] == "SensorMultilevel"
    assert by_key["temp"]["address"] == "{node}.0.1"
    assert by_key["battery"]["command_group"] == "Battery"
    assert by_key["battery"]["address"] == "{node}"


def test_expand_endpoint_specs_device_endpoints_overlay_by_key_preserves_command_group():
    # A device's own endpoints: tweaking one profile-derived field must not
    # clobber a sibling like command_group -- see _overlay_endpoint_spec.
    module = _profile_module()
    entry = {"id": "sirene", "module": "zwaylike", "device_profile": "multisensor_t",
             "endpoints": [{"key": "battery", "address": "16.0"}]}
    specs = _expand_endpoint_specs(module, entry, "sirene")
    battery = next(s for s in specs if s["key"] == "battery")
    assert battery["command_group"] == "Battery"
    assert battery["address"] == "16.0"


def test_expand_endpoint_specs_single_endpoint_profile_without_device_profile():
    # An endpoint_profile never carries an address, so a single-endpoint
    # use (no device_profile:) must still supply its own.
    module = _profile_module()
    entry = {"id": "fus18_meteo", "module": "zwaylike",
             "endpoints": [{"key": "f18_temp", "endpoint_profile": "temperature",
                            "address": "{node}.0.1"}]}
    specs = _expand_endpoint_specs(module, entry, "fus18_meteo")
    assert specs[0]["command_group"] == "SensorMultilevel"
    assert specs[0]["address"] == "{node}.0.1"


def test_expand_endpoint_specs_rejects_unknown_device_profile():
    module = _profile_module()
    entry = {"id": "dev", "module": "zwaylike", "device_profile": "bogus"}
    with pytest.raises(ConfigError):
        _expand_endpoint_specs(module, entry, "dev")


def test_expand_endpoint_specs_rejects_unknown_endpoint_profile():
    module = _profile_module()
    entry = {"id": "dev", "module": "zwaylike", "endpoints": [{"key": "x", "endpoint_profile": "bogus"}]}
    with pytest.raises(ConfigError):
        _expand_endpoint_specs(module, entry, "dev")


def test_expand_endpoint_specs_does_not_mutate_cached_module_profiles():
    # module.endpoint_profiles/device_profiles are process-global-cached
    # descriptors (_module_descriptors) -- expanding a profile for one
    # device must not leave a later mutation of the returned dicts visible
    # in the cache (or in another device's independently-expanded specs).
    module = _profile_module()
    entry = {"id": "a", "module": "zwaylike", "device_profile": "multisensor_t"}
    specs = _expand_endpoint_specs(module, entry, "a")
    by_key = {s["key"]: s for s in specs}
    by_key["temp"]["address"] = "MUTATED"
    assert module.device_profiles["multisensor_t"]["endpoints"][0]["address"] == "{node}.0.1"
    specs2 = _expand_endpoint_specs(module, entry, "b")
    by_key2 = {s["key"]: s for s in specs2}
    assert by_key2["temp"]["address"] == "{node}.0.1"


def test_expand_and_substitute_device_profile_end_to_end():
    # The two-step pipeline together: _expand_endpoint_specs resolves which
    # endpoints exist (still templated), _substitute_endpoint_spec (as
    # _merge_endpoints calls it) fills in the device's own node.
    module = _profile_module()
    entry = {"id": "multi_liv", "module": "zwaylike", "device_profile": "multisensor_t"}
    specs = _expand_endpoint_specs(module, entry, "multi_liv")
    substituted = [_substitute_endpoint_spec(s, {"node": 11}, "multi_liv") for s in specs]
    by_key = {s["key"]: s for s in substituted}
    assert by_key["temp"]["command_group"] == "SensorMultilevel"
    assert by_key["temp"]["address"] == "11.0.1"
    assert by_key["battery"]["command_group"] == "Battery"
    assert by_key["battery"]["address"] == "11"


def test_build_effective_module_is_identity_when_no_system_profiles():
    # The common case (no modules.<name>.device_profiles/endpoint_profiles
    # at all) must return `module` itself unchanged -- no copy, no cost.
    module = _profile_module()
    assert _build_effective_module(module, {}) is module
    assert _build_effective_module(module, {"zwaylike": {"update": "5s"}}) is module


def test_build_effective_module_merges_system_device_profile():
    module = ModuleDescriptor("virtual", {"endpoints": []})
    modules_config = {"virtual": {"device_profiles": {
        "siren": {"endpoints": [{"key": "state", "writable": True, "type": "int",
                                 "values": {0: "off", 1: "on"}, "default": 0}]},
    }}}
    effective = _build_effective_module(module, modules_config)
    assert effective.name == module.name
    assert "siren" in effective.device_profiles
    assert effective.device_profiles["siren"]["endpoints"][0]["key"] == "state"


def test_build_effective_module_does_not_mutate_cached_module_descriptor():
    # module is the process-global _module_descriptors-cached object --
    # merging a system profile onto the effective view must never touch it,
    # or one system config's profiles would leak into another's use of the
    # same module (see _expand_endpoint_specs_does_not_mutate_cached_module_
    # profiles for the equivalent discipline on the module.yaml-only path).
    module = ModuleDescriptor("virtual", {"endpoints": []})
    modules_config = {"virtual": {"device_profiles": {
        "siren": {"endpoints": [{"key": "state"}]},
    }}}
    _build_effective_module(module, modules_config)
    assert module.device_profiles == {}
    assert module.endpoint_profiles == {}


def test_build_effective_module_rejects_device_profile_name_collision():
    module = _profile_module()
    modules_config = {"zwaylike": {"device_profiles": {
        "multisensor_t": {"endpoints": [{"key": "x"}]},
    }}}
    with pytest.raises(ConfigError):
        _build_effective_module(module, modules_config)


def test_build_effective_module_rejects_endpoint_profile_name_collision():
    module = _profile_module()
    modules_config = {"zwaylike": {"endpoint_profiles": {
        "temperature": {"type": "float"},
    }}}
    with pytest.raises(ConfigError):
        _build_effective_module(module, modules_config)


def test_build_effective_module_rejects_device_profiles_combined_with_nonempty_module_endpoints():
    module = ModuleDescriptor("m", {"endpoints": [{"key": "x"}]})
    modules_config = {"m": {"device_profiles": {"y": {"endpoints": [{"key": "z"}]}}}}
    with pytest.raises(ConfigError):
        _build_effective_module(module, modules_config)


def test_build_effective_module_validates_endpoint_spec_keys_of_system_profile():
    # Proves the shared _parse_profile_library validation is actually wired
    # in for the system-config path, not just device_profiles' own shape.
    module = ModuleDescriptor("virtual", {"endpoints": []})
    modules_config = {"virtual": {"device_profiles": {
        "siren": {"endpoints": [{"key": "state", "bogus_field": 1}]},
    }}}
    with pytest.raises(ConfigError):
        _build_effective_module(module, modules_config)


def test_resolve_interval_module_default_when_no_instance_override():
    module = ModuleDescriptor("m", {"update": "10s"})
    seconds = _resolve_interval(module, {}, {})
    assert seconds == 10.0


def test_resolve_interval_named_group():
    module = ModuleDescriptor("m", {"update": "10s"})
    seconds = _resolve_interval(module, {"update": "fast"}, {"fast": "3s"})
    assert seconds == 3.0


def test_resolve_interval_literal_override_bypasses_named_map():
    module = ModuleDescriptor("m", {"update": "10s"})
    seconds = _resolve_interval(module, {"update": "5s"}, {"fast": "3s"})
    assert seconds == 5.0


def test_resolve_interval_host_default_null_stays_none():
    module = ModuleDescriptor("m", {"update": None})
    seconds = _resolve_interval(module, {}, {})
    assert seconds is None


# ---------- history: _parse_history_spec ----------

def test_parse_history_spec_shorthand_sets_size():
    size, interval = _parse_history_spec(8, {}, "dev", "temp")
    assert (size, interval) == (8, None)


def test_parse_history_spec_mapping_parses_interval():
    size, interval = _parse_history_spec({"size": 8, "interval": "5m"}, {}, "dev", "temp")
    assert (size, interval) == (8, 300.0)


def test_parse_history_spec_interval_accepts_named_interval():
    size, interval = _parse_history_spec(
        {"size": 4, "interval": "radon"}, {"radon": "1m"}, "dev", "temp")
    assert (size, interval) == (4, 60.0)


def test_parse_history_spec_rejects_unknown_mapping_key():
    with pytest.raises(ConfigError):
        _parse_history_spec({"size": 4, "bogus": 1}, {}, "dev", "temp")


def test_parse_history_spec_rejects_missing_size():
    with pytest.raises(ConfigError):
        _parse_history_spec({"interval": "5m"}, {}, "dev", "temp")


@pytest.mark.parametrize("bad_size", [8.5, "8", True])
def test_parse_history_spec_rejects_non_integer_size(bad_size):
    with pytest.raises(ConfigError):
        _parse_history_spec(bad_size, {}, "dev", "temp")


@pytest.mark.parametrize("bad_size", [0, -1])
def test_parse_history_spec_rejects_zero_or_negative_size(bad_size):
    with pytest.raises(ConfigError):
        _parse_history_spec(bad_size, {}, "dev", "temp")


def test_parse_history_spec_rejects_malformed_interval():
    with pytest.raises(ConfigError):
        _parse_history_spec({"size": 4, "interval": "not-a-duration"}, {}, "dev", "temp")


# ---------- history: _merge_endpoints wiring ----------

def test_merge_endpoints_history_shorthand_sets_size():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "temp", "type": "float", "history": 8}],
    })
    endpoints, _ = _merge_endpoints(module, [], "dev", {})
    assert endpoints[0].history_size == 8
    assert endpoints[0].history_interval is None


def test_merge_endpoints_history_mapping_parses_interval():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "temp", "type": "float",
                       "history": {"size": 8, "interval": "5m"}}],
    })
    endpoints, _ = _merge_endpoints(module, [], "dev", {})
    assert endpoints[0].history_size == 8
    assert endpoints[0].history_interval == 300.0


def test_merge_endpoints_history_interval_uses_intervals_map():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "temp", "type": "float",
                       "history": {"size": 4, "interval": "radon"}}],
    })
    endpoints, _ = _merge_endpoints(module, [], "dev", {}, {"radon": "1m"})
    assert endpoints[0].history_interval == 60.0


def test_merge_endpoints_history_rejects_string_typed_endpoint():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "label", "type": "str", "history": 4}],
    })
    with pytest.raises(ConfigError):
        _merge_endpoints(module, [], "dev", {})


def test_merge_endpoints_history_allows_untyped_endpoint():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "temp", "history": 4}],
    })
    endpoints, _ = _merge_endpoints(module, [], "dev", {})
    assert endpoints[0].history_size == 4


def test_merge_endpoints_history_rejects_invalid_size():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "temp", "type": "float", "history": 0}],
    })
    with pytest.raises(ConfigError):
        _merge_endpoints(module, [], "dev", {})


def test_endpoint_entry_keys_accepts_history():
    # Complements test_merge_endpoints_rejects_unknown_endpoint_entry_key --
    # 'history' must NOT be rejected as an unrecognized endpoint entry key.
    module = ModuleDescriptor("m", {"endpoints": []})
    endpoints, _ = _merge_endpoints(module, [{"key": "temp", "history": 4}], "dev", {})
    assert endpoints[0].history_size == 4


def test_instance_endpoint_overlay_adds_history_to_module_endpoint():
    # The exact _overlay_endpoint_spec mechanism the fusion18 radon config
    # relies on: an instance-level `history:` overlay must add history
    # without disturbing the module-declared endpoint's other fields.
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "temperature", "type": "float", "column": "x"}],
        "endpoint_parameters": [{"name": "column"}],
    })
    endpoints, _ = _merge_endpoints(
        module, [{"key": "temperature", "history": 8}], "dev", {})
    ep = endpoints[0]
    assert ep.history_size == 8
    assert ep.params == {"column": "x"}


# ---------- history: _collect_history_records / _make_history_tick_hook ----------

def test_collect_history_records_defaults_interval_to_device_update():
    device = Device("d", endpoints=[Endpoint("temp", value_type="float", history=4)],
                     update_interval=30.0)
    records = _collect_history_records({"d": device})
    assert len(records) == 1
    assert records[0].interval == 30.0


def test_collect_history_records_explicit_interval_overrides_device_update():
    device = Device("d", endpoints=[
        Endpoint("temp", value_type="float", history=4, history_interval=300.0)],
        update_interval=30.0)
    records = _collect_history_records({"d": device})
    assert records[0].interval == 300.0


def test_collect_history_records_raises_for_unpolled_device_without_interval():
    device = Device("d", endpoints=[Endpoint("temp", value_type="float", history=4)],
                     update_interval=None)
    with pytest.raises(ConfigError):
        _collect_history_records({"d": device})


def test_collect_history_records_ignores_endpoints_without_history():
    device = Device("d", endpoints=[Endpoint("temp", value_type="float")],
                     update_interval=30.0)
    assert _collect_history_records({"d": device}) == []


def test_history_tick_hook_samples_only_once_per_interval(monkeypatch):
    device = Device("d", endpoints=[
        Endpoint("temp", value_type="float", history=4, history_interval=10.0)],
        update_interval=30.0)
    ep = device.endpoint("temp")
    ep.set(1.0)
    ep.update_state()
    records = _collect_history_records({"d": device})
    hook = _make_history_tick_hook(records)

    now = [1000.0]
    monkeypatch.setattr("core.config.time.time", lambda: now[0])

    hook({"d": device})  # next_due starts at 0.0 -> immediately due
    assert ep.get_history() == [1.0]

    ep.set(2.0)
    ep.update_state()
    hook({"d": device})  # < 10s since last sample -> not due yet
    assert ep.get_history() == [1.0]

    now[0] += 10.0
    hook({"d": device})
    assert ep.get_history() == [1.0, 2.0]


def test_history_tick_hook_retries_after_a_skipped_none_sample(monkeypatch):
    device = Device("d", endpoints=[
        Endpoint("temp", value_type="float", history=4, history_interval=10.0)],
        update_interval=30.0)
    ep = device.endpoint("temp")
    records = _collect_history_records({"d": device})
    hook = _make_history_tick_hook(records)

    now = [1000.0]
    monkeypatch.setattr("core.config.time.time", lambda: now[0])

    hook({"d": device})  # ep.get() is still None -> record_history() is False
    assert ep.get_history() == []

    now[0] += 1.0
    ep.set(5.0)
    ep.update_state()
    hook({"d": device})  # retried on the very next tick, not after a full interval
    assert ep.get_history() == [5.0]

    now[0] += 1.0
    ep.set(6.0)
    ep.update_state()
    hook({"d": device})  # now waits a full interval from the successful sample
    assert ep.get_history() == [5.0]


# ---------- history: load_system integration ----------

def test_load_system_registers_history_tick_hook(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: sensor
    module: virtual
    update: 5s
    endpoints: [{ key: temp, type: float, history: 4 }]
""")
    system = load_system(system_yaml)
    assert len(system.tick_hooks) >= 1


def test_load_system_registers_no_history_hook_without_history(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: sensor
    module: virtual
    update: 5s
    endpoints: [{ key: temp, type: float }]
""")
    system = load_system(system_yaml)
    assert len(system.tick_hooks) == 0


def test_load_system_history_raises_for_unpolled_device_without_interval(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: sensor
    module: virtual
    update: null
    endpoints: [{ key: temp, type: float, history: 4 }]
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_history_with_explicit_interval_ignores_missing_device_update(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: sensor
    module: virtual
    update: null
    endpoints:
      - { key: temp, type: float, history: { size: 4, interval: 5m } }
""")
    system = load_system(system_yaml)
    assert len(system.tick_hooks) >= 1


def test_load_system_history_interval_accepts_top_level_named_interval(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
intervals:
  radon: 1m
devices:
  - id: sensor
    module: virtual
    update: null
    endpoints:
      - { key: temp, type: float, history: { size: 4, interval: radon } }
""")
    system = load_system(system_yaml)
    endpoint = system.devices["sensor"].endpoint("temp")
    assert endpoint.history_interval == 60.0


def test_load_system_builds_tree_with_dotted_ids(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
intervals: { fast: 3s }
devices:
  - id: living_light
    module: virtual
    update: fast
    endpoints:
      - key: state
        writable: true
        default: "off"
  - id: house
    module: host
    children:
      - id: kitchen
        module: host
        children:
          - id: kitchen_light
            module: virtual
            update: 5s
            endpoints:
              - key: state
                writable: true
""")
    system = load_system(system_yaml)

    assert set(system.devices.keys()) == {
        "living_light", "house", "house.kitchen", "house.kitchen.kitchen_light",
    }
    assert system.devices["living_light"].get() == "off"

    scheduled = system.scheduled_devices()
    assert "living_light" in scheduled
    assert "house.kitchen.kitchen_light" in scheduled
    assert "house" not in scheduled
    assert "house.kitchen" not in scheduled


def test_load_system_modules_section_sets_module_scoped_param_for_all_instances(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
modules:
  meteoswiss:
    cache_time: 5m
devices:
  - id: meteo-bern
    module: meteoswiss
    station: BER
  - id: meteo-zurich
    module: meteoswiss
    station: ZRH
""")
    system = load_system(system_yaml)
    assert system.devices["meteo-bern"].params["cache_time"] == "5m"
    assert system.devices["meteo-zurich"].params["cache_time"] == "5m"


def test_load_system_modules_section_defaults_when_absent(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: meteo-bern
    module: meteoswiss
    station: BER
""")
    system = load_system(system_yaml)
    assert system.devices["meteo-bern"].params["cache_time"] == "10m"


def test_load_system_rejects_module_scoped_param_in_device_params(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: meteo-bern
    module: meteoswiss
    station: BER
    cache_time: 1m
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_rejects_unknown_modules_section_param(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
modules:
  meteoswiss:
    bogus: 1
devices:
  - id: meteo-bern
    module: meteoswiss
    station: BER
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_modules_section_device_scoped_default_used_when_device_omits_it(tmp_path):
    # zway's base_url is override: required, scope: device -- supplying it
    # once directly under modules.zway lets every device below omit it
    # entirely.
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
modules:
  zway:
    base_url: http://192.168.1.1:8083
devices:
  - id: light_one
    module: zway
  - id: light_two
    module: zway
""")
    system = load_system(system_yaml)
    assert system.devices["light_one"].params["base_url"] == "http://192.168.1.1:8083"
    assert system.devices["light_two"].params["base_url"] == "http://192.168.1.1:8083"


def test_load_system_modules_section_device_param_overrides_module_default(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
modules:
  zway:
    base_url: http://192.168.1.1:8083
devices:
  - id: light
    module: zway
    base_url: "http://other-controller:8083"
""")
    system = load_system(system_yaml)
    assert system.devices["light"].params["base_url"] == "http://other-controller:8083"


def test_load_system_modules_section_update_sits_between_device_and_module_yaml(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
intervals: { zwave: 30s }
modules:
  zway:
    update: zwave
    base_url: "http://x:8083"
devices:
  - id: light_default
    module: zway
  - id: light_override
    module: zway
    update: 5s
""")
    system = load_system(system_yaml)
    assert system.devices["light_default"].update_interval == 30.0
    assert system.devices["light_override"].update_interval == 5.0


def test_load_system_modules_section_update_null_means_never_poll(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
modules:
  zway:
    update: null
    base_url: "http://x:8083"
devices:
  - id: light
    module: zway
""")
    system = load_system(system_yaml)
    assert system.devices["light"].update_interval is None


def test_load_system_rejects_unknown_key_under_modules_section(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
modules:
  zway:
    bogus_key: 1
devices: []
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_rejects_typo_d_module_name_in_modules_section(tmp_path):
    # A typo'd module NAME under modules: is validated up front, even
    # though no device below actually uses "zwya" -- rather than being
    # silently ignored (see load_system's eager modules_config validation
    # pass, before _build_device is ever called).
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
modules:
  zwya:
    base_url: "http://x:8083"
devices: []
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_resolves_device_profile_defined_under_modules_section(tmp_path):
    # A system config can extend a module's profile library too (see
    # _build_effective_module) -- device_profile: siren resolves against a
    # profile that exists only in this system YAML, not in virtual's own
    # module.yaml.
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
modules:
  virtual:
    device_profiles:
      siren:
        endpoints:
          - key: state
            writable: true
            type: int
            values: { 0: "off", 1: "on" }
            default: 0
devices:
  - id: siren_one
    module: virtual
    device_profile: siren
  - id: siren_two
    module: virtual
    device_profile: siren
""")
    system = load_system(system_yaml)
    assert system.devices["siren_one"].get("state") == 0
    assert system.devices["siren_two"].get("state") == 0


def test_load_system_rejects_system_device_profile_name_colliding_with_module_yaml_profile(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
modules:
  zway:
    base_url: "http://x:8083"
    device_profiles:
      fibaro-fgs222:
        endpoints:
          - key: state
devices: []
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_rejects_system_device_profiles_under_module_with_nonempty_endpoints(tmp_path):
    # meteoswiss's module.yaml declares six unconditional endpoints: -- same
    # base/overlay ambiguity as module.yaml declaring both itself.
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
modules:
  meteoswiss:
    device_profiles:
      x:
        endpoints:
          - key: y
devices: []
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_rejects_malformed_system_device_profile_entry(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
modules:
  virtual:
    device_profiles:
      siren:
        brand: Acme
devices: []
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_rejects_unknown_device_entry_key(tmp_path):
    # A typo'd device-entry key (e.g. "device_profil" instead of
    # "device_profile") must not be silently ignored: devices/zway/device.py
    # documents that a misconfigured endpoint permanently reports None with
    # no error, so catching the typo here, at load time, is the only safety
    # net.
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: light
    module: virtual
    bogus_key: 1
    endpoints:
      - key: state
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_rejects_unknown_endpoint_entry_key(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: light
    module: virtual
    endpoints:
      - key: state
        bogus_key: 1
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_merge_endpoints_rejects_unknown_endpoint_entry_key():
    module = ModuleDescriptor("m", {"endpoints": []})
    with pytest.raises(ConfigError):
        _merge_endpoints(module, [{"key": "state", "bogus_key": 1}], "dev", {})


def test_load_system_rejects_old_profile_key_spelling_on_device(tmp_path):
    # profile: was split into device_profile:/endpoint_profile: -- the old
    # spelling must be rejected as an unrecognized key, not silently
    # ignored (which would leave the device with zero endpoints).
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: light
    module: virtual
    profile: bogus
    endpoints: [{ key: state }]
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_rejects_old_profile_key_spelling_on_endpoint(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: light
    module: virtual
    endpoints:
      - key: state
        profile: bogus
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_rejects_old_parameters_key_spelling_on_endpoint(tmp_path):
    # parameters: was renamed to params: -- the old spelling must be
    # rejected as an unrecognized key.
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: light
    module: virtual
    endpoints:
      - key: state
        parameters: { foo: bar }
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_rejects_params_key_on_endpoint(tmp_path):
    # params: nesting on an endpoint was itself replaced by declared,
    # top-level endpoint_parameters -- params: is no longer accepted either.
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: light
    module: virtual
    endpoints:
      - key: state
        params: { foo: bar }
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_sibling_local_ids_do_not_collide(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: zone_a
    module: host
    children:
      - id: light
        module: virtual
        update: 1s
        endpoints: [{ key: state, writable: true }]
  - id: zone_b
    module: host
    children:
      - id: light
        module: virtual
        update: 1s
        endpoints: [{ key: state, writable: true }]
""")
    system = load_system(system_yaml)
    assert "zone_a.light" in system.devices
    assert "zone_b.light" in system.devices
    assert system.devices["zone_a.light"] is not system.devices["zone_b.light"]


def test_load_system_parses_blink_and_report_tasks(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    update: 1s
    endpoints:
      - key: state
        writable: true
        default: "off"
tasks:
  - tag: blink
    description: "Toggle the light every 3s"
    time: "+1s"
    repeat: 3s
    action: { kind: toggle, device: "living_light.state" }
  - tag: report
    description: "Log whenever the light changes"
    repeat: 0
    condition: { device: "living_light.state", changed: true }
    action: { kind: log, device: "living_light.state", message: "living_light changed to {state}" }
""")
    system = load_system(system_yaml)
    assert {t.tag for t in system.tasks} == {"blink", "report"}

    blink = next(t for t in system.tasks if t.tag == "blink")
    assert blink.repeat == 3.0
    assert blink.condition is None
    assert blink.actions[0].device_id == "living_light"
    assert blink.actions[0].endpoint_key == "state"

    report = next(t for t in system.tasks if t.tag == "report")
    assert report.condition is not None
    assert report.condition.changed is True
    assert report.condition.device_id == "living_light"
    assert report.condition.endpoint_key == "state"


def test_load_system_action_device_without_endpoint_resolves_bare(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: desk_lamp
    module: virtual
    update: 1s
    endpoints:
      - key: power
        writable: true
        default: "off"
      - key: brightness
        writable: true
        default: 0
tasks:
  - tag: report
    condition: { device: "desk_lamp.power", changed: true }
    action: { kind: log, device: "desk_lamp", message: "desk_lamp changed to {state}" }
""")
    system = load_system(system_yaml)
    task = next(t for t in system.tasks if t.tag == "report")
    assert task.actions[0].device_id == "desk_lamp"
    assert task.actions[0].endpoint_key is None


def test_load_system_condition_device_without_endpoint_still_rejected(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: desk_lamp
    module: virtual
    update: 1s
    endpoints: [{ key: power, writable: true, default: "off" }]
tasks:
  - tag: report
    condition: { device: "desk_lamp", changed: true }
    action: { kind: log, device: "desk_lamp.power", message: "changed to {state}" }
""")
    # Conditions require a dotted device.endpoint reference (see
    # resolve_endpoint_ref's allow_bare docstring): a bare device would make
    # get_event(None) return a dict on a multi-endpoint device, which is
    # never None, silently defeating the "changed" gate.
    with pytest.raises(ValueError):
        load_system(system_yaml)


def test_load_system_task_resolves_nested_device_endpoint(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: house
    module: host
    children:
      - id: desk_lamp
        module: virtual
        update: 1s
        endpoints:
          - key: power
            writable: true
            default: "off"
tasks:
  - tag: lamp_on
    time: "+1s"
    action: { kind: set, device: "house.desk_lamp.power", value: "on" }
""")
    system = load_system(system_yaml)
    task = next(t for t in system.tasks if t.tag == "lamp_on")
    assert task.actions[0].device_id == "house.desk_lamp"
    assert task.actions[0].endpoint_key == "power"


def test_load_system_task_with_actions_list_builds_all_actions(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
  - id: desk_lamp
    module: virtual
    endpoints: [{ key: power, writable: true, default: "off" }]
tasks:
  - tag: both
    time: "+1s"
    actions:
      - { kind: set, device: "living_light.state", value: "on" }
      - { kind: set, device: "desk_lamp.power", value: "on" }
""")
    system = load_system(system_yaml)
    task = next(t for t in system.tasks if t.tag == "both")
    assert len(task.actions) == 2
    assert task.actions[0].device_id == "living_light"
    assert task.actions[1].device_id == "desk_lamp"


def test_load_system_task_rejects_both_action_and_actions_keys(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: bad
    time: "+1s"
    action: { kind: set, device: "living_light.state", value: "on" }
    actions:
      - { kind: set, device: "living_light.state", value: "on" }
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_task_rejects_neither_action_nor_actions_keys(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: bad
    time: "+1s"
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_task_actions_must_be_nonempty_list(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: bad
    time: "+1s"
    actions: []
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_create_task_action_builds_without_raising_at_load_time(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    update: 1s
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: raise_alert
    condition: { device: "living_light.state", changed: true }
    action:
      kind: create_task
      specs:
        tag: clear_alert
        time: "+1s"
        action: { kind: set, device: "living_light.state", value: "off" }
""")
    system = load_system(system_yaml)
    task = next(t for t in system.tasks if t.tag == "raise_alert")
    from core.task import CreateTaskAction
    assert isinstance(task.actions[0], CreateTaskAction)


def test_load_system_create_task_with_bad_nested_device_does_not_raise_at_load_time(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    update: 1s
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: raise_alert
    condition: { device: "living_light.state", changed: true }
    action:
      kind: create_task
      specs:
        tag: clear_alert
        time: "+1s"
        action: { kind: set, device: "nonexistent.state", value: "off" }
""")
    # Lazy validation: the nested spec references an unknown device, but
    # since create_task only builds it when it actually fires, load_system()
    # itself must not raise.
    system = load_system(system_yaml)
    assert any(t.tag == "raise_alert" for t in system.tasks)


def test_load_system_create_task_missing_specs_raises(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: raise_alert
    time: "+1s"
    action:
      kind: create_task
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_task_referencing_unknown_device_raises(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices: []
tasks:
  - tag: bad
    time: "+1s"
    action: { kind: toggle, device: "nope.state" }
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_task_missing_time_and_condition_raises(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: bad
    action: { kind: toggle, device: "living_light.state" }
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_duplicate_task_tags_raise(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: dup
    time: "+1s"
    action: { kind: toggle, device: "living_light.state" }
  - tag: dup
    time: "+2s"
    action: { kind: toggle, device: "living_light.state" }
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


# ---------- ExtensionDescriptor / _merge_extension_params ----------

def test_merge_extension_params_uses_default_when_no_override():
    descriptor = ExtensionDescriptor("logdb", {
        "parameters": [{"name": "max_records", "override": "allowed", "default": 100000}],
    })
    merged = _merge_extension_params(descriptor, {}, "logdb.house_log")
    assert merged == {"max_records": 100000}


def test_merge_extension_params_instance_overrides_allowed():
    descriptor = ExtensionDescriptor("logdb", {
        "parameters": [{"name": "max_records", "override": "allowed", "default": 100000}],
    })
    merged = _merge_extension_params(descriptor, {"max_records": 500}, "logdb.house_log")
    assert merged == {"max_records": 500}


def test_merge_extension_params_none_override_rejects_instance_value():
    descriptor = ExtensionDescriptor("logdb", {
        "parameters": [{"name": "csv_path", "override": "none", "default": "log.csv"}],
    })
    with pytest.raises(ConfigError):
        _merge_extension_params(descriptor, {"csv_path": "evil.csv"}, "logdb.house_log")


def test_merge_extension_params_required_missing_raises():
    descriptor = ExtensionDescriptor("logdb", {
        "parameters": [{"name": "allow", "override": "required"}],
    })
    with pytest.raises(ConfigError):
        _merge_extension_params(descriptor, {}, "logdb.house_log")


def test_merge_extension_params_required_supplied_ok():
    descriptor = ExtensionDescriptor("logdb", {
        "parameters": [{"name": "allow", "override": "required"}],
    })
    merged = _merge_extension_params(descriptor, {"allow": ["*"]}, "logdb.house_log")
    assert merged == {"allow": ["*"]}


def test_merge_extension_params_unknown_param_raises():
    descriptor = ExtensionDescriptor("logdb", {"parameters": []})
    with pytest.raises(ConfigError):
        _merge_extension_params(descriptor, {"bogus": 1}, "logdb.house_log")


# ---------- _load_extensions ----------

def test_load_extensions_returns_empty_dict_when_no_extensions_key():
    assert _load_extensions({}, {}) == {}


def test_load_extensions_returns_empty_dict_when_extensions_key_empty():
    assert _load_extensions({"extensions": {}}, {}) == {}


def test_load_extensions_rejects_non_mapping_instances():
    with pytest.raises(ConfigError):
        _load_extensions({"extensions": {"logdb": "not-a-mapping"}}, {})


def test_load_extensions_rejects_unknown_extension_name():
    with pytest.raises(ConfigError):
        _load_extensions({"extensions": {"nonexistent": {"an_instance": {}}}}, {})


def test_load_system_condition_expr_builds_expr_condition(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    update: 1s
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: report
    condition:
      refs: { s: "living_light.state" }
      expr: "s.changed"
    action: { kind: log, device: "living_light.state", message: "changed" }
""")
    system = load_system(system_yaml)
    from core.task import ExprCondition
    task = next(t for t in system.tasks if t.tag == "report")
    assert isinstance(task.condition, ExprCondition)


def test_load_system_condition_requires_exactly_one_of_device_or_expr(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: bad
    condition: {}
    action: { kind: log, device: "living_light.state", message: "x" }
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_condition_rejects_both_device_and_expr(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: bad
    condition: { device: "living_light.state", expr: "True" }
    action: { kind: log, device: "living_light.state", message: "x" }
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_condition_expr_bad_syntax_raises_config_error(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: bad
    condition: { expr: "__import__('os')" }
    action: { kind: log, device: "living_light.state", message: "x" }
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_condition_expr_unknown_device_raises(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: bad
    condition: { expr: "state('nonexistent.state') == 1" }
    action: { kind: log, device: "living_light.state", message: "x" }
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_condition_expr_registers_sticky_tick_hook(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    update: 1s
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: report
    condition: { expr: "sticky('living_light.state') == 'off'" }
    action: { kind: log, device: "living_light.state", message: "x" }
""")
    system = load_system(system_yaml)
    assert len(system.tick_hooks) >= 1


# ---------- start_hooks / stop_hooks auto-collection ----------

def test_load_system_defaults_to_empty_start_and_stop_hooks(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
""")
    system = load_system(system_yaml)
    assert system.start_hooks == []
    assert system.stop_hooks == []


def test_load_system_collects_on_start_on_stop_from_extension_instances(tmp_path, monkeypatch):
    """No shipped extension has on_start/on_stop yet (only extensions.web_ui
    will), so this exercises core.config's auto-collection directly by
    monkeypatching _load_extensions to return a fake instance -- the same
    collection mechanism already proven for on_tick (see
    test_load_system_condition_expr_registers_sticky_tick_hook above and
    logdb/random_light's own end-to-end tests)."""
    import core.config as config_module

    calls = {"start": [], "stop": []}

    class FakeExtensionInstance:
        async def on_start(self, devices):
            calls["start"].append(devices)

        async def on_stop(self, devices):
            calls["stop"].append(devices)

    monkeypatch.setattr(config_module, "_load_extensions",
                         lambda raw, flat: {"fake.instance": FakeExtensionInstance()})

    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
""")
    system = load_system(system_yaml)

    assert len(system.start_hooks) == 1
    assert len(system.stop_hooks) == 1
    import asyncio
    asyncio.run(system.start_hooks[0]({"living_light": system.devices["living_light"]}))
    asyncio.run(system.stop_hooks[0]({"living_light": system.devices["living_light"]}))
    assert len(calls["start"]) == 1
    assert len(calls["stop"]) == 1


# ---------- Task.min_interval ----------

def test_load_system_task_min_interval_parsed(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: report
    min_interval: 5s
    condition: { device: "living_light.state", changed: true }
    action: { kind: log, device: "living_light.state", message: "x" }
""")
    system = load_system(system_yaml)
    task = next(t for t in system.tasks if t.tag == "report")
    assert task.min_interval == 5.0


def test_load_system_task_min_interval_defaults_to_zero(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: report
    time: "+1s"
    action: { kind: log, device: "living_light.state", message: "x" }
""")
    system = load_system(system_yaml)
    task = next(t for t in system.tasks if t.tag == "report")
    assert task.min_interval == 0.0


# ---------- kind: script / kind: kill_task ----------

def test_load_system_script_action_builds(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    update: 1s
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: react
    time: "+1s"
    action:
      kind: script
      code: |
        set_state('living_light.state', 'on')
        log('done')
""")
    system = load_system(system_yaml)
    from core.task import ScriptAction
    task = next(t for t in system.tasks if t.tag == "react")
    assert isinstance(task.actions[0], ScriptAction)


def test_load_system_script_action_bad_syntax_raises_config_error(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: react
    time: "+1s"
    action:
      kind: script
      code: "import os"
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_script_action_unknown_referenced_device_raises(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: react
    time: "+1s"
    action:
      kind: script
      code: "set_state('nonexistent.state', 'on')"
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_set_action_expr_builds(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: source
    module: virtual
    endpoints: [{ key: state, writable: true, default: "on" }]
  - id: target
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: mirror
    condition: { device: "source.state", changed: true }
    action: { kind: set, device: "target.state", expr: "state('source.state')" }
""")
    system = load_system(system_yaml)
    from core.task import SetAction
    task = next(t for t in system.tasks if t.tag == "mirror")
    action = task.actions[0]
    assert isinstance(action, SetAction)
    assert action.compiled is not None


def test_load_system_set_action_requires_exactly_one_of_value_or_expr(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: bad
    time: "+1s"
    action: { kind: set, device: "living_light.state" }
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_set_action_rejects_both_value_and_expr(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: bad
    time: "+1s"
    action: { kind: set, device: "living_light.state", value: "on", expr: "'on'" }
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_set_action_expr_bad_syntax_raises_config_error(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: bad
    time: "+1s"
    action: { kind: set, device: "living_light.state", expr: "__import__('os')" }
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_set_action_expr_unknown_referenced_device_raises(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: bad
    time: "+1s"
    action: { kind: set, device: "living_light.state", expr: "state('nonexistent.state')" }
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_set_action_expr_registers_sticky_tick_hook(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: source
    module: virtual
    update: 1s
    endpoints: [{ key: state, writable: true, default: "on" }]
  - id: target
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: mirror
    time: "+1s"
    action: { kind: set, device: "target.state", expr: "state('source.state')" }
""")
    system = load_system(system_yaml)
    assert len(system.tick_hooks) >= 1


def test_load_system_kill_task_action_builds(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: disable
    time: "+1s"
    action: { kind: kill_task, tags: ["surveillance_*"] }
""")
    system = load_system(system_yaml)
    from core.task import KillTaskAction
    task = next(t for t in system.tasks if t.tag == "disable")
    assert isinstance(task.actions[0], KillTaskAction)


def test_load_system_log_db_action_unknown_instance_raises(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text(f"""
heartbeat: 1s
devices:
  - id: alarm
    module: virtual
    update: 1s
    endpoints: [{{ key: state, writable: true, type: int, default: 0 }}]
extensions:
  logdb:
    house_log:
      selectors: ["alarm/state"]
      csv_path: "{(tmp_path / 'log.csv').as_posix()}"
tasks:
  - tag: log_house
    time: "+1s"
    action: {{ kind: log_db, instance: "logdb.nonexistent" }}
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


# ---------- !include ----------

def test_load_system_include_as_mapping_value(tmp_path):
    (tmp_path / "station.yaml").write_text("BER\n")
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: meteo-bern
    module: meteoswiss
    station: !include station.yaml
""")
    system = load_system(system_yaml)
    assert system.devices["meteo-bern"].params["station"] == "BER"


def test_load_system_include_as_sequence_item(tmp_path):
    (tmp_path / "device.yaml").write_text("""
id: living_light
module: virtual
update: fast
endpoints:
  - key: state
    writable: true
    default: "off"
""")
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
intervals: { fast: 3s }
devices:
  - !include device.yaml
""")
    system = load_system(system_yaml)
    assert system.devices["living_light"].get() == "off"


def test_load_system_include_resolves_relative_to_including_file(tmp_path):
    # sub/device.yaml's own !include of "endpoints.yaml" only resolves if
    # it's taken relative to sub/ (where endpoints.yaml actually lives),
    # not relative to tmp_path/ (the root system.yaml's directory).
    sub_dir = tmp_path / "sub"
    sub_dir.mkdir()
    (sub_dir / "endpoints.yaml").write_text("""
- key: state
  writable: true
  default: "off"
""")
    (sub_dir / "device.yaml").write_text("""
id: living_light
module: virtual
endpoints: !include endpoints.yaml
""")
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - !include sub/device.yaml
""")
    system = load_system(system_yaml)
    assert system.devices["living_light"].get() == "off"


def test_load_system_include_missing_file_raises(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices: !include missing.yaml
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_include_circular_raises(tmp_path):
    (tmp_path / "a.yaml").write_text("!include b.yaml\n")
    (tmp_path / "b.yaml").write_text("!include a.yaml\n")
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices: !include a.yaml
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


# ---------- <<: !include (merge-key) ----------

def test_load_system_merge_include_extends_a_mapping(tmp_path):
    (tmp_path / "conn.yaml").write_text("""
base_url: http://192.168.1.21:8083
user: admin
""")
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
intervals: { zwave: 30s }
modules:
  zway:
    update: zwave
    <<: !include conn.yaml
devices:
  - id: light
    module: zway
""")
    system = load_system(system_yaml)
    assert system.devices["light"].params["base_url"] == "http://192.168.1.21:8083"
    assert system.devices["light"].params["user"] == "admin"
    assert system.devices["light"].update_interval == 30.0


def test_load_system_merge_include_own_keys_win_over_fragment(tmp_path):
    (tmp_path / "conn.yaml").write_text("""
base_url: http://fragment:8083
""")
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
modules:
  zway:
    <<: !include conn.yaml
    base_url: http://own-key-wins:8083
devices:
  - id: light
    module: zway
""")
    system = load_system(system_yaml)
    assert system.devices["light"].params["base_url"] == "http://own-key-wins:8083"


def test_load_system_merge_include_populates_device_params(tmp_path):
    (tmp_path / "conn.yaml").write_text("""
base_url: http://192.168.1.21:8083
user: admin
password: secret
""")
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: light
    module: zway
    <<: !include conn.yaml
""")
    system = load_system(system_yaml)
    params = system.devices["light"].params
    assert params["base_url"] == "http://192.168.1.21:8083"
    assert params["user"] == "admin"
    assert params["password"] == "secret"


def test_load_system_merge_include_nested_include_resolves_relative_to_fragment(tmp_path):
    # conn.yaml's own !include of "secret.yaml" only resolves if it's taken
    # relative to sub/ (where secret.yaml actually lives), not relative to
    # tmp_path/ (the root system.yaml's directory) -- same relative-path
    # rule as a plain (non-merge) !include, see
    # test_load_system_include_resolves_relative_to_including_file.
    sub_dir = tmp_path / "sub"
    sub_dir.mkdir()
    (sub_dir / "secret.yaml").write_text("supersecret\n")
    (sub_dir / "conn.yaml").write_text("""
base_url: http://192.168.1.21:8083
password: !include secret.yaml
""")
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: light
    module: zway
    <<: !include sub/conn.yaml
""")
    system = load_system(system_yaml)
    assert system.devices["light"].params["password"] == "supersecret"


def test_load_system_merge_include_rejects_non_mapping_target(tmp_path):
    (tmp_path / "conn.yaml").write_text("just a string\n")
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: light
    module: zway
    <<: !include conn.yaml
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_merge_include_missing_file_raises(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: light
    module: zway
    <<: !include missing.yaml
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_merge_include_circular_raises(tmp_path):
    (tmp_path / "a.yaml").write_text("<<: !include b.yaml\n")
    (tmp_path / "b.yaml").write_text("<<: !include a.yaml\n")
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: light
    module: zway
    <<: !include a.yaml
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_merge_anchor_still_works(tmp_path):
    # Plain `<<: *anchor` (no !include involved) must still work exactly as
    # PyYAML's own merge-key support already provided.
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: light
    module: zway
    <<: &conn
      base_url: http://192.168.1.21:8083
      user: admin
""")
    system = load_system(system_yaml)
    assert system.devices["light"].params["base_url"] == "http://192.168.1.21:8083"
    assert system.devices["light"].params["user"] == "admin"


@pytest.fixture
def _restore_phc_logger_levels():
    # configure_logging() replaces the "phc" logger's level and handler
    # list on every call -- global, process-wide state with no built-in
    # reset -- so loading an example here must not leak into unrelated
    # tests running later in the same session (e.g.
    # tests/test_logging_setup.py). Restore both afterward, and close
    # (rather than just drop) any handler this test's configure_logging()
    # call installed and the restore is about to discard -- particularly a
    # FileHandler, which would otherwise leak an open fd.
    root = logging.getLogger("phc")
    snapshot_level = root.level
    snapshot_handlers = list(root.handlers)
    yield
    for handler in root.handlers:
        if handler not in snapshot_handlers:
            handler.close()
    root.setLevel(snapshot_level)
    root.handlers = snapshot_handlers


@pytest.mark.parametrize("example_path", sorted(_EXAMPLES_DIR.glob("*.yaml")), ids=lambda p: p.name)
def test_load_system_examples_load_without_error(example_path, _restore_phc_logger_levels):
    # Non-recursive glob: examples/common/ holds !include fragments, not
    # standalone runnable systems, so it's naturally excluded here.
    load_system(example_path)
