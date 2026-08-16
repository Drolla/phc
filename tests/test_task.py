"""Tests for phc.core.task: Condition, Task, and the built-in Action kinds."""

import logging

import pytest

from phc.core.endpoint import Endpoint
from phc.core.scripting import compile_expression
from phc.core.task import (
    Condition, CreateTaskAction, ExprCondition, KillTaskAction, LogAction, ScriptAction,
    SetAction, Task, ToggleAction, _build_rule_namespace, kill_tasks, register_task,
    resolve_endpoint_ref,
)
from phc.devices.virtual.device import VirtualDevice
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


def _named_light(device_id, default="off"):
    """Like _light(), but with a caller-chosen device id -- for tests that
    need two distinct devices in the same `devices` dict (e.g. a set
    action's `expr` reading one device's state to write another's)."""
    light = VirtualDevice(device_id, endpoints=[Endpoint("state", writable=True)])
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


# ---------- Action device_id/endpoint_key defaults ----------

def test_action_device_id_defaults_to_none():
    assert SetAction(value="on").device_id is None


def test_create_task_action_builds_without_a_device():
    assert CreateTaskAction(specs={}, flat={}, tasks=[]).device_id is None


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


def test_set_action_requires_exactly_one_of_value_or_expr():
    with pytest.raises(ValueError):
        SetAction(device_id="living_light", endpoint_key="state")
    with pytest.raises(ValueError):
        SetAction(device_id="living_light", endpoint_key="state", value="on", expr="'on'")


def test_set_action_expr_inline_state_call_without_refs():
    source = _named_light("source", "on")
    target = _named_light("target", "off")
    devices = {"source": source, "target": target}
    SetAction(device_id="target", endpoint_key="state", expr="state('source.state')",
              flat=devices, task_tag="mirror").perform(devices)
    fetch_sync(target)
    target.update_state()
    assert target.get() == "on"


def test_set_action_expr_refs_attribute_form():
    source = _named_light("source", "on")
    target = _named_light("target", "off")
    devices = {"source": source, "target": target}
    SetAction(device_id="target", endpoint_key="state", expr="src.state",
              refs={"src": "source.state"}, flat=devices, task_tag="mirror").perform(devices)
    fetch_sync(target)
    target.update_state()
    assert target.get() == "on"


def test_set_action_expr_reevaluates_on_each_perform():
    source = _named_light("source", "on")
    target = _named_light("target", "off")
    devices = {"source": source, "target": target}
    action = SetAction(device_id="target", endpoint_key="state", expr="state('source.state')",
                        flat=devices, task_tag="mirror")

    action.perform(devices)
    fetch_sync(target)
    target.update_state()
    assert target.get() == "on"

    source.set("off")
    fetch_sync(source)
    source.update_state()
    action.perform(devices)
    fetch_sync(target)
    target.update_state()
    assert target.get() == "off"


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


def test_log_action_with_no_device_logs_message_verbatim(task_log):
    action = LogAction(message="all lights off")
    action.perform({})
    assert "all lights off" in task_log.text


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


def test_condition_changed_false_true_when_no_event():
    """changed=False is the negation of changed=True, not "always true":
    it holds only on a tick with NO change event."""
    light = _light("off")
    devices = {"living_light": light}
    condition = Condition(device_id="living_light", endpoint_key="state", changed=False)
    fetch_sync(light)
    light.update_state()  # settle: no event this tick
    assert condition.evaluate(devices) is True


def test_condition_changed_false_false_when_event_present():
    light = _light("off")
    devices = {"living_light": light}
    condition = Condition(device_id="living_light", endpoint_key="state", changed=False)
    light.set("on")
    fetch_sync(light)
    light.update_state()
    assert condition.evaluate(devices) is False


def test_condition_value_only_is_a_level_check():
    """value= alone (no changed=) ignores whether the state just changed --
    it holds on every tick the CURRENT state matches, transition tick or
    not, and is combinable with min_interval for a "while X holds" gate."""
    light = _typed_light(default=1)
    devices = {"living_light": light}
    condition = Condition(device_id="living_light", endpoint_key="state", value=1)
    # the transition tick itself (_typed_light's own initial commit)
    assert condition.evaluate(devices) is True
    # a later, settled tick with no new event -- still True
    fetch_sync(light)
    light.update_state()
    assert condition.evaluate(devices) is True


