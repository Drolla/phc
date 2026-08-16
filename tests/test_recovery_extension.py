"""Tests for phc.extensions.recovery.extension: selector resolution/validation,
configure(), and the RecoveryInstance's on_start/on_tick/on_stop hook
wiring, plus an end-to-end test through phc.core.config.load_system() and a
simulated crash/restart. Selector pattern resolution itself is tested in
tests/test_selectors.py (phc.core.selectors.resolve_selectors)."""

import asyncio
import logging
from pathlib import Path

import pytest

from phc.core.config import ConfigError, load_system
from phc.core.endpoint import Endpoint
from phc.core.registry import discover_extensions
from phc.core.scheduler import Scheduler
from phc.devices.virtual.device import VirtualDevice
from phc.extensions.recovery.extension import configure
from phc.extensions.recovery.recovery import RecoveryStore
from tests.conftest import fetch_sync


def _commit(device, endpoint_key, value):
    device.set(value, name=endpoint_key)
    fetch_sync(device)
    device.update_state()


def _house():
    lamp = VirtualDevice("desk_lamp", endpoints=[
        Endpoint("power", writable=True, value_type="int"),
        Endpoint("brightness", writable=True, value_type="int"),
        Endpoint("battery_level", writable=False, value_type="int"),
    ], parent_qualified_id="house")
    thermostat = VirtualDevice("thermostat", endpoints=[
        Endpoint("setpoint", writable=True, value_type="float"),
    ], parent_qualified_id="house")
    sensor = VirtualDevice("sensor", endpoints=[
        Endpoint("temperature", writable=False, value_type="float"),
    ], parent_qualified_id="house")
    house = VirtualDevice("house", children=[lamp, thermostat, sensor])
    return {
        "house": house,
        "house.desk_lamp": lamp,
        "house.thermostat": thermostat,
        "house.sensor": sensor,
    }


@pytest.fixture
def recovery_log(caplog):
    """caplog, but attached directly to the "phc.recovery" logger,
    bypassing propagate=False set by configure_logging() (invoked via
    load_system() in other tests in this session) -- see
    tests/test_logdb.py's logdb_log fixture for the same pattern. Set at
    INFO (not WARNING) since some tests assert on the on_start() summary
    line, which logs at INFO."""
    recovery_logger = logging.getLogger("phc.recovery")
    recovery_logger.addHandler(caplog.handler)
    recovery_logger.setLevel(logging.INFO)
    try:
        with caplog.at_level("INFO", logger="phc.recovery"):
            yield caplog
    finally:
        recovery_logger.removeHandler(caplog.handler)


# ---------- configure() ----------

def test_configure_resolves_selectors_and_builds_writable_only_pairs(tmp_path):
    flat = _house()
    params = {"selectors": ["house.desk_lamp/power"], "path": str(tmp_path / "r.yaml")}
    instance = configure(params, flat, "recovery.main")
    assert instance.pairs == [("house.desk_lamp", "power")]


def test_configure_raises_config_error_when_selector_matches_non_writable_endpoint(tmp_path):
    flat = _house()
    params = {"selectors": ["house.sensor/temperature"], "path": str(tmp_path / "r.yaml")}
    with pytest.raises(ConfigError, match="house.sensor/temperature"):
        configure(params, flat, "recovery.main")


def test_configure_raises_config_error_when_selectors_match_zero_endpoints(tmp_path):
    flat = _house()
    params = {"selectors": ["nonexistent.device/*"], "path": str(tmp_path / "r.yaml")}
    with pytest.raises(ConfigError):
        configure(params, flat, "recovery.main")


def test_configure_raises_config_error_when_some_matches_are_writable_and_others_are_not(tmp_path):
    flat = _house()
    params = {"selectors": ["house.desk_lamp/*"], "path": str(tmp_path / "r.yaml")}
    with pytest.raises(ConfigError, match="battery_level"):
        configure(params, flat, "recovery.main")


# ---------- on_tick() ----------

def test_on_tick_writes_file_when_a_pair_changes(tmp_path):
    flat = _house()
    path = tmp_path / "r.yaml"
    instance = configure({"selectors": ["house.desk_lamp/power"], "path": str(path)}, flat, "recovery.main")
    lamp = flat["house.desk_lamp"]

    _commit(lamp, "power", 1)
    instance.on_tick(flat)

    assert RecoveryStore(path).load() == {"house.desk_lamp/power": 1}


def test_on_tick_does_not_write_when_nothing_changed(tmp_path, monkeypatch):
    flat = _house()
    path = tmp_path / "r.yaml"
    instance = configure({"selectors": ["house.desk_lamp/power"], "path": str(path)}, flat, "recovery.main")
    calls = []
    monkeypatch.setattr(instance.store, "write", lambda values: calls.append(values))

    instance.on_tick(flat)  # never committed -- get_event() is None
    instance.on_tick(flat)

    assert calls == []


