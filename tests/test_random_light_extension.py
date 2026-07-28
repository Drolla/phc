"""Tests for extensions.random_light.extension: configure()'s validation
and extension-default/instance/per-light cascade, RandomLightInstance.apply()'s
gating/write-diffing, RandomLightAction, and an end-to-end test through
core.config.load_system(). The pure algorithm itself is tested in
tests/test_random_light.py."""

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
    entry = {"device": "hallway_light.state"}
    entry.update(overrides)
    return entry


def _base_params(**overrides):
    """A complete params dict, as core.config._merge_extension_params would
    hand to configure() -- every declared parameter present, instance-set
    or extension.yaml-defaulted. Direct configure() calls in these tests
    bypass _merge_extension_params, so (like tests/test_logdb_extension.py)
    they must supply the full dict themselves."""
    params = {
        "lights": [_light_entry()],
        "windows": [{"start": "00:00", "end": "23:59"}],
        "min_interval": "30m",
        "probability_on": 0.5,
        "sun_device": "sun",
        "enable_ref": None,
        "pause_ref": None,
    }
    params.update(overrides)
    return params


# ---------- configure(): basics ----------

def test_configure_builds_targets():
    flat = _devices()
    instance = configure(_base_params(), flat, "random_light.house")
    assert instance._targets == {"hallway_light.state": ("hallway_light", "state")}


def test_configure_missing_lights_raises():
    flat = _devices()
    params = _base_params()
    del params["lights"]
    with pytest.raises(ConfigError):
        configure(params, flat, "random_light.house")


def test_configure_unknown_device_raises():
    flat = _devices()
    params = _base_params(lights=[_light_entry(device="nonexistent.state")])
    with pytest.raises(ConfigError):
        configure(params, flat, "random_light.house")


def test_configure_non_writable_endpoint_raises():
    flat = _devices()
    flat["hallway_light"].endpoint("state").writable = False
    with pytest.raises(ConfigError):
        configure(_base_params(), flat, "random_light.house")


def test_configure_empty_windows_raises():
    # An explicit empty list (as opposed to omitting `windows` entirely)
    # is still an error, whether given per-light or as the instance default.
    flat = _devices()
    with pytest.raises(ConfigError):
        configure(_base_params(lights=[_light_entry(windows=[])]), flat, "random_light.house")


def test_configure_instance_default_empty_windows_raises():
    flat = _devices()
    with pytest.raises(ConfigError):
        configure(_base_params(windows=[]), flat, "random_light.house")


def test_configure_out_of_range_probability_raises():
    flat = _devices()
    params = _base_params(lights=[_light_entry(probability_on=1.5)])
    with pytest.raises(ConfigError):
        configure(params, flat, "random_light.house")


def test_configure_instance_default_out_of_range_probability_raises():
    flat = _devices()
    with pytest.raises(ConfigError):
        configure(_base_params(probability_on=1.5), flat, "random_light.house")


def test_configure_duplicate_light_raises():
    flat = _devices()
    params = _base_params(lights=[_light_entry(), _light_entry()])
    with pytest.raises(ConfigError):
        configure(params, flat, "random_light.house")


# ---------- configure(): extension default / instance / per-light cascade ----------

def test_configure_light_uses_instance_values_when_it_omits_all_three():
    flat = _devices()
    instance = configure(_base_params(), flat, "random_light.house")
    light = instance._controller._lights["hallway_light.state"]
    assert light.windows[0][0].hour == 0 and light.windows[0][0].minute == 0  # "00:00", the instance default
    assert light.min_interval == 1800.0  # "30m"
    assert light.probability_on == 0.5
    assert light.is_default is False


def test_configure_light_overrides_instance_windows():
    flat = _devices()
    params = _base_params(lights=[_light_entry(windows=[{"start": "10:00", "end": "12:00"}])])
    instance = configure(params, flat, "random_light.house")
    light = instance._controller._lights["hallway_light.state"]
    assert len(light.windows) == 1
    assert light.windows[0][0].hour == 10
    assert light.windows[0][1].hour == 12


def test_configure_light_overrides_instance_min_interval_and_probability():
    flat = _devices()
    params = _base_params(lights=[_light_entry(min_interval="5m", probability_on=0.9)])
    instance = configure(params, flat, "random_light.house")
    light = instance._controller._lights["hallway_light.state"]
    assert light.min_interval == 300.0
    assert light.probability_on == 0.9


def test_configure_light_uses_instance_windows_when_instance_overrides_extension_default():
    flat = _devices()
    params = _base_params(windows=[{"start": "01:00", "end": "02:00"}])
    instance = configure(params, flat, "random_light.house")
    light = instance._controller._lights["hallway_light.state"]
    assert light.windows[0][0].hour == 1
    assert light.windows[0][1].hour == 2


# ---------- configure(): lazy sun_device requirement ----------

def test_configure_no_sun_device_needed_for_fixed_only_windows():
    flat = _devices()  # no "sun" device present at all
    instance = configure(_base_params(), flat, "random_light.house")
    assert instance._sun_device_id is None


def test_configure_light_sun_anchored_window_requires_sun_device():
    flat = _devices()
    params = _base_params(lights=[_light_entry(windows=[{"start": "sunrise-18m", "end": "sunset+12m"}])])
    with pytest.raises(ConfigError):
        configure(params, flat, "random_light.house")