def test_condition_value_only_false_when_state_differs():
    light = _typed_light(default=0)
    devices = {"living_light": light}
    condition = Condition(device_id="living_light", endpoint_key="state", value=1)
    assert condition.evaluate(devices) is False


def test_condition_changed_true_and_value_is_an_edge_to_value_check():
    """changed=True + value= holds only on the tick state transitions TO
    value -- not on a later stable tick already at that value, and not on
    a transition to a different value."""
    light = _typed_light(default=0)
    devices = {"living_light": light}
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True, value=1)

    light.set(1)
    fetch_sync(light)
    light.update_state()
    assert condition.evaluate(devices) is True  # transitioned to 1 this tick

    fetch_sync(light)
    light.update_state()  # settle: still 1, but no new event
    assert condition.evaluate(devices) is False

    light.set(0)
    fetch_sync(light)
    light.update_state()
    assert condition.evaluate(devices) is False  # transitioned, but to 0 not 1


def test_condition_changed_false_and_value_is_a_steady_state_check():
    """changed=False + value= holds on a tick the state is already at
    value with no fresh event -- but NOT on the transition tick itself,
    even though the state matches there too."""
    light = _typed_light(default=0)
    devices = {"living_light": light}
    condition = Condition(device_id="living_light", endpoint_key="state", changed=False, value=1)

    light.set(1)
    fetch_sync(light)
    light.update_state()
    assert condition.evaluate(devices) is False  # matches, but this IS the transition tick

    fetch_sync(light)
    light.update_state()  # settle: still 1, no new event
    assert condition.evaluate(devices) is True

    light.set(0)
    fetch_sync(light)
    light.update_state()
    assert condition.evaluate(devices) is False  # no event, but doesn't match value


def test_condition_with_no_filters_is_always_true():
    """Neither changed= nor value= given: both are unset defaults, so
    evaluate() is unconditionally True regardless of device state -- the
    same "unconditional" behavior previously spelled changed=False."""
    light = _light("off")
    devices = {"living_light": light}
    condition = Condition(device_id="living_light", endpoint_key="state")
    assert condition.evaluate(devices) is True
    fetch_sync(light)
    light.update_state()
    assert condition.evaluate(devices) is True


# ---------- Task ----------

def test_one_shot_task_runs_once_and_marks_itself_finished():
    """repeat=None (the default, i.e. omitted) is ONE-SHOT (see Task's
    class docstring -- THC's -repeat "" case): fires once, then
    `finished` becomes True. Task never touches any task list itself --
    removing a finished task from the live list is the Scheduler's job
    (see phc/core/scheduler.py's tick loop)."""
    light = _light("off")
    devices = {"living_light": light}
    task = Task("blink", due_time=0.0,
                actions=[ToggleAction(device_id="living_light", endpoint_key="state")])

    assert task.finished is False
    assert task.run(0.0, devices) is True
    assert task.finished is True


def test_permanent_task_repeat_zero_fires_every_tick():
    """repeat=0 (or negative) is PERMANENT (THC's -repeat 0): due_time is
    left alone and the task keeps firing on every subsequent tick."""
    light = _light("off")
    devices = {"living_light": light}
    task = Task("heartbeat", repeat=0.0,
                actions=[ToggleAction(device_id="living_light", endpoint_key="state")])
    assert task.run(0.0, devices) is True
    assert task.due_time is None
    assert task.finished is False
    assert task.run(1.0, devices) is True  # still fires -- due_time never gated it


def test_condition_only_task_never_finished_regardless_of_repeat():
    """A bare condition (no `time:` ever given, due_time stays None) has
    no due-time schedule to exhaust, so it's never `finished` -- stays
    resident and re-evaluated every tick regardless of `repeat`."""
    light = _light("off")
    devices = {"living_light": light}

    once_repeat = Task("once_repeat_default",
                        actions=[ToggleAction(device_id="living_light", endpoint_key="state")])
    assert once_repeat.run(0.0, devices) is True
    assert once_repeat.finished is False
    assert once_repeat.due_time is None

    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    conditional = Task("conditional", condition=condition,
                        actions=[ToggleAction(device_id="living_light", endpoint_key="state")])
    assert conditional.run(0.0, devices) is True
    assert conditional.finished is False
    assert conditional.due_time is None  # condition-only, no schedule: untouched


