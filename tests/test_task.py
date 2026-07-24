"""Tests for core.task: Condition, Task, and the built-in Action kinds."""

import logging

import pytest

from core.endpoint import Endpoint
from core.task import (
    Condition, CreateTaskAction, LogAction, SetAction, Task, ToggleAction,
    resolve_endpoint_ref,
)
from devices.virtual.device import VirtualDevice
from tests.conftest import fetch_sync


def _light(default="off"):
    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)])
    light.set(default)
    fetch_sync(light)
    light.update_state()
    return light


def _typed_light(default=0):
    light = VirtualDevice("living_light", endpoints=[
        Endpoint("state", writable=True, value_type="int", values={0: "off", 1: "on"}),
    ])
    light.set(default)
    fetch_sync(light)
    light.update_state()
    return light


@pytest.fixture
def task_log(caplog):
    """caplog, but attached directly to the "phc.tasks" logger.

    configure_logging() (invoked by other tests via load_system()) sets
    propagate=False on the "phc" logger, which would otherwise stop
    caplog's root-attached handler from ever seeing "phc.tasks" records."""
    task_logger = logging.getLogger("phc.tasks")
    task_logger.addHandler(caplog.handler)
    task_logger.setLevel(logging.INFO)
    try:
        with caplog.at_level("INFO", logger="phc.tasks"):
            yield caplog
    finally:
        task_logger.removeHandler(caplog.handler)


# ---------- resolve_endpoint_ref ----------

def test_resolve_endpoint_ref_simple():
    assert resolve_endpoint_ref("living_light.state") == ("living_light", "state")


def test_resolve_endpoint_ref_nested_device_id():
    assert resolve_endpoint_ref("house.desk_lamp.power") == ("house.desk_lamp", "power")


def test_resolve_endpoint_ref_rejects_no_dot():
    with pytest.raises(ValueError):
        resolve_endpoint_ref("living_light")


def test_resolve_endpoint_ref_allow_bare_returns_none_endpoint():
    assert resolve_endpoint_ref("living_light", allow_bare=True) == ("living_light", None)


def test_resolve_endpoint_ref_allow_bare_still_splits_dotted_ref():
    assert resolve_endpoint_ref("living_light.state", allow_bare=True) == ("living_light", "state")


# ---------- default actions ----------

def test_set_action_untyped_passes_value_through():
    light = _light("off")
    devices = {"living_light": light}
    SetAction(device_id="living_light", endpoint_key="state", value="on").perform(devices)
    fetch_sync(light)
    light.update_state()
    assert light.get() == "on"


def test_set_action_translates_text_via_values_mapping():
    light = _typed_light(0)
    devices = {"living_light": light}
    SetAction(device_id="living_light", endpoint_key="state", value="on").perform(devices)
    fetch_sync(light)
    light.update_state()
    assert light.get() == 1


def test_set_action_accepts_raw_value_via_values_mapping():
    light = _typed_light(0)
    devices = {"living_light": light}
    SetAction(device_id="living_light", endpoint_key="state", value=1).perform(devices)
    fetch_sync(light)
    light.update_state()
    assert light.get() == 1


def test_toggle_action_flips_off_to_on():
    light = _light("off")
    devices = {"living_light": light}
    ToggleAction(device_id="living_light", endpoint_key="state").perform(devices)
    fetch_sync(light)
    light.update_state()
    assert light.get() == "on"


def test_toggle_action_flips_on_to_off():
    light = _light("on")
    devices = {"living_light": light}
    ToggleAction(device_id="living_light", endpoint_key="state").perform(devices)
    fetch_sync(light)
    light.update_state()
    assert light.get() == "off"


def test_toggle_action_flips_via_values_mapping():
    light = _typed_light(0)
    devices = {"living_light": light}
    ToggleAction(device_id="living_light", endpoint_key="state").perform(devices)
    fetch_sync(light)
    light.update_state()
    assert light.get() == 1

    ToggleAction(device_id="living_light", endpoint_key="state").perform(devices)
    fetch_sync(light)
    light.update_state()
    assert light.get() == 0


def test_log_action_formats_state_placeholder(task_log):
    light = _light("on")
    devices = {"living_light": light}
    action = LogAction(device_id="living_light", endpoint_key="state",
                        message="living_light changed to {state}")
    action.perform(devices)
    assert "living_light changed to on" in task_log.text


def test_log_action_formats_text_placeholder_via_values_mapping(task_log):
    light = _typed_light(1)
    devices = {"living_light": light}
    action = LogAction(device_id="living_light", endpoint_key="state",
                        message="living_light changed to {text} (raw {state})")
    action.perform(devices)
    assert "living_light changed to on (raw 1)" in task_log.text


def test_log_action_with_no_endpoint_reports_every_endpoint(task_log):
    lamp = VirtualDevice("desk_lamp", endpoints=[Endpoint("power", writable=True),
                                                  Endpoint("brightness", writable=True)])
    lamp.set({"power": "on", "brightness": 80})
    fetch_sync(lamp)
    lamp.update_state()
    devices = {"desk_lamp": lamp}

    action = LogAction(device_id="desk_lamp", endpoint_key=None,
                        message="desk_lamp changed to {state}")
    action.perform(devices)
    assert "'power': 'on'" in task_log.text
    assert "'brightness': 80" in task_log.text


# ---------- Condition ----------

def test_condition_true_when_event_present():
    light = _light("off")
    devices = {"living_light": light}
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    light.set("on")
    fetch_sync(light)
    light.update_state()
    assert condition.evaluate(devices) is True


