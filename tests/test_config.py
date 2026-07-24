"""Tests for core.config: system YAML loading, param/endpoint merging, and validation."""

import pytest

from core.config import (ConfigError, ExtensionDescriptor, ModuleDescriptor, _load_extensions,
                          _merge_endpoints, _merge_extension_params, _merge_params,
                          _resolve_interval, _resolve_module_params, load_system)


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


def test_resolve_module_params_uses_default_when_absent():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "cache_time", "override": "allowed", "scope": "module", "default": "10m"},
        ]
    })
    assert _resolve_module_params(module, {}) == {"cache_time": "10m"}


def test_resolve_module_params_supplied_value_used():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "cache_time", "override": "allowed", "scope": "module", "default": "10m"},
        ]
    })
    modules_config = {"m": {"params": {"cache_time": "5m"}}}
    assert _resolve_module_params(module, modules_config) == {"cache_time": "5m"}


def test_resolve_module_params_required_missing_raises():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "region", "override": "required", "scope": "module"},
        ]
    })
    with pytest.raises(ConfigError):
        _resolve_module_params(module, {})


def test_resolve_module_params_required_supplied_ok():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "region", "override": "required", "scope": "module"},
        ]
    })
    modules_config = {"m": {"params": {"region": "CH"}}}
    assert _resolve_module_params(module, modules_config) == {"region": "CH"}


def test_resolve_module_params_none_override_rejects_supplied_value():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "data_url", "override": "none", "scope": "module", "default": "http://x"},
        ]
    })
    modules_config = {"m": {"params": {"data_url": "http://evil"}}}
    with pytest.raises(ConfigError):
        _resolve_module_params(module, modules_config)


def test_resolve_module_params_unknown_param_raises():
    module = ModuleDescriptor("m", {"parameters": []})
    modules_config = {"m": {"params": {"bogus": 1}}}
    with pytest.raises(ConfigError):
        _resolve_module_params(module, modules_config)


def test_resolve_module_params_rejects_device_scope_param_set_at_module_level():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "station", "override": "allowed", "scope": "device", "default": "BER"},
        ]
    })
    modules_config = {"m": {"params": {"station": "ZRH"}}}
    with pytest.raises(ConfigError):
        _resolve_module_params(module, modules_config)


def test_resolve_module_params_ignores_unrelated_modules_config_when_no_module_scope_params():
    module = ModuleDescriptor("m", {
        "parameters": [
            {"name": "station", "override": "required", "scope": "device"},
        ]
    })
    assert _resolve_module_params(module, {"other": {"params": {"x": 1}}}) == {}


def test_merge_endpoints_defaults_log_aggregation_to_max():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "state"}],
    })
    endpoints, _ = _merge_endpoints(module, [], "dev")
    assert endpoints[0].log_aggregation == "max"


def test_merge_endpoints_accepts_explicit_log_aggregation():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "state", "log_aggregation": "min"}],
    })
    endpoints, _ = _merge_endpoints(module, [], "dev")
    assert endpoints[0].log_aggregation == "min"


def test_merge_endpoints_rejects_invalid_log_aggregation():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "state", "log_aggregation": "bogus"}],
    })
    with pytest.raises(ConfigError):
        _merge_endpoints(module, [], "dev")


def test_merge_endpoints_static_module_with_instance_default_override():
    module = ModuleDescriptor("m", {
        "endpoints": [
            {"key": "temperature", "readable": True, "writable": False},
        ]
    })
    endpoints, seeds = _merge_endpoints(module, [], "dev")
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
        "dev",
    )
    keys = {e.key for e in endpoints}
    assert keys == {"power", "brightness"}
    assert len(seeds) == 2


def test_merge_endpoints_instance_parameters_merge_per_key():
    module = ModuleDescriptor("m", {
        "endpoints": [
            {"key": "temperature", "parameters": {"column": "tre200s0", "unit": "C"}},
        ]
    })
    endpoints, _ = _merge_endpoints(
        module, [{"key": "temperature", "parameters": {"unit": "F"}}], "dev",
    )
    assert endpoints[0].parameters == {"column": "tre200s0", "unit": "F"}


def test_merge_endpoints_parses_type_unit_values():
    module = ModuleDescriptor("m", {
        "endpoints": [
            {"key": "state", "type": "int", "unit": "%", "values": {0: "off", 1: "on"}},
        ]
    })
    endpoints, _ = _merge_endpoints(module, [], "dev")
    assert endpoints[0].value_type == "int"
    assert endpoints[0].unit == "%"
    assert endpoints[0].values == {0: "off", 1: "on"}


def test_merge_endpoints_rejects_invalid_type():
    module = ModuleDescriptor("m", {
        "endpoints": [{"key": "state", "type": "bogus"}],
    })
    with pytest.raises(ConfigError):
        _merge_endpoints(module, [], "dev")


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


def test_load_system_builds_tree_with_dotted_ids(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
log: { dest: stdout }
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
    params:
      cache_time: 5m
devices:
  - id: meteo-bern
    module: meteoswiss
    params: { station: BER }
  - id: meteo-zurich
    module: meteoswiss
    params: { station: ZRH }
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
    params: { station: BER }
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
    params: { station: BER, cache_time: 1m }
""")
    with pytest.raises(ConfigError):
        load_system(system_yaml)


def test_load_system_rejects_unknown_modules_section_param(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
modules:
  meteoswiss:
    params:
      bogus: 1
devices:
  - id: meteo-bern
    module: meteoswiss
    params: { station: BER }
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
