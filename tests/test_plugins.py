"""Out-of-tree plugin discovery: a device module shipped outside the phc
package must work exactly like a bundled one.

Until this landed, `discover_modules` scanned only phc/devices/ and the
descriptor loader looked for module.yaml only under phc.devices, so there
was no way to add a device module without editing PHC itself -- the
largest gap between "pluggable framework" as designed and as implemented.
"""

from pathlib import Path

import pytest

from phc.core.config import ConfigError, load_system
from phc.core.registry import discover_modules, get_device_class, module_package
from tests.conftest import fetch_sync

# The working and the deliberately-broken plugin live in SEPARATE
# directories: scanning a directory imports every plugin in it, so a
# broken one would (correctly) blow up the tests for the good one.
PLUGINS_DIR = Path(__file__).resolve().parent / "fixtures" / "plugins"
BROKEN_PLUGINS_DIR = Path(__file__).resolve().parent / "fixtures" / "broken_plugins"
SENSOR_PLUGIN = PLUGINS_DIR / "acme_sensor"


def _system_yaml(tmp_path, body: str) -> Path:
    path = tmp_path / "system.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_plugin_path_module_is_discovered_and_configured(tmp_path):
    """The end-to-end case: a module in a plugin_paths: directory is
    usable as `module:`, its module.yaml is read from its own package, and
    its declared parameters/endpoints behave like any bundled module's."""
    system_yaml = _system_yaml(tmp_path, f"""
heartbeat: 1s
plugin_paths: ["{SENSOR_PLUGIN.parent.as_posix()}"]
devices:
  - id: shed
    module: acme_sensor
    offset: 5.0
""")
    system = load_system(system_yaml)

    device = system.devices["shed"]
    # module.yaml was found next to the plugin's own code, not under phc.
    assert module_package("acme_sensor") == "acme_sensor"
    # Its declared endpoint, type, and unit came from that module.yaml.
    endpoint = device.endpoint("temperature")
    assert endpoint.value_type == "float"
    assert endpoint.unit == "°C"
    # Its declared parameter reached the device.
    fetch_sync(device)
    device.update_state()
    assert device.get("temperature") == 25.0
    assert device.get_text("temperature") == "25.0 °C"
    # And its module.yaml's own `update:` default applied.
    assert device.update_interval == 1.0


def test_plugin_module_parameter_defaults_apply(tmp_path):
    """A plugin's module.yaml default is honoured like a bundled one's --
    i.e. the descriptor really is being parsed, not just located."""
    system_yaml = _system_yaml(tmp_path, f"""
heartbeat: 1s
plugin_paths: ["{SENSOR_PLUGIN.parent.as_posix()}"]
devices:
  - id: shed
    module: acme_sensor
""")
    system = load_system(system_yaml)
    device = system.devices["shed"]
    fetch_sync(device)
    device.update_state()
    assert device.get("temperature") == 20.0    # offset defaulted to 0.0


def test_unknown_module_names_what_is_available(tmp_path):
    """A typo'd module: is a config error naming the discovered modules,
    rather than a bare KeyError from the registry."""
    system_yaml = _system_yaml(tmp_path, """
heartbeat: 1s
devices:
  - id: x
    module: not_a_real_module
""")
    with pytest.raises(ConfigError) as excinfo:
        load_system(system_yaml)
    message = str(excinfo.value)
    assert "not_a_real_module" in message
    assert "virtual" in message, "should list what IS available"


def test_a_plugins_own_broken_import_is_reported_not_swallowed():
    """Regression guard: discovery used to `except ModuleNotFoundError:
    continue`, which could not tell "no device.py here" from "device.py
    exists but its own import failed". The second case must surface as the
    import error it is, naming the missing dependency."""
    with pytest.raises(ModuleNotFoundError) as excinfo:
        discover_modules(plugin_paths=[str(BROKEN_PLUGINS_DIR)])
    assert "a_dependency_that_is_not_installed" in str(excinfo.value)


def test_a_package_without_a_device_module_is_skipped_silently(tmp_path):
    """The other half of the same distinction: a directory that simply
    isn't a device module is not an error."""
    (tmp_path / "just_a_package").mkdir()
    (tmp_path / "just_a_package" / "__init__.py").write_text("", encoding="utf-8")
    discover_modules(plugin_paths=[str(tmp_path)])   # must not raise
    with pytest.raises(KeyError):
        get_device_class("just_a_package")


def test_plugin_paths_rejects_a_missing_directory(tmp_path):
    """A mistyped plugin_paths: entry fails at load with a clear message,
    rather than silently discovering nothing."""
    system_yaml = _system_yaml(tmp_path, """
heartbeat: 1s
plugin_paths: ["./does_not_exist"]
devices: []
""")
    with pytest.raises(ConfigError, match="plugin_paths"):
        load_system(system_yaml)
