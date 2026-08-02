"""Tests for phc.py's command-line argument parsing."""

import pytest

from core.endpoint import Endpoint
from core.scheduler import Scheduler
from devices.virtual.device import VirtualDevice
from phc import _parse_log_level_module, _resolve_debug_portal_instance, main


def test_parse_log_level_module_splits_name_and_level():
    assert _parse_log_level_module("scheduler=DEBUG") == ("scheduler", "DEBUG")


def test_parse_log_level_module_rejects_missing_equals():
    with pytest.raises(Exception):
        _parse_log_level_module("scheduler")


def test_log_level_merge_order(tmp_path, monkeypatch):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
log:
  - dest: stdout
    levels:
      default: INFO
      scheduler: INFO
devices: []
""")

    import logging

    from core.config import load_system
    from core.logging_setup import _LevelMapFilter

    def _resolved(name, level=logging.INFO):
        root = logging.getLogger("phc")
        record = logging.LogRecord(name, level, __file__, 0, "msg", None, None)
        return all(f.filter(record) for h in root.handlers for f in h.filters
                   if isinstance(f, _LevelMapFilter))

    # No CLI overrides: config file values apply as-is.
    load_system(system_yaml, log_levels_override={})
    assert logging.getLogger("phc").getEffectiveLevel() == logging.INFO
    assert _resolved("phc.scheduler", logging.INFO) is True
    assert _resolved("phc.scheduler", logging.DEBUG) is False

    # CLI --log-level overrides default; --log-level-module overrides just that key.
    load_system(system_yaml, log_levels_override={"default": "ERROR", "scheduler": "DEBUG"})
    assert logging.getLogger("phc").getEffectiveLevel() == logging.DEBUG
    assert _resolved("phc.tasks", logging.WARNING) is False
    assert _resolved("phc.tasks", logging.ERROR) is True
    assert _resolved("phc.scheduler", logging.DEBUG) is True


class _FakeSystem:
    """Minimal stand-in for core.config.System: _resolve_debug_portal_instance
    only ever reads .devices (handed straight to configure()'s selector
    resolution) and .extensions (to detect an already YAML-configured
    debug_portal instance)."""

    def __init__(self, devices=None, extensions=None):
        self.devices = devices or {}
        self.extensions = extensions or {}


# ---------- _resolve_debug_portal_instance ----------

def test_resolve_debug_portal_instance_returns_none_without_port():
    assert _resolve_debug_portal_instance(_FakeSystem(), None, []) is None


def test_resolve_debug_portal_instance_builds_instance_bound_to_system():
    lamp = VirtualDevice("lamp", endpoints=[Endpoint("state", writable=True, value_type="bool")])
    system = _FakeSystem(devices={"lamp": lamp})

    instance = _resolve_debug_portal_instance(system, 8081, [])

    assert instance is not None
    assert instance._port == 8081
    assert instance._pairs == [("lamp", "state")]
    assert instance._system is system  # on_bind() was called


def test_resolve_debug_portal_instance_uses_given_selectors():
    lamp = VirtualDevice("lamp", endpoints=[Endpoint("state", writable=True, value_type="bool")])
    sensor = VirtualDevice("sensor", endpoints=[Endpoint("temp", value_type="float")])
    system = _FakeSystem(devices={"lamp": lamp, "sensor": sensor})

    instance = _resolve_debug_portal_instance(system, 8081, ["lamp/*"])

    assert instance._pairs == [("lamp", "state")]


def test_resolve_debug_portal_instance_raises_if_yaml_already_configures_one():
    system = _FakeSystem(extensions={"debug_portal.debug": object()})
    with pytest.raises(ValueError, match="debug_portal.debug"):
        _resolve_debug_portal_instance(system, 8081, [])


# ---------- main(): CLI misuse and wiring ----------

def _write_minimal_system(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
""")
    return system_yaml


def test_main_rejects_debug_portal_selector_without_port(tmp_path):
    system_yaml = _write_minimal_system(tmp_path)
    with pytest.raises(SystemExit):
        main(["--config", str(system_yaml), "--debug-portal-selector", "living_light/*"])


def test_main_rejects_cli_debug_portal_when_yaml_already_has_one(tmp_path):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
extensions:
  debug_portal:
    debug:
      port: 8081
""")
    with pytest.raises(SystemExit):
        main(["--config", str(system_yaml), "--debug-portal-port", "8082"])


def test_main_wires_cli_debug_portal_without_yaml_entry(tmp_path, monkeypatch):
    """End-to-end through main() itself (not just _resolve_debug_portal_instance):
    proves --debug-portal-port reaches Scheduler construction without error
    when --config has no extensions.debug_portal: of its own.
    Scheduler.run_forever is stubbed out so the test doesn't block forever --
    the real socket-binding path is already covered by
    tests/test_debug_portal_extension.py's on_start()/on_stop() tests."""
    system_yaml = _write_minimal_system(tmp_path)
    monkeypatch.setattr(Scheduler, "run_forever", lambda self: None)
    main(["--config", str(system_yaml), "--debug-portal-port", "0"])
