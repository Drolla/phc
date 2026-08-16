"""Tests for phc.extensions.timer.extension: configure()'s selector resolution/
validation, TimerInstance's on_bind/on_tick hook wiring and CRUD API, and
an end-to-end test through phc.core.config.load_system() and Scheduler.tick().
Pure timer data/persistence is tested in tests/test_timer.py."""

import logging
import time
from pathlib import Path

import pytest

from phc.core.config import ConfigError, load_system
from phc.core.endpoint import Endpoint
from phc.core.registry import discover_extensions
from phc.core.scheduler import Scheduler
from phc.devices.virtual.device import VirtualDevice
from phc.extensions.timer.extension import configure
from phc.extensions.timer.timer import TimerDef, TimerStore
from tests.conftest import fetch_sync


def _commit(device, endpoint_key, value):
    device.set(value, name=endpoint_key)
    fetch_sync(device)
    device.update_state()


def _house():
    lamp = VirtualDevice("desk_lamp", endpoints=[
        Endpoint("power", writable=True, value_type="int", values={0: "off", 1: "on"}),
        Endpoint("brightness", writable=True, value_type="int", min=0, max=100),
        Endpoint("battery_level", writable=False, value_type="int"),
    ], parent_qualified_id="house")
    house = VirtualDevice("house", children=[lamp])
    return {"house": house, "house.desk_lamp": lamp}


class FakeSystem:
    """Stand-in for phc.core.config.System: TimerInstance.on_bind only ever
    reads .devices/.tasks/.extensions off whatever it's given -- see
    tests/test_debug_portal_extension.py's own FakeSystem for the same
    pattern."""

    def __init__(self, devices, tasks=None, extensions=None):
        from phc.core.config import _build_task
        from phc.core.task import TaskRegistry

        self.devices = devices
        self.extensions = extensions if extensions is not None else {}
        # A real TaskRegistry with a real builder, exactly as load_system
        # wires one: TimerInstance mirrors each timer into a live Task
        # through it, so a bare list would not exercise the real path.
        self.tasks = tasks if isinstance(tasks, TaskRegistry) else TaskRegistry(
            tasks, build_task=_build_task, flat=devices, extensions=self.extensions)


@pytest.fixture
def timer_log(caplog):
    """caplog, but attached directly to the "phc.timer" logger, bypassing
    propagate=False set by configure_logging() -- see
    tests/test_recovery_extension.py's recovery_log fixture for the same
    pattern. Set at INFO since some tests assert on the on_bind summary line."""
    timer_logger = logging.getLogger("phc.timer")
    timer_logger.addHandler(caplog.handler)
    timer_logger.setLevel(logging.INFO)
    try:
        with caplog.at_level("INFO", logger="phc.timer"):
            yield caplog
    finally:
        timer_logger.removeHandler(caplog.handler)


def _configure(flat, tmp_path, **overrides):
    params = {
        "path": str(tmp_path / "timers.yaml"),
        # battery_level (see _house()) is deliberately excluded here -- a
        # wildcard covering it would be rejected (every matched endpoint
        # must be writable, see test_configure_raises_config_error_when_
        # selector_matches_non_writable_endpoint below).
        "selectors": ["house.desk_lamp/power", "house.desk_lamp/brightness"],
        "catch_up": "5m",
    }
    params.update(overrides)
    return configure(params, flat, "timer.house")


def _bind(instance, flat, tasks=None, extensions=None):
    system = FakeSystem(flat, tasks=tasks, extensions=extensions)
    instance.on_bind(system)
    return system


def _stored_timer(**overrides):
    """A TimerDef with sensible defaults, for pre-seeding a TimerStore file
    directly (bypassing TimerInstance's own add_timer()) -- mirrors
    tests/test_timer.py's own _timer() helper."""
    fields = {
        "id": 1,
        "time": time.time() + 3600,
        "device": "house.desk_lamp",
        "endpoint": "power",
        "action": "set",
        "value": "on",
        "repeat": None,
        "description": "test timer",
        "enabled": True,
    }
    fields.update(overrides)
    return TimerDef(**fields)


# ---------- configure() ----------

def test_configure_resolves_selectors_to_target_pairs(tmp_path):
    flat = _house()
    instance = _configure(flat, tmp_path)
    assert instance.target_pairs == [("house.desk_lamp", "brightness"), ("house.desk_lamp", "power")]


