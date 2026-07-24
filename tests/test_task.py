"""Tests for core.task: Condition, Task, and the built-in Action kinds."""

import logging

import pytest

from core.endpoint import Endpoint
from core.scripting import compile_expression
from core.task import (
    Condition, CreateTaskAction, ExprCondition, KillTaskAction, LogAction, ScriptAction,
    SetAction, Task, ToggleAction, kill_tasks, register_task, resolve_endpoint_ref,
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


# ---------- Action.requires_device ----------

def test_action_requires_device_defaults_true():
    assert SetAction(device_id="living_light", endpoint_key="state", value="on").requires_device is True


def test_create_task_action_requires_device_is_false():
    assert CreateTaskAction(specs={}, flat={}, tasks=[]).requires_device is False


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


# ---------- register_task / kill_tasks ----------

def test_register_task_threads_sticky_endpoints_into_dynamically_created_task():
    light = _light("off")
    flat = {"living_light": light}
    tasks: list[Task] = []
    sticky_endpoints: set = set()
    specs = {
        "tag": "watch",
        "condition": {"refs": {"s": "living_light.state"}, "expr": "s.changed"},
        "action": {"kind": "log", "device": "living_light.state", "message": "x"},
    }

    register_task(specs, flat, tasks, {}, sticky_endpoints)

    assert tasks[0].tag == "watch"
    assert light.endpoint("state") in sticky_endpoints


def test_kill_tasks_removes_multiple_matching_tags():
    tasks = [Task("a", due_time=1.0, actions=[]), Task("b", due_time=1.0, actions=[]),
              Task("c", due_time=1.0, actions=[])]
    removed = kill_tasks(["a", "c"], tasks)
    assert removed == 2
    assert [t.tag for t in tasks] == ["b"]


def test_kill_tasks_glob_pattern():
    tasks = [Task("surv_a", due_time=1.0, actions=[]), Task("surv_b", due_time=1.0, actions=[]),
              Task("keep", due_time=1.0, actions=[])]
    removed = kill_tasks(["surv_*"], tasks)
    assert removed == 2
    assert [t.tag for t in tasks] == ["keep"]


def test_kill_tasks_no_match_is_noop():
    tasks = [Task("a", due_time=1.0, actions=[])]
    assert kill_tasks(["zzz*"], tasks) == 0
    assert len(tasks) == 1


def test_kill_task_action_removes_matching_tags():
    tasks = [Task("a", due_time=1.0, actions=[]), Task("b", due_time=1.0, actions=[])]
    action = KillTaskAction(tags=["a"], flat={}, tasks=tasks)
    action.perform({})
    assert [t.tag for t in tasks] == ["b"]


# ---------- ExprCondition ----------

def test_expr_condition_refs_attribute_form():
    light = _light("off")
    fetch_sync(light)
    light.update_state()  # settle: clear the event from the initial set()
    devices = {"living_light": light}
    compiled = compile_expression("sensor.changed and sensor.state == 'on'")
    condition = ExprCondition(compiled=compiled, refs={"sensor": "living_light.state"},
                               task_tag="report", flat=devices)
    assert condition.evaluate(devices) is False

    light.set("on")
    fetch_sync(light)
    light.update_state()
    assert condition.evaluate(devices) is True


def test_expr_condition_inline_state_call_without_refs():
    light = _light("off")
    devices = {"living_light": light}
    compiled = compile_expression("state('living_light.state') == 'off'")
    condition = ExprCondition(compiled=compiled, refs={}, task_tag="report", flat=devices)
    assert condition.evaluate(devices) is True


def test_expr_condition_combines_multiple_devices():
    light = _light("on")
    lamp = VirtualDevice("desk_lamp", endpoints=[Endpoint("power", writable=True)])
    lamp.set("off")
    fetch_sync(lamp)
    lamp.update_state()
    devices = {"living_light": light, "desk_lamp": lamp}
    compiled = compile_expression(
        "state('living_light.state') == 'on' and state('desk_lamp.power') == 'off'")
    condition = ExprCondition(compiled=compiled, refs={}, task_tag="t", flat=devices)
    assert condition.evaluate(devices) is True


def test_expr_condition_reads_sticky_value():
    light = _light("off")
    fetch_sync(light)
    light.update_state()
    devices = {"living_light": light}
    endpoint = light.endpoint("state")
    endpoint.subscribe_log("report")
    endpoint.update_log_value()
    compiled = compile_expression("s.sticky == 'off'")
    condition = ExprCondition(compiled=compiled, refs={"s": "living_light.state"},
                               task_tag="report", flat=devices)
    assert condition.evaluate(devices) is True


# ---------- ScriptAction ----------

def test_script_action_set_state_and_log(task_log):
    light = _light("off")
    devices = {"living_light": light}
    action = ScriptAction(code="set_state('living_light.state', 'on')\nlog('set to on')",
                           task_tag="t", flat=devices, tasks=[])
    action.perform(devices)
    fetch_sync(light)
    light.update_state()
    assert light.get() == "on"
    assert "set to on" in task_log.text


def test_script_action_create_task_appends_to_list():
    light = _light("off")
    flat = {"living_light": light}
    tasks: list[Task] = []
    code = ("create_task({'tag': 'clear_alert', 'time': '+1s', "
            "'action': {'kind': 'set', 'device': 'living_light.state', 'value': 'off'}})")
    action = ScriptAction(code=code, task_tag="t", flat=flat, tasks=tasks)
    action.perform(flat)
    assert len(tasks) == 1
    assert tasks[0].tag == "clear_alert"


def test_script_action_kill_task_removes_matching_tag():
    flat: dict = {}
    tasks = [Task("alert_a", due_time=1.0, actions=[]), Task("keep_me", due_time=1.0, actions=[])]
    action = ScriptAction(code="kill_task('alert_a')", task_tag="t", flat=flat, tasks=tasks)
    action.perform({})
    assert [t.tag for t in tasks] == ["keep_me"]


def test_script_action_reset_sticky():
    light = _light("off")
    devices = {"living_light": light}
    endpoint = light.endpoint("state")
    endpoint.subscribe_log("t")
    endpoint.update_log_value()
    assert endpoint.get_log_value("t") == "off"

    action = ScriptAction(code="reset_sticky('living_light.state')", task_tag="t", flat=devices, tasks=[])
    action.perform(devices)
    assert endpoint.get_log_value("t") is None


def test_script_action_if_and_for_over_devices_selector(task_log):
    light = _light("off")
    lamp = VirtualDevice("desk_lamp", endpoints=[Endpoint("power", writable=True)])
    lamp.set("off")
    fetch_sync(lamp)
    lamp.update_state()
    flat = {"living_light": light, "desk_lamp": lamp}
    code = "for ref in devices('*/*'):\n    if state(ref) == 'off':\n        log(ref)"
    action = ScriptAction(code=code, task_tag="t", flat=flat, tasks=[])
    action.perform(flat)
    assert "desk_lamp.power" in task_log.text
    assert "living_light.state" in task_log.text


# ---------- Task.min_interval ----------

def test_min_interval_blocks_refire_within_cooldown():
    light = _light("off")
    devices = {"living_light": light}
    task = Task("blink", due_time=0.0, repeat=0.0, min_interval=5.0,
                actions=[ToggleAction(device_id="living_light", endpoint_key="state")])

    assert task.run(0.0, devices) is True
    assert task.run(1.0, devices) is False  # still cooling down
    assert task.run(5.0, devices) is True  # cooldown elapsed


def test_min_interval_debounces_condition_task(task_log):
    light = _light("off")
    fetch_sync(light)
    light.update_state()  # settle
    devices = {"living_light": light}
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    task = Task("report", due_time=float("-inf"), condition=condition, min_interval=10.0,
                actions=[LogAction(device_id="living_light", endpoint_key="state",
                                    message="changed to {state}")])

    light.set("on")
    fetch_sync(light)
    light.update_state()
    assert task.run(1.0, devices) is True
    assert "changed to on" in task_log.text

    light.set("off")
    fetch_sync(light)
    light.update_state()
    assert task.run(2.0, devices) is False  # condition holds again, but still cooling down

    light.set("on")
    fetch_sync(light)
    light.update_state()
    assert task.run(11.0, devices) is True  # cooldown elapsed