def test_configure_light_sun_anchored_window_ok_when_sun_device_present():
    flat = _devices()
    flat["sun"] = VirtualDevice("sun", endpoints=[Endpoint("sunrise"), Endpoint("sunset")])
    params = _base_params(lights=[_light_entry(windows=[{"start": "sunrise-18m", "end": "sunset+12m"}])])
    instance = configure(params, flat, "random_light.house")
    assert instance._sun_device_id == "sun"


def test_configure_instance_default_sun_anchored_requires_sun_device():
    # No light overrides windows -- the instance-level default itself uses
    # a sun anchor, so the requirement must still be detected.
    flat = _devices()
    params = _base_params(windows=[{"start": "sunrise-18m", "end": "sunset+12m"}])
    with pytest.raises(ConfigError):
        configure(params, flat, "random_light.house")


def test_configure_instance_default_sun_anchored_ok_when_sun_device_present():
    flat = _devices()
    flat["sun"] = VirtualDevice("sun", endpoints=[Endpoint("sunrise"), Endpoint("sunset")])
    params = _base_params(windows=[{"start": "sunrise-18m", "end": "sunset+12m"}])
    instance = configure(params, flat, "random_light.house")
    assert instance._sun_device_id == "sun"


# ---------- configure(): enable_ref / pause_ref ----------

def test_configure_enable_ref_unknown_device_raises():
    flat = _devices()
    with pytest.raises(ConfigError):
        configure(_base_params(enable_ref="nonexistent.state"), flat, "random_light.house")


def test_configure_pause_ref_unknown_device_raises():
    flat = _devices()
    with pytest.raises(ConfigError):
        configure(_base_params(pause_ref="nonexistent.state"), flat, "random_light.house")


def test_configure_enable_and_pause_ref_none_when_omitted():
    flat = _devices()
    instance = configure(_base_params(), flat, "random_light.house")
    assert instance._enable_ref is None
    assert instance._pause_ref is None


# ---------- RandomLightInstance.apply() ----------

def test_apply_no_op_when_paused():
    flat = _devices()
    params = _base_params(lights=[_light_entry(probability_on=1.0)], pause_ref="alarm.state")
    instance = configure(params, flat, "random_light.house")
    _commit(flat["hallway_light"], "state", 0)
    _commit(flat["alarm"], "state", 1)
    instance.apply(flat)
    assert flat["hallway_light"]._pending == {}


def test_apply_no_op_when_not_enabled():
    flat = _devices()
    params = _base_params(lights=[_light_entry(probability_on=1.0)], enable_ref="surveillance.armed")
    instance = configure(params, flat, "random_light.house")
    _commit(flat["hallway_light"], "state", 0)
    _commit(flat["surveillance"], "armed", 0)
    instance.apply(flat)
    assert flat["hallway_light"]._pending == {}


def test_apply_writes_only_changed_lights():
    flat = _devices()
    params = _base_params(lights=[
        _light_entry(device="hallway_light.state", probability_on=1.0),
        _light_entry(device="porch_light.state", probability_on=0.0),
    ])
    instance = configure(params, flat, "random_light.house")
    _commit(flat["hallway_light"], "state", 0)
    _commit(flat["porch_light"], "state", 0)  # already at its target -- must not be rewritten
    instance.apply(flat)
    assert flat["hallway_light"]._pending == {"state": 1}
    assert flat["porch_light"]._pending == {}


def test_apply_force_bypasses_gating():
    flat = _devices()
    params = _base_params(enable_ref="surveillance.armed", pause_ref="alarm.state")
    instance = configure(params, flat, "random_light.house")
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
    instance = configure(_base_params(), flat, "random_light.house")
    with pytest.raises(ConfigError):
        RandomLightAction(instance="random_light.house", extensions={"random_light.house": instance}, force=2)


def test_random_light_action_perform_delegates_to_instance_apply():
    flat = _devices()
    params = _base_params(lights=[_light_entry(probability_on=1.0)])
    instance = configure(params, flat, "random_light.house")
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


def test_end_to_end_load_system_light_falls_back_to_extension_default_windows(tmp_path, monkeypatch):
    """A light omitting windows/min_interval/probability_on entirely picks
    up extension.yaml's own defaults (never overridden by the instance
    block here) -- the tier-1 fallback, exercised through the real
    _merge_extension_params pathway (unlike the direct configure() tests
    above)."""
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
  - id: sun
    module: sun
    latitude: 47.3769
    longitude: 8.5417
    timezone: Europe/Zurich

extensions:
  random_light:
    house:
      lights:
        - device: "hallway_light.state"

tasks:
  - tag: random_light_tick
    time: "+1s"
    action: { kind: random_light, instance: "random_light.house" }
""")
    system = load_system(system_yaml)
    instance = next(t for t in system.tasks if t.tag == "random_light_tick").actions[0]._instance
    light = instance._controller._lights["hallway_light.state"]
    assert light.min_interval == 1800.0  # extension.yaml's own default, "30m"
    assert light.probability_on == 0.5
    assert len(light.windows) == 2  # extension.yaml's own default: sunset-1h/23:00 and 06:30/sunrise+30m