def test_configure_raises_config_error_when_selector_matches_non_writable_endpoint(tmp_path):
    flat = _house()
    with pytest.raises(ConfigError, match="battery_level"):
        _configure(flat, tmp_path, selectors=["house.desk_lamp/battery_level"])


def test_configure_raises_config_error_when_selectors_match_zero_endpoints(tmp_path):
    flat = _house()
    with pytest.raises(ConfigError):
        _configure(flat, tmp_path, selectors=["nonexistent.device/*"])


# ---------- on_bind(): restore ----------

def test_on_bind_registers_a_task_per_persisted_timer(tmp_path):
    flat = _house()
    path = tmp_path / "timers.yaml"
    TimerStore(path).write(2, [_stored_timer(id=1, description="evening")])
    instance = _configure(flat, tmp_path)
    system = _bind(instance, flat)

    assert [t.tag for t in system.tasks] == ["timer.house.1"]
    assert [t.id for t in instance.list_timers()] == [1]


def test_on_bind_drops_one_shot_missed_beyond_catch_up_and_logs(tmp_path, timer_log):
    flat = _house()
    path = tmp_path / "timers.yaml"
    TimerStore(path).write(2, [_stored_timer(id=1, time=time.time() - 600, description="stale")])
    instance = _configure(flat, tmp_path, catch_up="5m")
    system = _bind(instance, flat)

    assert list(system.tasks) == []
    assert instance.list_timers() == []
    assert "dropping expired" in timer_log.text


def test_on_bind_keeps_one_shot_missed_within_catch_up(tmp_path):
    flat = _house()
    path = tmp_path / "timers.yaml"
    TimerStore(path).write(2, [_stored_timer(id=1, time=time.time() - 30, description="recent")])
    instance = _configure(flat, tmp_path, catch_up="5m")
    system = _bind(instance, flat)

    assert [t.tag for t in system.tasks] == ["timer.house.1"]


def test_on_bind_never_drops_a_repeating_timer_regardless_of_age(tmp_path):
    flat = _house()
    path = tmp_path / "timers.yaml"
    TimerStore(path).write(2, [
        _stored_timer(id=1, time=time.time() - 999999, repeat="1D", description="daily"),
    ])
    instance = _configure(flat, tmp_path, catch_up="5m")
    system = _bind(instance, flat)

    assert [t.tag for t in system.tasks] == ["timer.house.1"]


def test_on_bind_does_not_register_a_disabled_timer(tmp_path):
    flat = _house()
    path = tmp_path / "timers.yaml"
    TimerStore(path).write(2, [_stored_timer(id=1, description="off", enabled=False)])
    instance = _configure(flat, tmp_path)
    system = _bind(instance, flat)

    assert list(system.tasks) == []
    assert [t.id for t in instance.list_timers()] == [1]


def test_on_bind_persists_after_dropping_expired_timer(tmp_path):
    flat = _house()
    path = tmp_path / "timers.yaml"
    TimerStore(path).write(2, [_stored_timer(id=1, time=time.time() - 600, description="stale")])
    instance = _configure(flat, tmp_path, catch_up="5m")
    _bind(instance, flat)

    next_id, timers = TimerStore(path).load()
    assert timers == []


# ---------- on_tick(): reconcile ----------

def test_on_tick_removes_timer_whose_one_shot_task_was_retired(tmp_path):
    flat = _house()
    instance = _configure(flat, tmp_path)
    system = _bind(instance, flat)
    t = instance.add_timer(time_spec=str(int(time.time() - 1)), device="house.desk_lamp",
                            endpoint="power", action="set", value="on")
    assert t.id in {x.id for x in instance.list_timers()}

    # Simulate the Scheduler retiring the one-shot Task after it fired
    # (phc.core.scheduler.Scheduler._tick_async pass 2 removes a finished task).
    system.tasks.remove(system.tasks.by_tag(t.tag("timer.house")))
    instance.on_tick(flat)

    assert instance.list_timers() == []