def test_on_tick_persists_every_pairs_current_value_not_just_the_changed_one(tmp_path):
    flat = _house()
    path = tmp_path / "r.yaml"
    instance = configure(
        {"selectors": ["house.desk_lamp/power", "house.desk_lamp/brightness"], "path": str(path)},
        flat, "recovery.main")
    lamp = flat["house.desk_lamp"]

    _commit(lamp, "power", 1)
    _commit(lamp, "brightness", 50)
    instance.on_tick(flat)

    assert RecoveryStore(path).load() == {"house.desk_lamp/power": 1, "house.desk_lamp/brightness": 50}


def test_on_tick_skips_pairs_whose_device_no_longer_exists(tmp_path):
    flat = _house()
    path = tmp_path / "r.yaml"
    instance = configure(
        {"selectors": ["house.desk_lamp/power", "house.thermostat/setpoint"], "path": str(path)},
        flat, "recovery.main")
    lamp = flat["house.desk_lamp"]
    thermostat = flat["house.thermostat"]
    _commit(lamp, "power", 1)
    _commit(thermostat, "setpoint", 21.0)

    devices_without_thermostat = {k: v for k, v in flat.items() if k != "house.thermostat"}
    instance.on_tick(devices_without_thermostat)  # must not raise

    assert RecoveryStore(path).load() == {"house.desk_lamp/power": 1}


def test_on_tick_omits_a_pair_whose_value_is_still_none(tmp_path):
    flat = _house()
    path = tmp_path / "r.yaml"
    instance = configure(
        {"selectors": ["house.desk_lamp/power", "house.desk_lamp/brightness"], "path": str(path)},
        flat, "recovery.main")
    lamp = flat["house.desk_lamp"]

    _commit(lamp, "power", 1)  # brightness never committed -- stays None
    instance.on_tick(flat)

    assert RecoveryStore(path).load() == {"house.desk_lamp/power": 1}


# ---------- on_start() ----------

def test_on_start_restores_persisted_values(tmp_path):
    flat = _house()
    path = tmp_path / "r.yaml"
    RecoveryStore(path).write({"house.desk_lamp/power": 1})
    instance = configure({"selectors": ["house.desk_lamp/power"], "path": str(path)}, flat, "recovery.main")
    lamp = flat["house.desk_lamp"]
    assert lamp.get(name="power") is None

    asyncio.run(instance.on_start(flat))
    fetch_sync(lamp)
    lamp.update_state()

    assert lamp.get(name="power") == 1


def test_on_start_treats_missing_file_as_nothing_to_recover(tmp_path):
    flat = _house()
    path = tmp_path / "r.yaml"
    instance = configure({"selectors": ["house.desk_lamp/power"], "path": str(path)}, flat, "recovery.main")
    lamp = flat["house.desk_lamp"]

    asyncio.run(instance.on_start(flat))
    fetch_sync(lamp)
    lamp.update_state()

    assert lamp.get(name="power") is None


def test_on_start_treats_corrupt_file_as_nothing_to_recover(tmp_path, recovery_log):
    flat = _house()
    path = tmp_path / "r.yaml"
    path.write_text("a: [unterminated")
    instance = configure({"selectors": ["house.desk_lamp/power"], "path": str(path)}, flat, "recovery.main")
    lamp = flat["house.desk_lamp"]

    asyncio.run(instance.on_start(flat))

    assert lamp.get(name="power") is None
    assert str(path) in recovery_log.text


def test_on_start_skips_missing_device_and_logs_warning(tmp_path, recovery_log):
    flat = _house()
    path = tmp_path / "r.yaml"
    RecoveryStore(path).write({"house.nonexistent/power": 1})
    instance = configure({"selectors": ["house.desk_lamp/power"], "path": str(path)}, flat, "recovery.main")

    asyncio.run(instance.on_start(flat))

    assert "house.nonexistent/power" in recovery_log.text


def test_on_start_skips_missing_endpoint_and_logs_warning(tmp_path, recovery_log):
    flat = _house()
    path = tmp_path / "r.yaml"
    RecoveryStore(path).write({"house.desk_lamp/no_longer_exists": 1})
    instance = configure({"selectors": ["house.desk_lamp/power"], "path": str(path)}, flat, "recovery.main")

    asyncio.run(instance.on_start(flat))

    assert "house.desk_lamp/no_longer_exists" in recovery_log.text


def test_on_start_skips_now_non_writable_endpoint_and_logs_warning(tmp_path, recovery_log):
    flat = _house()
    path = tmp_path / "r.yaml"
    RecoveryStore(path).write({"house.desk_lamp/battery_level": 50})
    instance = configure({"selectors": ["house.desk_lamp/power"], "path": str(path)}, flat, "recovery.main")

    asyncio.run(instance.on_start(flat))

    assert "house.desk_lamp/battery_level" in recovery_log.text