def test_time_driven_task_reschedules_on_repeat_and_stays_not_finished():
    light = _light("off")
    devices = {"living_light": light}
    task = Task("repeating", due_time=0.0, repeat=3.0,
                actions=[ToggleAction(device_id="living_light", endpoint_key="state")])
    assert task.run(0.0, devices) is True
    assert task.due_time == 3.0  # rearmed, not exhausted
    assert task.finished is False


def test_time_driven_task_reschedules_on_repeat():
    light = _light("off")
    devices = {"living_light": light}
    task = Task("blink", due_time=1.0, repeat=3.0,
                actions=[ToggleAction(device_id="living_light", endpoint_key="state")])

    assert task.run(0.5, devices) is False  # not due yet
    assert task.run(1.0, devices) is True
    assert task.due_time == 4.0
    assert task.run(2.0, devices) is False
    assert task.run(4.0, devices) is True


def test_time_driven_task_reschedule_catches_up_after_stall():
    light = _light("off")
    devices = {"living_light": light}
    task = Task("blink", due_time=1.0, repeat=3.0,
                actions=[ToggleAction(device_id="living_light", endpoint_key="state")])

    task.run(1.0, devices)
    # process stalled; next tick observed is way past several missed periods
    task.run(11.0, devices)
    assert task.due_time == 13.0


def test_condition_task_with_no_due_time_is_always_due_gate_is_condition_only():
    light = _light("off")
    devices = {"living_light": light}
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    task = Task("report", repeat=0.0, condition=condition,
                actions=[LogAction(device_id="living_light", endpoint_key="state",
                                    message="changed to {state}")])
    # due_time defaults to None: never gates run(), only the condition does.
    assert task.due_time is None
    task.run(9999.0, devices)
    assert task.due_time is None  # untouched: no repeat rearm without a due-time schedule


