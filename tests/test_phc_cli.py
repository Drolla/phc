"""Tests for phc.cli: command-line argument parsing and the subcommands."""

from pathlib import Path

import pytest

from phc.core.endpoint import Endpoint
from phc.core.scheduler import Scheduler
from phc.devices.virtual.device import VirtualDevice
from phc.cli import _parse_log_level_module, _resolve_debug_portal_instance, main


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

    from phc.core.config import load_system
    from phc.core.logging_setup import _LevelMapFilter

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
    """Minimal stand-in for phc.core.config.System: _resolve_debug_portal_instance
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


def test_main_reports_config_error_without_traceback(tmp_path, capsys):
    """A ConfigError (e.g. an unfilled !placeholder -- see
    phc.core.config._find_placeholders) must reach the user as a plain
    argparse error message, not an uncaught Python traceback."""
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
modules:
  zway:
    base_url: !placeholder <URL>
devices: []
""")
    with pytest.raises(SystemExit):
        main(["--config", str(system_yaml)])
    assert "Traceback" not in capsys.readouterr().err


def test_main_reports_missing_config_file_without_traceback(tmp_path, capsys):
    missing = tmp_path / "does_not_exist.yaml"
    with pytest.raises(SystemExit):
        main(["--config", str(missing)])
    assert "Traceback" not in capsys.readouterr().err


def test_main_reports_malformed_yaml_without_traceback(tmp_path, capsys):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("devices: [\n")  # unterminated flow sequence
    with pytest.raises(SystemExit):
        main(["--config", str(system_yaml)])
    assert "Traceback" not in capsys.readouterr().err


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


# ---------- subcommands ----------

def test_validate_reports_ok_and_exits_zero(tmp_path, capsys):
    """`phc validate` runs the whole load -- discovery, parameter and
    endpoint resolution, task building -- without starting the scheduler,
    binding a port, or touching hardware."""
    config = tmp_path / "system.yaml"
    config.write_text("""
heartbeat: 2s
devices:
  - id: living_light
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
tasks:
  - tag: noop
    time: "+1h"
    action: { kind: toggle, device: "living_light.state" }
""", encoding="utf-8")

    assert main(["validate", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "1 device(s)" in out
    assert "1 task(s)" in out
    assert "heartbeat 2s" in out


def test_validate_reports_the_error_and_exits_nonzero(tmp_path, capsys):
    """A broken config is reported as a message and a non-zero exit code,
    so this is usable as a pre-deploy check."""
    config = tmp_path / "system.yaml"
    config.write_text("""
heartbeat: 1s
devices:
  - id: x
    module: no_such_module
""", encoding="utf-8")

    assert main(["validate", "--config", str(config)]) == 1
    out = capsys.readouterr().out
    assert "INVALID" in out
    assert "no_such_module" in out


def test_validate_on_a_missing_file_exits_nonzero(tmp_path, capsys):
    assert main(["validate", "--config", str(tmp_path / "nope.yaml")]) == 1
    assert "INVALID" in capsys.readouterr().out


def test_list_modules_reports_names_packages_and_parameters(capsys):
    assert main(["list-modules"]) == 0
    out = capsys.readouterr().out
    assert "virtual" in out
    assert "[phc.devices.sun]" in out, "should name the package each module lives in"
    assert "latitude (required)" in out, "should list declared parameters"


def test_list_modules_includes_a_plugin_path(capsys):
    """The listing answers "is my plugin installed?", so it has to cover
    out-of-tree modules too."""
    plugins = Path(__file__).resolve().parent / "fixtures" / "plugins"
    assert main(["list-modules", "--plugin-path", str(plugins)]) == 0
    out = capsys.readouterr().out
    assert "acme_sensor" in out
    assert "offset (default 0.0)" in out


def test_list_extensions_reports_names_and_parameters(capsys):
    assert main(["list-extensions"]) == 0
    out = capsys.readouterr().out
    assert "logdb" in out
    assert "[phc.extensions.web_ui]" in out


def test_bare_form_without_a_subcommand_still_requires_config(capsys):
    """The original `phc --config X` spelling stays the default action --
    a subcommand is opt-in, not mandatory."""
    with pytest.raises(SystemExit):
        main([])
    assert "--config" in capsys.readouterr().err