def test_on_start_isolates_one_bad_entry_from_the_rest(tmp_path, monkeypatch, recovery_log):
    flat = _house()
    path = tmp_path / "r.yaml"
    RecoveryStore(path).write({
        "house.desk_lamp/power": 1,
        "house.thermostat/setpoint": 21.5,
    })
    instance = configure(
        {"selectors": ["house.desk_lamp/power", "house.thermostat/setpoint"], "path": str(path)},
        flat, "recovery.main")
    lamp = flat["house.desk_lamp"]
    thermostat = flat["house.thermostat"]

    def _boom(value, name=None):
        raise ValueError("boom")

    monkeypatch.setattr(lamp, "set", _boom)

    asyncio.run(instance.on_start(flat))

    assert "house.desk_lamp/power" in recovery_log.text  # failure logged, not raised
    fetch_sync(thermostat)
    thermostat.update_state()
    assert thermostat.get(name="setpoint") == 21.5  # second entry still restored


def test_on_start_logs_restored_and_skipped_counts(tmp_path, recovery_log):
    flat = _house()
    path = tmp_path / "r.yaml"
    RecoveryStore(path).write({
        "house.desk_lamp/power": 1,
        "house.nonexistent/x": 1,
    })
    instance = configure({"selectors": ["house.desk_lamp/power"], "path": str(path)}, flat, "recovery.main")

    asyncio.run(instance.on_start(flat))

    assert "restored 1" in recovery_log.text
    assert "skipped 1" in recovery_log.text


# ---------- on_stop() ----------

def test_on_stop_writes_current_values_unconditionally(tmp_path):
    flat = _house()
    path = tmp_path / "r.yaml"
    instance = configure({"selectors": ["house.desk_lamp/power"], "path": str(path)}, flat, "recovery.main")
    lamp = flat["house.desk_lamp"]
    _commit(lamp, "power", 5)  # deliberately no on_tick() call first

    asyncio.run(instance.on_stop(flat))

    assert RecoveryStore(path).load() == {"house.desk_lamp/power": 5}


# ---------- end-to-end via load_system() ----------

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples"


def test_load_system_collects_on_start_on_tick_on_stop_from_recovery_instance(tmp_path):
    discover_extensions()
    path = tmp_path / "recovery.yaml"
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text(f"""
heartbeat: 1s
devices:
  - id: lamp
    module: virtual
    endpoints:
      - key: power
        writable: true
        type: int
        default: 0

extensions:
  recovery:
    main:
      selectors: ["lamp/power"]
      path: "{path.as_posix()}"
""")
    system = load_system(system_yaml)

    assert len(system.start_hooks) == 1
    assert len(system.tick_hooks) == 1
    assert len(system.stop_hooks) == 1


def test_end_to_end_crash_and_restart_restores_last_known_state(tmp_path):
    discover_extensions()
    path = tmp_path / "recovery.yaml"
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text(f"""
heartbeat: 1s
devices:
  - id: house
    module: host
    children:
      - id: lamp
        module: virtual
        update: 1s
        endpoints:
          - key: power
            writable: true
            type: int
            default: 0

extensions:
  recovery:
    main:
      selectors: ["house.lamp/power"]
      path: "{path.as_posix()}"
""")
    system1 = load_system(system_yaml)
    asyncio.run(system1.start_hooks[0](system1.devices))  # first run: nothing to recover yet

    scheduler1 = Scheduler(system1.devices, tasks=system1.tasks, heartbeat=system1.heartbeat,
                            tick_hooks=system1.tick_hooks)
    system1.devices["house"].child("lamp").set(42, name="power")
    scheduler1.tick(now=0.0)  # commits power=42; on_tick persists it to disk

    # Simulate a crash: stop_hooks are deliberately never run -- the
    # persisted value must have come from on_tick, not a graceful shutdown.

    system2 = load_system(system_yaml)  # fresh device tree; power back to default: 0
    asyncio.run(system2.start_hooks[0](system2.devices))
    lamp2 = system2.devices["house"].child("lamp")
    fetch_sync(lamp2)
    lamp2.update_state()

    assert lamp2.get(name="power") == 42  # restored, not the hardcoded default: 0


def test_end_to_end_final_persist_on_graceful_stop(tmp_path):
    discover_extensions()
    path = tmp_path / "recovery.yaml"
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text(f"""
heartbeat: 1s
devices:
  - id: house
    module: host
    children:
      - id: lamp
        module: virtual
        update: 1s
        endpoints:
          - key: power
            writable: true
            type: int
            default: 0

extensions:
  recovery:
    main:
      selectors: ["house.lamp/power"]
      path: "{path.as_posix()}"
""")
    system1 = load_system(system_yaml)
    asyncio.run(system1.start_hooks[0](system1.devices))

    scheduler1 = Scheduler(system1.devices, tasks=system1.tasks, heartbeat=system1.heartbeat,
                            tick_hooks=system1.tick_hooks)
    system1.devices["house"].child("lamp").set(7, name="power")
    scheduler1.tick(now=0.0)
    asyncio.run(system1.stop_hooks[0](system1.devices))  # graceful shutdown, final persist

    system2 = load_system(system_yaml)
    asyncio.run(system2.start_hooks[0](system2.devices))
    lamp2 = system2.devices["house"].child("lamp")
    fetch_sync(lamp2)
    lamp2.update_state()

    assert lamp2.get(name="power") == 7