def test_on_tick_mirrors_forward_a_repeating_timers_advanced_due_time(tmp_path):
    flat = _house()
    instance = _configure(flat, tmp_path)
    system = _bind(instance, flat)
    t = instance.add_timer(time_spec=str(int(time.time() - 1)), device="house.desk_lamp",
                            endpoint="power", action="toggle", repeat_spec="2s")
    task = next(x for x in system.tasks if x.tag == t.tag("timer.house"))
    task.due_time += 100.0  # simulate the Task having fired and rearmed

    instance.on_tick(flat)

    assert instance.get_timer(t.id).time == task.due_time
    _, persisted = TimerStore(Path(instance.store.path)).load()
    assert persisted[0].time == task.due_time


def test_on_tick_does_not_persist_when_nothing_changed(tmp_path, monkeypatch):
    flat = _house()
    instance = _configure(flat, tmp_path)
    _bind(instance, flat)
    instance.add_timer(time_spec=str(int(time.time() + 3600)), device="house.desk_lamp",
                        endpoint="power", action="set", value="on")
    calls = []
    monkeypatch.setattr(instance.store, "write", lambda next_id, timers: calls.append(timers))

    instance.on_tick(flat)

    assert calls == []


def test_on_tick_skips_disabled_timers(tmp_path):
    flat = _house()
    instance = _configure(flat, tmp_path)
    system = _bind(instance, flat)
    t = instance.add_timer(time_spec=str(int(time.time() + 3600)), device="house.desk_lamp",
                            endpoint="power", action="set", value="on", enabled=False)

    instance.on_tick(flat)  # must not raise despite no Task existing for `t`

    assert [x.id for x in instance.list_timers()] == [t.id]


# ---------- CRUD API ----------

def test_add_timer_registers_a_task_and_persists(tmp_path):
    flat = _house()
    instance = _configure(flat, tmp_path)
    system = _bind(instance, flat)

    t = instance.add_timer(time_spec=str(int(time.time() + 3600)), device="house.desk_lamp",
                            endpoint="power", action="set", value="on", repeat_spec="1D",
                            description="evening")

    assert t.id == 1
    assert [x.tag for x in system.tasks] == ["timer.house.1"]
    _, persisted = TimerStore(Path(instance.store.path)).load()
    assert [p.id for p in persisted] == [1]


def test_add_timer_rejects_target_outside_configured_selectors(tmp_path):
    flat = _house()
    instance = _configure(flat, tmp_path, selectors=["house.desk_lamp/power"])
    _bind(instance, flat)

    with pytest.raises(ValueError, match="brightness"):
        instance.add_timer(time_spec=str(int(time.time() + 60)), device="house.desk_lamp",
                            endpoint="brightness", action="set", value="50")


def test_add_timer_rejects_invalid_value_for_target_endpoint(tmp_path):
    flat = _house()
    instance = _configure(flat, tmp_path)
    _bind(instance, flat)

    with pytest.raises(ValueError):
        instance.add_timer(time_spec=str(int(time.time() + 60)), device="house.desk_lamp",
                            endpoint="brightness", action="set", value="not-a-number")


def test_add_timer_rejects_invalid_repeat_notation(tmp_path):
    flat = _house()
    instance = _configure(flat, tmp_path)
    _bind(instance, flat)

    with pytest.raises(ValueError):
        instance.add_timer(time_spec=str(int(time.time() + 60)), device="house.desk_lamp",
                            endpoint="power", action="set", value="on", repeat_spec="bogus")


def test_update_timer_replaces_task_under_same_tag(tmp_path):
    flat = _house()
    instance = _configure(flat, tmp_path)
    system = _bind(instance, flat)
    t = instance.add_timer(time_spec=str(int(time.time() + 3600)), device="house.desk_lamp",
                            endpoint="power", action="set", value="on")

    instance.update_timer(t.id, time_spec=str(int(time.time() + 7200)), device="house.desk_lamp",
                           endpoint="power", action="set", value="off", description="updated")

    assert [x.tag for x in system.tasks] == ["timer.house.1"]  # replaced, not duplicated
    assert instance.get_timer(t.id).value == "off"
    assert instance.get_timer(t.id).description == "updated"


def test_update_timer_unknown_id_raises(tmp_path):
    flat = _house()
    instance = _configure(flat, tmp_path)
    _bind(instance, flat)

    with pytest.raises(ValueError):
        instance.update_timer(999, time_spec=str(int(time.time() + 60)), device="house.desk_lamp",
                               endpoint="power", action="set", value="on")


