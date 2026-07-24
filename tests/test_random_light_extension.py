"""Tests for extensions.random_light.extension: configure() validation,
RandomLightInstance.apply()'s gating/write-diffing, RandomLightAction, and
an end-to-end test through core.config.load_system(). The pure algorithm
itself is tested in tests/test_random_light.py."""

from datetime import datetime

import pytest

from core.config import ConfigError, load_system
from core.endpoint import Endpoint
from core.registry import discover_extensions
from core.scheduler import Scheduler
from devices.virtual.device import VirtualDevice
from extensions.random_light.extension import RandomLightAction, configure
from tests.conftest import fetch_sync


def _commit(device, endpoint_key, value):
    device.set(value, name=endpoint_key)
    fetch_sync(device)
    device.update_state()


def _devices():
    hallway_light = VirtualDevice("hallway_light", endpoints=[
        Endpoint("state", writable=True, value_type="int", values={0: "off", 1: "on"}),
    ])
    porch_light = VirtualDevice("porch_light", endpoints=[
        Endpoint("state", writable=True, value_type="int", values={0: "off", 1: "on"}),
    ])
    surveillance = VirtualDevice("surveillance", endpoints=[Endpoint("armed", writable=True, value_type="int")])
    alarm = VirtualDevice("alarm", endpoints=[Endpoint("state", writable=True, value_type="int")])
    return {"hallway_light": hallway_light, "porch_light": porch_light,
            "surveillance": surveillance, "alarm": alarm}


def _light_entry(**overrides):
    entry = {"device": "hallway_light.state", "windows": [{"start": "00:00", "end": "23:59"}]}
    entry.update(overrides)
    return entry


# ---------- configure() ----------

def test_configure_builds_targets():
    flat = _devices()
    instance = configure({"lights": [_light_entry()]}, flat, "random_light.house")
    assert instance._targets == {"hallway_light.state": ("hallway_light", "state")}


def test_configure_applies_defaults_when_omitted():
    flat = _devices()
    instance = configure({"lights": [_light_entry()]}, flat, "random_light.house")
    light = instance._controller._lights["hallway_light.state"]
    assert light.min_interval == 1800.0  # "30m"
    assert light.probability_on == 0.5
    assert light.is_default is False


def test_configure_missing_lights_raises():
    flat = _devices()
    with pytest.raises(ConfigError):
        configure({}, flat, "random_light.house")


def test_configure_unknown_device_raises():
    flat = _devices()
    with pytest.raises(ConfigError):
        configure({"lights": [_light_entry(device="nonexistent.state")]}, flat, "random_light.house")


def test_configure_non_writable_endpoint_raises():
    flat = _devices()
    flat["hallway_light"].endpoint("state").writable = False
    with pytest.raises(ConfigError):
        configure({"lights": [_light_entry()]}, flat, "random_light.house")


def test_configure_missing_windows_raises():
    flat = _devices()
    with pytest.raises(ConfigError):
        configure({"lights": [{"device": "hallway_light.state"}]}, flat, "random_light.house")


def test_configure_empty_windows_raises():
    flat = _devices()
    with pytest.raises(ConfigError):
        configure({"lights": [_light_entry(windows=[])]}, flat, "random_light.house")


def test_configure_out_of_range_probability_raises():
    flat = _devices()
    with pytest.raises(ConfigError):
        configure({"lights": [_light_entry(probability_on=1.5)]}, flat, "random_light.house")


def test_configure_duplicate_light_raises():
    flat = _devices()
    with pytest.raises(ConfigError):
        configure({"lights": [_light_entry(), _light_entry()]}, flat, "random_light.house")


def test_configure_no_sun_device_needed_for_fixed_only_windows():
    flat = _devices()  # no "sun" device present at all
    instance = configure({"lights": [_light_entry()]}, flat, "random_light.house")
    assert instance._sun_device_id is None


def test_configure_sun_anchored_window_requires_sun_device():
    flat = _devices()
    with pytest.raises(ConfigError):
        configure({"lights": [_light_entry(windows=[{"start": "sunrise-18m", "end": "sunset+12m"}])]},
                  flat, "random_light.house")


def test_configure_sun_anchored_window_ok_when_sun_device_present():
    flat = _devices()
    flat["sun"] = VirtualDevice("sun", endpoints=[Endpoint("sunrise"), Endpoint("sunset")])
    instance = configure({"lights": [_light_entry(windows=[{"start": "sunrise-18m", "end": "sunset+12m"}])]},
                         flat, "random_light.house")
    assert instance._sun_device_id == "sun"


def test_configure_enable_ref_unknown_device_raises():
    flat = _devices()
    with pytest.raises(ConfigError):
        configure({"lights": [_light_entry()], "enable_ref": "nonexistent.state"}, flat, "random_light.house")