def test_condition_false_when_no_event():
    light = _light("off")
    devices = {"living_light": light}
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    fetch_sync(light)
    light.update_state()
    assert condition.evaluate(devices) is False


# ---------- Task ----------

def test_time_driven_task_runs_once_when_repeat_zero():
    light = _light("off")
    devices = {"living_light": light}
    task = Task("blink", due_time=0.0, repeat=0.0,
                actions=[ToggleAction(device_id="living_light", endpoint_key="state")])

    assert task.due(0.0) is True
    assert task.run(0.0, devices) is True
    task.mark_run(0.0)
    assert task.due(0.1) is False
    assert task.due_time == float("inf")


def test_time_driven_task_reschedules_on_repeat():
    light = _light("off")
    devices = {"living_light": light}
    task = Task("blink", due_time=1.0, repeat=3.0,
                actions=[ToggleAction(device_id="living_light", endpoint_key="state")])

    assert task.due(1.0) is True
    assert task.run(1.0, devices) is True
    task.mark_run(1.0)
    assert task.due_time == 4.0
    assert task.due(2.0) is False
    assert task.due(4.0) is True


def test_time_driven_task_reschedule_catches_up_after_stall():
    light = _light("off")
    devices = {"living_light": light}
    task = Task("blink", due_time=1.0, repeat=3.0,
                actions=[ToggleAction(device_id="living_light", endpoint_key="state")])

    task.run(1.0, devices)
    # process stalled; next tick observed is way past several missed periods
    task.mark_run(11.0)
    assert task.due_time == 13.0


def test_condition_task_is_always_due_gate_is_condition_only():
    light = _light("off")
    devices = {"living_light": light}
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    task = Task("report", due_time=float("-inf"), repeat=0.0, condition=condition,
                actions=[LogAction(device_id="living_light", endpoint_key="state",
                                    message="changed to {state}")])
    assert task.due(0.0) is True
    assert task.due(9999.0) is True


def test_condition_task_only_performs_action_when_condition_true(task_log):
    light = _light("off")
    fetch_sync(light)
    light.update_state()  # settle: clear the event from the initial set()
    devices = {"living_light": light}
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    task = Task("report", due_time=float("-inf"), condition=condition,
                actions=[LogAction(device_id="living_light", endpoint_key="state",
                                    message="changed to {state}")])

    assert task.run(1.0, devices) is False  # no change this cycle
    assert "changed to" not in task_log.text

    light.set("on")
    fetch_sync(light)
    light.update_state()
    assert task.run(2.0, devices) is True
    assert "changed to on" in task_log.text


def test_task_with_multiple_actions_runs_all_in_order():
    light = _light("off")
    lamp = VirtualDevice("desk_lamp", endpoints=[Endpoint("power", writable=True)])
    devices = {"living_light": light, "desk_lamp": lamp}
    task = Task("both", due_time=0.0, repeat=0.0,
                actions=[SetAction(device_id="living_light", endpoint_key="state", value="on"),
                         SetAction(device_id="desk_lamp", endpoint_key="power", value="on")])

    assert task.run(0.0, devices) is True
    fetch_sync(light)
    light.update_state()
    fetch_sync(lamp)
    lamp.update_state()
    assert light.get() == "on"
    assert lamp.get() == "on"


def test_condition_task_with_multiple_actions_all_run_only_when_condition_true(task_log):
    light = _light("off")
    fetch_sync(light)
    light.update_state()  # settle: clear the event from the initial set()
    lamp = VirtualDevice("desk_lamp", endpoints=[Endpoint("power", writable=True)])
    devices = {"living_light": light, "desk_lamp": lamp}
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    task = Task("both", due_time=float("-inf"), condition=condition,
                actions=[LogAction(device_id="living_light", endpoint_key="state",
                                    message="changed to {state}"),
                         SetAction(device_id="desk_lamp", endpoint_key="power", value="on")])

    assert task.run(1.0, devices) is False  # no change this cycle
    assert "changed to" not in task_log.text
    fetch_sync(lamp)
    lamp.update_state()
    assert lamp.get() is None

    light.set("on")
    fetch_sync(light)
    light.update_state()
    assert task.run(2.0, devices) is True
    assert "changed to on" in task_log.text
    fetch_sync(lamp)
    lamp.update_state()
    assert lamp.get() == "on"


# ---------- CreateTaskAction ----------

def test_create_task_action_appends_new_task_to_list():
    light = _light("off")
    flat = {"living_light": light}
    tasks: list[Task] = []
    specs = {
        "tag": "clear_alert",
        "time": "+1s",
        "action": {"kind": "set", "device": "living_light.state", "value": "off"},
    }
    action = CreateTaskAction(specs=specs, flat=flat, tasks=tasks)

    action.perform({"living_light": light})

    assert len(tasks) == 1
    assert tasks[0].tag == "clear_alert"
    assert tasks[0].actions[0].device_id == "living_light"
    assert tasks[0].actions[0].endpoint_key == "state"


def test_create_task_action_replaces_existing_same_tag_task():
    light = _light("off")
    flat = {"living_light": light}
    old_task = Task("clear_alert", due_time=1.0, repeat=0.0,
                     actions=[SetAction(device_id="living_light", endpoint_key="state", value="off")])
    tasks: list[Task] = [old_task]
    specs = {
        "tag": "clear_alert",
        "time": "+1s",
        "action": {"kind": "set", "device": "living_light.state", "value": "off"},
    }
    action = CreateTaskAction(specs=specs, flat=flat, tasks=tasks)

    action.perform({"living_light": light})

    assert len(tasks) == 1
    assert tasks[0] is not old_task
    assert tasks[0].tag == "clear_alert"