def test_delete_timer_removes_task_and_record(tmp_path):
    flat = _house()
    instance = _configure(flat, tmp_path)
    system = _bind(instance, flat)
    t = instance.add_timer(time_spec=str(int(time.time() + 3600)), device="house.desk_lamp",
                            endpoint="power", action="set", value="on")

    instance.delete_timer(t.id)

    assert list(system.tasks) == []
    assert instance.list_timers() == []


def test_set_enabled_false_kills_task_but_keeps_record(tmp_path):
    flat = _house()
    instance = _configure(flat, tmp_path)
    system = _bind(instance, flat)
    t = instance.add_timer(time_spec=str(int(time.time() + 3600)), device="house.desk_lamp",
                            endpoint="power", action="set", value="on")

    instance.set_enabled(t.id, False)

    assert list(system.tasks) == []
    assert instance.get_timer(t.id).enabled is False


def test_set_enabled_true_re_registers_task(tmp_path):
    flat = _house()
    instance = _configure(flat, tmp_path)
    system = _bind(instance, flat)
    t = instance.add_timer(time_spec=str(int(time.time() + 3600)), device="house.desk_lamp",
                            endpoint="power", action="set", value="on", enabled=False)
    assert list(system.tasks) == []

    instance.set_enabled(t.id, True)

    assert [x.tag for x in system.tasks] == ["timer.house.1"]


# ---------- end-to-end via load_system() + Scheduler ----------

def test_load_system_collects_on_bind_and_on_tick_from_timer_instance(tmp_path):
    discover_extensions()
    path = tmp_path / "timers.yaml"
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text(f"""
heartbeat: 1s
devices:
  - id: lamp
    module: virtual
    update: 1s
    endpoints:
      - key: power
        writable: true
        type: bool

extensions:
  timer:
    house:
      path: "{path.as_posix()}"
      selectors: ["lamp/power"]
""")
    system = load_system(system_yaml)

    assert len(system.tick_hooks) == 1


def test_end_to_end_one_shot_timer_fires_and_is_removed(tmp_path):
    discover_extensions()
    path = tmp_path / "timers.yaml"
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text(f"""
heartbeat: 1s
devices:
  - id: lamp
    module: virtual
    update: 1s
    endpoints:
      - key: power
        writable: true
        type: bool

extensions:
  timer:
    house:
      path: "{path.as_posix()}"
      selectors: ["lamp/power"]
""")
    system = load_system(system_yaml)
    instance = system.extensions["timer.house"]
    now = time.time()
    instance.add_timer(time_spec=str(int(now - 1)), device="lamp", endpoint="power",
                        action="set", value="true")

    scheduler = Scheduler(system.devices, tasks=system.tasks, heartbeat=system.heartbeat,
                           tick_hooks=system.tick_hooks)
    scheduler.tick(now=now)      # fires the timer's Task (write staged)
    scheduler.tick(now=now + 1)  # next fetch/commit makes the write observable

    assert system.devices["lamp"].get("power") is True
    assert instance.list_timers() == []
    assert TimerStore(path).load() == (2, [])


def test_end_to_end_catch_up_grace_window_across_restart(tmp_path):
    discover_extensions()
    path = tmp_path / "timers.yaml"
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text(f"""
heartbeat: 1s
devices:
  - id: lamp
    module: virtual
    update: 1s
    endpoints:
      - key: power
        writable: true
        type: bool

extensions:
  timer:
    house:
      path: "{path.as_posix()}"
      selectors: ["lamp/power"]
      catch_up: 5m
""")
    now = time.time()
    TimerStore(path).write(3, [
        TimerDef(id=1, time=now - 600, device="lamp", endpoint="power", action="set",
                 value="true", repeat=None, description="stale", enabled=True),
        TimerDef(id=2, time=now - 30, device="lamp", endpoint="power", action="set",
                 value="true", repeat=None, description="recent", enabled=True),
    ])

    system = load_system(system_yaml)
    instance = system.extensions["timer.house"]

    assert [t.id for t in instance.list_timers()] == [2]
    assert [t.tag for t in system.tasks] == ["timer.house.2"]