def test_condition_task_only_performs_action_when_condition_true(task_log):
    light = _light("off")
    fetch_sync(light)
    light.update_state()  # settle: clear the event from the initial set()
    devices = {"living_light": light}
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    task = Task("report", condition=condition,
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
    task = Task("both", condition=condition,
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


def test_last_fired_property_tracks_run():
    light = _light("off")
    devices = {"living_light": light}
    task = Task("blink", due_time=0.0, repeat=0.0,
                actions=[ToggleAction(device_id="living_light", endpoint_key="state")])

    assert task.last_fired == float("-inf")
    task.run(3.0, devices)
    assert task.last_fired == 3.0


def test_min_interval_debounces_condition_task(task_log):
    light = _light("off")
    fetch_sync(light)
    light.update_state()  # settle
    devices = {"living_light": light}
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    task = Task("report", condition=condition, min_interval=10.0,
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


# ---------- history()/fractile()/median()/average() ----------

def _sensor(device_id="sensor", history=4):
    return VirtualDevice(device_id, endpoints=[
        Endpoint("temp", writable=True, value_type="float", history=history),
    ])


def _record(sensor, *values):
    for value in values:
        sensor.set(value)
        fetch_sync(sensor)
        sensor.update_state()
        sensor.endpoint("temp").record_history()


def _namespace(devices, task_tag="t"):
    return _build_rule_namespace(devices=devices, flat=devices, tasks=[], extensions={},
                                  sticky_endpoints=set(), task_tag=task_tag, writable=False)


def test_fractile_matches_thc_index_formula():
    sensor = _sensor(history=8)
    _record(sensor, *range(1, 9))  # 1..8
    devices = {"sensor": sensor}
    namespace = _namespace(devices)
    # THC's radon control: VHistory_Get ... 0.625 / 0.375
    assert namespace["fractile"]("sensor.temp", 0.625) == 5
    assert namespace["fractile"]("sensor.temp", 0.375) == 4


def test_fractile_lower_middle_for_even_pool():
    sensor = _sensor(history=6)
    _record(sensor, *range(1, 7))  # 1..6
    devices = {"sensor": sensor}
    namespace = _namespace(devices)
    # Tcl's round-half-away-from-zero: round((6-1)*0.5) == round(2.5) == 3
    # -> the value at (0-based) index 3 once sorted.
    assert namespace["fractile"]("sensor.temp", 0.5) == 4


def test_fractile_pools_multiple_refs():
    sensor_a = _sensor("sensor_a", history=4)
    sensor_b = _sensor("sensor_b", history=4)
    _record(sensor_a, 1.0, 2.0, 3.0, 4.0)
    _record(sensor_b, 5.0, 6.0, 7.0, 8.0)
    devices = {"sensor_a": sensor_a, "sensor_b": sensor_b}
    namespace = _namespace(devices)
    assert namespace["fractile"](["sensor_a.temp", "sensor_b.temp"], 0.625) == 5.0
    assert namespace["fractile"](["sensor_a.temp", "sensor_b.temp"], 0.375) == 4.0


def test_fractile_extremes_are_min_and_max():
    sensor = _sensor(history=4)
    _record(sensor, 3.0, 1.0, 4.0, 2.0)
    devices = {"sensor": sensor}
    namespace = _namespace(devices)
    assert namespace["fractile"]("sensor.temp", 0) == 1.0
    assert namespace["fractile"]("sensor.temp", 1) == 4.0


def test_fractile_returns_none_for_empty_history():
    sensor = _sensor(history=4)
    devices = {"sensor": sensor}
    namespace = _namespace(devices)
    assert namespace["fractile"]("sensor.temp", 0.5) is None


def test_fractile_single_sample_returns_that_sample():
    sensor = _sensor(history=4)
    _record(sensor, 7.0)
    devices = {"sensor": sensor}
    namespace = _namespace(devices)
    assert namespace["fractile"]("sensor.temp", 0.0) == 7.0
    assert namespace["fractile"]("sensor.temp", 1.0) == 7.0


@pytest.mark.parametrize("bad_f", [-0.1, 1.1])
def test_fractile_rejects_f_outside_range(bad_f):
    sensor = _sensor(history=4)
    _record(sensor, 1.0, 2.0)
    devices = {"sensor": sensor}
    namespace = _namespace(devices)
    with pytest.raises(ValueError):
        namespace["fractile"]("sensor.temp", bad_f)


def test_fractile_raises_for_endpoint_without_history():
    sensor = _sensor(history=0)
    devices = {"sensor": sensor}
    namespace = _namespace(devices)
    with pytest.raises(ValueError):
        namespace["fractile"]("sensor.temp", 0.5)


def test_median_equals_fractile_half():
    sensor = _sensor(history=8)
    _record(sensor, *range(1, 9))
    devices = {"sensor": sensor}
    namespace = _namespace(devices)
    assert namespace["median"]("sensor.temp") == namespace["fractile"]("sensor.temp", 0.5)


def test_average_pools_and_returns_none_when_empty():
    sensor_a = _sensor("sensor_a", history=4)
    sensor_b = _sensor("sensor_b", history=4)
    devices = {"sensor_a": sensor_a, "sensor_b": sensor_b}
    namespace = _namespace(devices)
    assert namespace["average"](["sensor_a.temp", "sensor_b.temp"]) is None

    _record(sensor_a, 1.0, 3.0)
    _record(sensor_b, 5.0, 7.0)
    assert namespace["average"](["sensor_a.temp", "sensor_b.temp"]) == 4.0


def test_fractile_accepts_a_devices_selector_list(task_log):
    sensor_a = _sensor("sensor_a", history=4)
    sensor_b = _sensor("sensor_b", history=4)
    _record(sensor_a, 1.0, 2.0, 3.0, 4.0)
    _record(sensor_b, 5.0, 6.0, 7.0, 8.0)
    devices = {"sensor_a": sensor_a, "sensor_b": sensor_b}
    action = ScriptAction(
        code="log(fractile(devices('sensor_*/temp'), 0.5))",
        task_tag="t", flat=devices, tasks=[])
    action.perform(devices)
    assert "5.0" in task_log.text


def test_history_functions_available_in_read_only_namespace():
    devices = {}
    namespace = _build_rule_namespace(devices=devices, flat=devices, tasks=None,
                                       extensions=None, sticky_endpoints=None,
                                       task_tag="t", writable=False)
    for name in ("history", "fractile", "median", "average"):
        assert name in namespace
    # writable-only functions must NOT leak into the read-only namespace
    for name in ("set_state", "create_task", "kill_task", "reset_sticky", "log"):
        assert name not in namespace