def test_configure_pause_ref_unknown_device_raises():
    flat = _devices()
    with pytest.raises(ConfigError):
        configure({"lights": [_light_entry()], "pause_ref": "nonexistent.state"}, flat, "random_light.house")


def test_configure_enable_and_pause_ref_none_when_omitted():
    flat = _devices()
    instance = configure({"lights": [_light_entry()]}, flat, "random_light.house")
    assert instance._enable_ref is None
    assert instance._pause_ref is None


# ---------- RandomLightInstance.apply() ----------

def test_apply_no_op_when_paused():
    flat = _devices()
    instance = configure({"lights": [_light_entry(probability_on=1.0)], "pause_ref": "alarm.state"},
                         flat, "random_light.house")
    _commit(flat["hallway_light"], "state", 0)
    _commit(flat["alarm"], "state", 1)
    instance.apply(flat)
    assert flat["hallway_light"]._pending == {}


def test_apply_no_op_when_not_enabled():
    flat = _devices()
    instance = configure({"lights": [_light_entry(probability_on=1.0)], "enable_ref": "surveillance.armed"},
                         flat, "random_light.house")
    _commit(flat["hallway_light"], "state", 0)
    _commit(flat["surveillance"], "armed", 0)
    instance.apply(flat)
    assert flat["hallway_light"]._pending == {}


def test_apply_writes_only_changed_lights():
    flat = _devices()
    instance = configure({"lights": [
        _light_entry(device="hallway_light.state", probability_on=1.0),
        _light_entry(device="porch_light.state", probability_on=0.0),
    ]}, flat, "random_light.house")
    _commit(flat["hallway_light"], "state", 0)
    _commit(flat["porch_light"], "state", 0)  # already at its target -- must not be rewritten
    instance.apply(flat)
    assert flat["hallway_light"]._pending == {"state": 1}
    assert flat["porch_light"]._pending == {}


def test_apply_force_bypasses_gating():
    flat = _devices()
    instance = configure({"lights": [_light_entry()], "enable_ref": "surveillance.armed",
                          "pause_ref": "alarm.state"}, flat, "random_light.house")
    _commit(flat["hallway_light"], "state", 1)
    _commit(flat["surveillance"], "armed", 0)
    _commit(flat["alarm"], "state", 1)
    instance.apply(flat, force=0)
    assert flat["hallway_light"]._pending == {"state": 0}


# ---------- RandomLightAction ----------

def test_random_light_action_requires_device_is_false():
    assert RandomLightAction.requires_device is False


def test_random_light_action_unknown_instance_raises():
    with pytest.raises(ConfigError):
        RandomLightAction(instance="random_light.nonexistent", extensions={})


def test_random_light_action_invalid_force_raises():
    flat = _devices()
    instance = configure({"lights": [_light_entry()]}, flat, "random_light.house")
    with pytest.raises(ConfigError):
        RandomLightAction(instance="random_light.house", extensions={"random_light.house": instance}, force=2)


def test_random_light_action_perform_delegates_to_instance_apply():
    flat = _devices()
    instance = configure({"lights": [_light_entry(probability_on=1.0)]}, flat, "random_light.house")
    action = RandomLightAction(instance="random_light.house", extensions={"random_light.house": instance})
    _commit(flat["hallway_light"], "state", 0)
    action.perform(flat)
    assert flat["hallway_light"]._pending == {"state": 1}


# ---------- end-to-end via load_system() ----------

def test_end_to_end_load_system_randomizes_light(tmp_path, monkeypatch):
    discover_extensions()
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: hallway_light
    module: virtual
    update: 1s
    endpoints:
      - key: state
        writable: true
        type: int
        default: 0

extensions:
  random_light:
    house:
      lights:
        - device: "hallway_light.state"
          windows: [{ start: "00:00", end: "23:59" }]
          probability_on: 1.0

tasks:
  - tag: random_light_tick
    time: "+1s"
    repeat: 30s
    action: { kind: random_light, instance: "random_light.house" }
""")
    system = load_system(system_yaml)
    # "time: +1s"/"repeat: 30s" is wall-clock-relative -- pin due_time
    # directly, same technique tests/test_logdb_extension.py's end-to-end
    # test uses, to decouple task scheduling from real time.
    task = next(t for t in system.tasks if t.tag == "random_light_tick")
    task.due_time = 30.0
    task.repeat = 0.0

    fixed_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    monkeypatch.setattr("extensions.random_light.extension.time.time", lambda: fixed_now)

    scheduler = Scheduler(system.devices, tasks=system.tasks, heartbeat=system.heartbeat,
                          tick_hooks=system.tick_hooks)
    scheduler.tick(now=30.0)

    light = system.devices["hallway_light"]
    fetch_sync(light)
    light.update_state()
    assert light.get() == 1
