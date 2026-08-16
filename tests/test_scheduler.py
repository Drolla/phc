"""Tests for phc.core.scheduler: the Scheduler's tick loop, tasks, and device polling."""

import logging
import time

import pytest

from phc.core.config import load_system
from phc.core.scheduler import Scheduler
from phc.core.task import Condition, LogAction, Task, ToggleAction
from pathlib import Path
from tests.conftest import fetch_sync


EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "virtual_system.yaml"
SURVEILLANCE_EXAMPLE = (Path(__file__).resolve().parent.parent / "examples"
                         / "virtual_surveillance-task_defs_1-nested.yaml")



def _registry(flat, **kwargs):
    """A TaskRegistry wired exactly as load_system wires one, for tests that
    drive create_task/kill_task through the Scheduler and therefore need a
    container that can actually build tasks."""
    from phc.core.config import _build_task
    from phc.core.task import TaskRegistry

    return TaskRegistry(build_task=_build_task, flat=flat, **kwargs)

@pytest.fixture
def task_log(caplog):
    """caplog, but attached directly to the "phc.tasks" logger, bypassing
    propagate=False set by configure_logging() (invoked via load_system()
    in other tests in this session)."""
    task_logger = logging.getLogger("phc.tasks")
    task_logger.addHandler(caplog.handler)
    task_logger.setLevel(logging.INFO)
    try:
        with caplog.at_level("INFO", logger="phc.tasks"):
            yield caplog
    finally:
        task_logger.removeHandler(caplog.handler)


def test_scheduler_only_runs_due_devices():
    from phc.devices.virtual.device import VirtualDevice
    from phc.core.endpoint import Endpoint

    fast = VirtualDevice("fast", endpoints=[Endpoint("state", writable=True)],
                          update_interval=1.0)
    slow = VirtualDevice("slow", endpoints=[Endpoint("state", writable=True)],
                          update_interval=100.0)

    scheduler = Scheduler({"fast": fast, "slow": slow})

    fast.set("on")
    slow.set("on")

    scheduler.tick(now=0.0)
    assert fast.get() == "on"
    assert slow.get() == "on"

    fast.set("off")
    slow.set("off")
    scheduler.tick(now=0.5)
    assert fast.get() == "on"
    assert slow.get() == "on"

    scheduler.tick(now=1.5)
    assert fast.get() == "off"
    assert slow.get() == "on"


def test_end_to_end_virtual_system_example():
    system = load_system(EXAMPLE)
    scheduler = Scheduler(system.scheduled_devices(), heartbeat=system.heartbeat)

    living_light = system.devices["lights.living_light"]
    assert living_light.get() == 0
    assert living_light.get_text() == "off"

    living_light.set_text("on")
    scheduler.tick(now=0.0)
    assert living_light.get() == 1
    assert living_light.get_text() == "on"
    assert living_light.event == 1

    # tick again after the device's interval has elapsed: value is steady,
    # event should have cleared
    scheduler.tick(now=living_light.update_interval + 0.1)
    assert living_light.get() == 1
    assert living_light.event is None


def test_end_to_end_nested_desk_lamp():
    system = load_system(EXAMPLE)
    scheduler = Scheduler(system.scheduled_devices(), heartbeat=system.heartbeat)

    desk_lamp = system.devices["lights.desk_lamp"]
    desk_lamp.set(75, name="brightness")
    scheduler.tick(now=0.0)

    assert desk_lamp.get("brightness") == 75
    assert desk_lamp.get("power") == 0
    assert desk_lamp.get_text("power") == "off"


def test_scheduler_commits_change_event_for_nested_device_despite_ancestor_presence():
    """Regression test: self._devices is a flat dict holding every device,
    both a "host" parent and each descendant under its own qualified id
    (see phc.core.config._build_device), so the commit pass's per-entry
    Device.update_state() call must not also reach into a device's
    children (see that method's docstring) -- otherwise a nested device's
    endpoint gets committed twice in one tick, and its second, redundant
    commit wipes out the change event the first one just computed. Uses
    the full flat dict (both "house" and "house.desk_lamp"), not
    system.scheduled_devices() (used by test_end_to_end_nested_desk_lamp,
    above) -- that helper excludes any device with no update_interval,
    which would exclude "house" entirely and hide the bug."""
    from phc.core.endpoint import Endpoint
    from phc.devices.host.device import HostDevice
    from phc.devices.virtual.device import VirtualDevice

    lamp = VirtualDevice("desk_lamp", endpoints=[Endpoint("power", writable=True, value_type="int")],
                          update_interval=1.0, parent_qualified_id="house")
    house = HostDevice("house", children=[lamp])
    # Insertion order matters: matches phc.core.config._build_device, which
    # always registers a child's flat entry before its parent's.
    flat = {"house.desk_lamp": lamp, "house": house}

    scheduler = Scheduler(flat, heartbeat=1.0)

    lamp.set(1, name="power")
    scheduler.tick(now=0.0)

    assert lamp.get("power") == 1
    assert lamp.get_event("power") == 1


def test_end_to_end_surveillance_example_arm_intrude_disarm(task_log):
    """Round-trip the condition.expr/kind:script/min_interval/create_task/
    kind:kill_task example
    (examples/virtual_surveillance-task_defs_1-nested.yaml) through the
    Scheduler: arming spawns surv_random_light/surv_intrusion (both nested
    create_task actions), motion raises the alarm and schedules further
    timed follow-ups (once, thanks to min_interval), and disarming tears
    all of them back down."""
    system = load_system(SURVEILLANCE_EXAMPLE)
    scheduler = Scheduler(system.devices, tasks=system.tasks, tick_hooks=system.tick_hooks)

    t = 0.0
    scheduler.tick(now=t)  # settle initial (unset) state

    system.devices["surveillance"].set(1)
    for _ in range(3):
        t += 2.0
        scheduler.tick(now=t)
    assert "surveillance enabled" in task_log.text
    assert system.devices["alarm"].get() == 0
    spawned_on_arm = {"surv_random_light", "surv_intrusion"}
    assert spawned_on_arm <= {task.tag for task in system.tasks}

    system.devices["hallway_motion"].set(1)
    for _ in range(3):
        t += 2.0
        scheduler.tick(now=t)
    assert "intrusion detected" in task_log.text
    spawned_tags = {"surv_alert_log", "surv_siren_off", "surv_light_off", "surv_alert_mail"}
    assert spawned_tags <= {task.tag for task in system.tasks}

    for _ in range(3):
        t += 2.0
        scheduler.tick(now=t)  # let the alarm/siren writes settle onto the devices
    assert system.devices["alarm"].get() == 1
    assert system.devices["siren"].get() == 1
    # kind:random_light, force:1 (surv_intrusion's second-to-last action)
    # forces BOTH lights on as a deterrent -- lights.kitchen_light is never
    # touched by the script action itself, so this specifically exercises
    # the random_light extension's force override.
    assert system.devices["lights.living_light"].get() == 1
    assert system.devices["lights.kitchen_light"].get() == 1

    system.devices["surveillance"].set(0)
    for _ in range(3):
        t += 2.0
        scheduler.tick(now=t)
    assert "surveillance disabled" in task_log.text
    assert (spawned_tags | spawned_on_arm).isdisjoint({task.tag for task in system.tasks})
    assert system.devices["alarm"].get() == 0
    assert system.devices["siren"].get() == 0
    assert system.devices["lights.living_light"].get() == 0
    # Only reachable via surveillance_disable's kind:random_light,
    # force:0 action -- the script action never mentions lights.kitchen_light.
    assert system.devices["lights.kitchen_light"].get() == 0


def test_end_to_end_surveillance_example_all_lights_override(task_log):
    """all_lights is independent of armed/alarm state: toggling it forces
    every random_light-managed light and silences the siren, regardless of
    whether surveillance is armed (see
    examples/virtual_surveillance-task_defs_1-nested.yaml's
    surveillance_all_lights_on/_off tasks)."""
    system = load_system(SURVEILLANCE_EXAMPLE)
    scheduler = Scheduler(system.devices, tasks=system.tasks, tick_hooks=system.tick_hooks)

    t = 0.0
    scheduler.tick(now=t)  # settle initial (unset) state

    system.devices["siren"].set(1)
    system.devices["all_lights"].set(1)
    for _ in range(3):
        t += 2.0
        scheduler.tick(now=t)
    assert "all lights on" in task_log.text
    assert system.devices["siren"].get() == 0
    assert system.devices["lights.living_light"].get() == 1
    assert system.devices["lights.kitchen_light"].get() == 1

    system.devices["all_lights"].set(0)
    for _ in range(3):
        t += 2.0
        scheduler.tick(now=t)
    assert "all lights off" in task_log.text
    assert system.devices["siren"].get() == 0
    assert system.devices["lights.living_light"].get() == 0
    assert system.devices["lights.kitchen_light"].get() == 0


def test_scheduler_runs_blink_task_and_reschedules():
    from phc.devices.virtual.device import VirtualDevice
    from phc.core.endpoint import Endpoint

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    light.set("off")
    fetch_sync(light)
    light.update_state()

    task = Task("blink", due_time=1.0, repeat=3.0,
                actions=[ToggleAction(device_id="living_light", endpoint_key="state")])

    scheduler = Scheduler({"living_light": light}, tasks=[task])

    # Task fires at now=1.0: within this tick, fetch() runs first, then the
    # task (reading the state committed by the PREVIOUS tick's
    # update_state(), still "off"), then this tick's own update_state()
    # commits fetch's result -- the toggle's own write ("on") was only
    # staged via transmit() and isn't picked up by fetch() until the
    # *next* due tick.
    scheduler.tick(now=1.0)
    assert light.get() == "off"
    assert task.due_time == 4.0

    scheduler.tick(now=2.0)
    assert light.get() == "on"

    # tick(4.0): fetch surfaces no new pending write (still "on"), then the
    # task fires again, staging "off" for the *next* tick.
    scheduler.tick(now=4.0)
    assert light.get() == "on"
    assert task.due_time == 7.0

    scheduler.tick(now=7.0)
    assert light.get() == "off"


def test_scheduler_removes_one_shot_task_after_it_fires():
    from phc.devices.virtual.device import VirtualDevice
    from phc.core.endpoint import Endpoint

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    light.set("off")
    fetch_sync(light)
    light.update_state()

    one_shot = Task("once", due_time=1.0,
                     actions=[ToggleAction(device_id="living_light", endpoint_key="state")])
    repeating = Task("repeating", due_time=1.0, repeat=3.0,
                      actions=[ToggleAction(device_id="living_light", endpoint_key="state")])
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    conditional = Task("conditional", condition=condition,
                        actions=[LogAction(device_id="living_light", endpoint_key="state",
                                            message="changed")])
    tasks: list[Task] = [one_shot, repeating, conditional]

    scheduler = Scheduler({"living_light": light}, tasks=tasks)

    scheduler.tick(now=1.0)
    assert one_shot not in scheduler._tasks
    assert repeating in scheduler._tasks
    assert conditional in scheduler._tasks

    # Stays gone -- it isn't re-added or re-evaluated on later ticks.
    scheduler.tick(now=10.0)
    assert one_shot not in scheduler._tasks
    assert len(scheduler._tasks) == 2


def test_scheduler_runs_condition_task_only_on_change_tick(task_log):
    from phc.devices.virtual.device import VirtualDevice
    from phc.core.endpoint import Endpoint

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)

    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    task = Task("report", condition=condition,
                actions=[LogAction(device_id="living_light", endpoint_key="state",
                                    message="living_light changed to {state}")])

    scheduler = Scheduler({"living_light": light}, tasks=[task])

    light.set("on")
    scheduler.tick(now=0.0)
    # The task pass runs BEFORE this tick's update_state() commits the
    # fetch, so the condition can only see what was committed last tick
    # (nothing yet, on the very first tick) -- it does not fire here.
    assert "living_light changed to on" not in task_log.text
    assert light.get() == "on"

    task_log.clear()
    scheduler.tick(now=1.0)
    # Now the task pass sees tick 0's commit (get_event() is still set,
    # since nothing has run update_state() again since then) -- fires
    # exactly one tick after the change.
    assert "living_light changed to on" in task_log.text

    task_log.clear()
    scheduler.tick(now=2.0)
    # Nothing new fetched/committed at tick 2 -- event stays cleared, no re-fire.
    assert "living_light changed to on" not in task_log.text


def test_scheduler_runs_condition_task_for_nested_device(task_log):
    """Nested-device counterpart to test_scheduler_runs_condition_task_only_
    on_change_tick, above: Condition.evaluate() (phc.core.task.Condition, which
    backs every `condition: {device, changed: true}` task trigger, e.g.
    examples/full_house_system.yaml's report_low_battery task) reads
    device.get_event(endpoint_key). Regression test for the bug where a
    nested device's event was always wiped before a task's condition ever
    saw it -- see test_scheduler_commits_change_event_for_nested_device_
    despite_ancestor_presence, above, for the underlying mechanism."""
    from phc.core.endpoint import Endpoint
    from phc.devices.host.device import HostDevice
    from phc.devices.virtual.device import VirtualDevice

    lamp = VirtualDevice("desk_lamp", endpoints=[Endpoint("power", writable=True)],
                          update_interval=1.0, parent_qualified_id="house")
    house = HostDevice("house", children=[lamp])
    flat = {"house.desk_lamp": lamp, "house": house}

    condition = Condition(device_id="house.desk_lamp", endpoint_key="power", changed=True)
    task = Task("report", condition=condition,
                actions=[LogAction(device_id="house.desk_lamp", endpoint_key="power",
                                    message="desk_lamp power changed to {state}")])

    scheduler = Scheduler(flat, tasks=[task])

    lamp.set("on")
    scheduler.tick(now=0.0)
    # Task pass runs before this tick's own commit -- can't see it yet.
    assert "desk_lamp power changed to" not in task_log.text

    task_log.clear()
    scheduler.tick(now=1.0)
    # Task pass now sees tick 0's commit -- fires exactly one tick after
    # the change, same as the non-nested case above.
    assert "desk_lamp power changed to on" in task_log.text

    task_log.clear()
    scheduler.tick(now=2.0)
    # Nothing new fetched/committed at tick 2 -- event stays cleared, no re-fire.
    assert "desk_lamp power changed to" not in task_log.text


def test_scheduler_create_task_action_spawns_task_that_later_fires():
    from phc.devices.virtual.device import VirtualDevice
    from phc.core.endpoint import Endpoint
    from phc.core.task import CreateTaskAction

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    flat = {"living_light": light}

    tasks = _registry(flat)
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    specs = {
        "tag": "clear_alert",
        "time": "+1s",
        "action": {"kind": "set", "device": "living_light.state", "value": "off"},
    }
    trigger = Task("raise_alert", condition=condition,
                    actions=[CreateTaskAction(specs=specs, flat=flat, tasks=tasks)])
    tasks.add(trigger)

    scheduler = Scheduler(flat, tasks=tasks)

    assert len(tasks) == 1  # only the trigger task exists so far

    light.set("on")
    scheduler.tick(now=0.0)
    scheduler.tick(now=1.0)  # condition sees tick 0's commit here, create_task fires

    assert len(tasks) == 2
    spawned = next(t for t in tasks if t.tag == "clear_alert")
    assert spawned.due_time > 1.0

    # advance past the spawned task's due_time and confirm it actually fires
    scheduler.tick(now=spawned.due_time + 0.1)
    fetch_sync(light)
    light.update_state()
    scheduler.tick(now=spawned.due_time + 1.1)
    assert light.get() == "off"


def test_scheduler_create_task_template_spawns_task_that_later_fires():
    """Same as test_scheduler_create_task_action_spawns_task_that_later_fires
    above, but the create_task action gives `template:` instead of a
    literal `specs:` -- the spec is looked up by name in task_specs at fire
    time (see phc.core.task.register_task/CreateTaskAction), everything else
    about spawning/firing the resulting task is identical."""
    from phc.devices.virtual.device import VirtualDevice
    from phc.core.endpoint import Endpoint
    from phc.core.task import CreateTaskAction

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    flat = {"living_light": light}

    task_specs = {
        "clear_alert": {
            "tag": "clear_alert",
            "time": "+1s",
            "action": {"kind": "set", "device": "living_light.state", "value": "off"},
        },
    }
    tasks = _registry(flat, task_specs=task_specs)
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    trigger = Task("raise_alert", condition=condition,
                    actions=[CreateTaskAction(template="clear_alert", flat=flat, tasks=tasks)])
    tasks.add(trigger)

    scheduler = Scheduler(flat, tasks=tasks)

    assert len(tasks) == 1  # only the trigger task exists so far

    light.set("on")
    scheduler.tick(now=0.0)
    scheduler.tick(now=1.0)  # condition sees tick 0's commit here, create_task fires

    assert len(tasks) == 2
    spawned = next(t for t in tasks if t.tag == "clear_alert")
    assert spawned.due_time > 1.0

    # advance past the spawned task's due_time and confirm it actually fires
    scheduler.tick(now=spawned.due_time + 0.1)
    fetch_sync(light)
    light.update_state()
    scheduler.tick(now=spawned.due_time + 1.1)
    assert light.get() == "off"


def test_scheduler_create_task_unknown_template_raises_at_fire_time():
    """Unlike a literal specs:, template: is only resolved when the
    create_task action fires -- an unknown name surfaces as a ValueError
    from perform() then, not at construction/config-load time (see
    phc.core.task.register_task)."""
    from phc.devices.virtual.device import VirtualDevice
    from phc.core.endpoint import Endpoint
    from phc.core.task import CreateTaskAction

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    flat = {"living_light": light}

    tasks = _registry(flat, task_specs={})
    action = CreateTaskAction(template="nonexistent", flat=flat, tasks=tasks)
    with pytest.raises(ValueError):
        action.perform(flat)


def test_scheduler_create_task_replaces_prior_same_tag_task_on_retrigger():
    from phc.devices.virtual.device import VirtualDevice
    from phc.core.endpoint import Endpoint
    from phc.core.task import CreateTaskAction

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    flat = {"living_light": light}

    tasks = _registry(flat)
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    specs = {
        "tag": "clear_alert",
        "time": "+1s",
        "action": {"kind": "set", "device": "living_light.state", "value": "off"},
    }
    trigger = Task("raise_alert", condition=condition,
                    actions=[CreateTaskAction(specs=specs, flat=flat, tasks=tasks)])
    tasks.add(trigger)

    scheduler = Scheduler(flat, tasks=tasks)

    light.set("on")
    scheduler.tick(now=0.0)
    scheduler.tick(now=1.0)  # first create_task fire
    first_spawned = next(t for t in tasks if t.tag == "clear_alert")

    # retrigger: change the light again, let it settle, then observe again
    light.set("off")
    scheduler.tick(now=2.0)
    scheduler.tick(now=3.0)  # second create_task fire

    clear_alert_tasks = [t for t in tasks if t.tag == "clear_alert"]
    assert len(clear_alert_tasks) == 1
    assert clear_alert_tasks[0] is not first_spawned


def test_scheduler_newly_created_task_not_visible_until_next_tick(task_log):
    from phc.devices.virtual.device import VirtualDevice
    from phc.core.endpoint import Endpoint
    from phc.core.task import CreateTaskAction

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    flat = {"living_light": light}

    tasks = _registry(flat)
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    # Nested spec is a condition-driven task (always due), not time-driven --
    # if a task appended mid-tick were visible within that same tick's
    # remaining pass 2 loop, this would fire immediately. Instead,
    # _tick_async snapshots self._tasks once per tick (`for task in
    # list(self._tasks)`, see phc/core/scheduler.py) specifically so a
    # create_task/kill_task action's same-tick mutation only takes effect
    # starting the NEXT tick -- deterministic, and consistent with tasks
    # only ever observing state committed by the previous tick.
    specs = {
        "tag": "clear_alert",
        "condition": {"device": "living_light.state", "changed": False},
        "action": {"kind": "log", "device": "living_light.state", "message": "clear_alert ran"},
    }
    trigger = Task("raise_alert", condition=condition,
                    actions=[CreateTaskAction(specs=specs, flat=flat, tasks=tasks)])
    tasks.add(trigger)

    scheduler = Scheduler(flat, tasks=tasks)

    light.set("on")
    scheduler.tick(now=0.0)
    scheduler.tick(now=1.0)  # raise_alert fires, spawns clear_alert

    assert "clear_alert" in [t.tag for t in tasks]
    assert "clear_alert ran" not in task_log.text  # not run within the tick it was created

    scheduler.tick(now=2.0)
    assert "clear_alert ran" in task_log.text  # runs starting the next tick


def test_scheduler_same_tick_create_and_kill_only_take_effect_next_tick(task_log):
    from phc.devices.virtual.device import VirtualDevice
    from phc.core.endpoint import Endpoint
    from phc.core.task import ScriptAction

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    flat = {"living_light": light}
    tasks = _registry(flat)

    trigger_condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    code = (
        "create_task({'tag': 'spawned', "
        # no 'changed' key: unconditional, same as omitting it entirely --
        # changed=False now means its negation (no event this tick), not
        # "always true" (see phc.core.task.Condition's docstring)
        "'condition': {'device': 'living_light.state'}, "
        "'action': {'kind': 'log', 'device': 'living_light.state', 'message': 'spawned ran'}})\n"
        "kill_task('victim')"
    )
    spawner = Task("spawner", condition=trigger_condition,
                    actions=[ScriptAction(code=code, task_tag="spawner", flat=flat, tasks=tasks)])
    victim = Task("victim", condition=Condition(device_id="living_light", endpoint_key="state"),
                   actions=[LogAction(device_id="living_light", endpoint_key="state", message="victim ran")])
    # "spawner" ordered before "victim": within the same tick, spawner's
    # kill_task('victim') mutates the LIVE list, but this tick's pass 2 loop
    # already snapshotted it (see phc/core/scheduler.py's `list(self._tasks)`),
    # so victim -- already in that snapshot -- still runs this tick; only
    # from the NEXT tick's fresh snapshot is it actually gone.
    tasks.add(spawner)
    tasks.add(victim)

    scheduler = Scheduler(flat, tasks=tasks)

    light.set("on")
    scheduler.tick(now=0.0)
    scheduler.tick(now=1.0)  # spawner fires: creates "spawned", kills "victim"

    assert "victim ran" in task_log.text  # was in this tick's snapshot -> still ran
    assert "spawned ran" not in task_log.text  # wasn't in this tick's snapshot -> didn't run
    assert [t.tag for t in tasks] == ["spawner", "spawned"]  # live list already updated

    task_log.clear()
    scheduler.tick(now=2.0)
    assert "spawned ran" in task_log.text  # now included in the fresh snapshot
    assert "victim ran" not in task_log.text  # gone for good


# ---------- tick_hooks ----------

def test_tick_hook_runs_once_per_tick_with_devices():
    from phc.devices.virtual.device import VirtualDevice
    from phc.core.endpoint import Endpoint

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    calls = []
    scheduler = Scheduler({"living_light": light}, tick_hooks=[lambda devices: calls.append(devices)])

    scheduler.tick(now=0.0)
    assert len(calls) == 1
    assert calls[0] == {"living_light": light}

    scheduler.tick(now=1.0)
    assert len(calls) == 2


def test_tick_hook_observes_this_ticks_freshly_committed_state():
    from phc.devices.virtual.device import VirtualDevice
    from phc.core.endpoint import Endpoint

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    observed = []
    scheduler = Scheduler({"living_light": light},
                           tick_hooks=[lambda devices: observed.append(devices["living_light"].get())])

    light.set("on")
    scheduler.tick(now=0.0)
    # Unlike a task (which sees the PREVIOUS tick's commit, see
    # test_scheduler_runs_condition_task_only_on_change_tick above), a tick
    # hook runs after pass 3 and so observes THIS tick's own fresh commit.
    assert observed == ["on"]


def test_history_hook_records_this_ticks_committed_state():
    from phc.devices.virtual.device import VirtualDevice
    from phc.core.endpoint import Endpoint
    from phc.core.config import _collect_history_records, _make_history_tick_hook

    sensor = VirtualDevice("sensor", endpoints=[
        Endpoint("temp", writable=True, value_type="float", history=4)],
        update_interval=1.0)
    flat = {"sensor": sensor}
    records = _collect_history_records(flat)
    hook = _make_history_tick_hook(records)
    scheduler = Scheduler(flat, tick_hooks=[hook])

    sensor.set(21.5)
    scheduler.tick(now=0.0)
    # The history hook runs in pass 4, after this tick's own update_state()
    # commit (pass 3) -- so the freshly fetched value is already in the
    # buffer by the time tick() returns, unlike a task (pass 2), which only
    # ever sees the PREVIOUS tick's commit (see
    # test_scheduler_runs_condition_task_only_on_change_tick, above).
    assert sensor.endpoint("temp").get_history() == [21.5]


def test_tick_hook_exception_is_caught_and_does_not_stop_other_hooks():
    from phc.devices.virtual.device import VirtualDevice
    from phc.core.endpoint import Endpoint

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    calls = []

    def failing_hook(devices):
        raise RuntimeError("boom")

    scheduler = Scheduler({"living_light": light},
                           tick_hooks=[failing_hook, lambda devices: calls.append(devices)])

    scheduler.tick(now=0.0)  # must not raise
    assert len(calls) == 1


# ---------- start_hooks / stop_hooks ----------

def _run_forever_in_thread(scheduler: Scheduler, *, timeout: float = 2.0):
    """Drive scheduler.run_forever() on a background daemon thread, wait
    (bounded) for it to actually start ticking, then stop it and join --
    the shared pattern for exercising start_hooks/stop_hooks, which only
    fire around run_forever()'s own loop, never around tick()."""
    import threading

    thread = threading.Thread(target=scheduler.run_forever, daemon=True)
    thread.start()
    deadline = time.time() + timeout
    while not scheduler._running and time.time() < deadline:
        time.sleep(0.01)
    return thread


def test_tick_never_runs_start_or_stop_hooks():
    from phc.devices.virtual.device import VirtualDevice
    from phc.core.endpoint import Endpoint

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    start_calls, stop_calls = [], []

    async def on_start(devices):
        start_calls.append(devices)

    async def on_stop(devices):
        stop_calls.append(devices)

    scheduler = Scheduler({"living_light": light}, start_hooks=[on_start], stop_hooks=[on_stop])

    scheduler.tick(now=0.0)
    scheduler.tick(now=1.0)
    scheduler.close()

    assert start_calls == []
    assert stop_calls == []


def test_run_forever_runs_start_and_stop_hooks_exactly_once():
    from phc.devices.virtual.device import VirtualDevice
    from phc.core.endpoint import Endpoint

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    start_calls, stop_calls = [], []

    async def on_start(devices):
        start_calls.append(devices)

    async def on_stop(devices):
        stop_calls.append(devices)

    scheduler = Scheduler({"living_light": light}, heartbeat=0.01,
                           start_hooks=[on_start], stop_hooks=[on_stop])

    thread = _run_forever_in_thread(scheduler)
    assert len(start_calls) == 1
    assert start_calls[0] == {"living_light": light}
    assert stop_calls == []  # not yet stopped

    scheduler.stop()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert len(stop_calls) == 1
    assert stop_calls[0] == {"living_light": light}


def test_start_and_stop_hook_exceptions_are_caught_and_do_not_block_siblings_or_crash(caplog):
    from phc.devices.virtual.device import VirtualDevice
    from phc.core.endpoint import Endpoint

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    start_calls, stop_calls = [], []

    async def failing_start(devices):
        raise RuntimeError("start boom")

    async def ok_start(devices):
        start_calls.append(devices)

    async def failing_stop(devices):
        raise RuntimeError("stop boom")

    async def ok_stop(devices):
        stop_calls.append(devices)

    scheduler = Scheduler({"living_light": light}, heartbeat=0.01,
                           start_hooks=[failing_start, ok_start],
                           stop_hooks=[failing_stop, ok_stop])

    sched_logger = logging.getLogger("phc.scheduler")
    sched_logger.addHandler(caplog.handler)
    sched_logger.setLevel(logging.ERROR)
    try:
        with caplog.at_level("ERROR", logger="phc.scheduler"):
            thread = _run_forever_in_thread(scheduler)
            assert len(start_calls) == 1  # sibling still ran despite failing_start

            scheduler.stop()
            thread.join(timeout=2.0)
    finally:
        sched_logger.removeHandler(caplog.handler)

    assert not thread.is_alive()  # run_forever() completed, did not crash/hang
    assert len(stop_calls) == 1  # sibling still ran despite failing_stop
    assert "start hook" in caplog.text
    assert "stop hook" in caplog.text


# ---------- heartbeat grid, shutdown latency, and the two clocks ----------

def test_run_forever_holds_the_heartbeat_grid_despite_slow_ticks():
    """The tick period must stay on the heartbeat grid, not become
    `heartbeat + tick duration`. A device that takes ~40ms to read on a
    50ms heartbeat used to yield a ~90ms period (80% slow, compounding
    forever); the deadline grid should keep it near 50ms."""
    from phc.core.endpoint import Endpoint
    from phc.core.device import Device

    class SlowDevice(Device):
        def receive(self) -> dict:
            time.sleep(0.04)
            return {"value": 1}

    ticks: list[float] = []

    def record_tick(devices):
        ticks.append(time.monotonic())

    device = SlowDevice("slow", endpoints=[Endpoint("value")], update_interval=0.0)
    scheduler = Scheduler({"slow": device}, heartbeat=0.05, tick_hooks=[record_tick])

    thread = _run_forever_in_thread(scheduler)
    time.sleep(0.6)
    scheduler.stop()
    thread.join(timeout=2.0)
    assert not thread.is_alive()

    assert len(ticks) >= 5, f"too few ticks to judge the grid: {len(ticks)}"
    periods = [b - a for a, b in zip(ticks, ticks[1:])]
    average = sum(periods) / len(periods)
    # Serial (pre-fix) behaviour would sit at ~0.09s. Allow generous slack
    # for CI scheduling jitter while still failing the old implementation.
    assert average < 0.075, f"heartbeat drifted: average period {average:.3f}s"


def test_stop_interrupts_the_heartbeat_sleep_instead_of_waiting_it_out():
    """stop() must not have to wait out a pending heartbeat sleep. With a
    5s heartbeat, the old flag-only stop() took ~5s to return; waking the
    sleep should make it near-instant."""
    from phc.core.endpoint import Endpoint
    from phc.devices.virtual.device import VirtualDevice

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    scheduler = Scheduler({"living_light": light}, heartbeat=5.0)

    thread = _run_forever_in_thread(scheduler)
    time.sleep(0.1)     # let it get into the heartbeat sleep

    start = time.perf_counter()
    scheduler.stop()
    thread.join(timeout=6.0)
    elapsed = time.perf_counter() - start

    assert not thread.is_alive()
    assert elapsed < 1.0, f"stop() waited out the heartbeat sleep: {elapsed:.3f}s"


def test_device_polling_survives_a_wall_clock_step_backwards():
    """Device intervals run on the monotonic clock, so an NTP correction or
    DST change that moves time.time() backwards must not stall polling.
    Driven through tick()'s explicit clocks: the wall clock jumps back an
    hour while the monotonic clock keeps advancing."""
    from phc.core.endpoint import Endpoint
    from phc.devices.virtual.device import VirtualDevice

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=10.0)
    scheduler = Scheduler({"living_light": light})

    scheduler.tick(now=1_000_000.0, now_mono=100.0)
    assert light.next_due() == 110.0        # expressed in monotonic terms

    # Wall clock jumps back an hour; monotonic advances past the interval.
    scheduler.tick(now=1_000_000.0 - 3600.0, now_mono=111.0)
    assert light.next_due() == 121.0, "device was not re-polled after a wall-clock step back"
    scheduler.close()


def test_task_cooldown_survives_a_wall_clock_step_backwards():
    """min_interval is a cooldown, so it is measured on the monotonic clock
    -- a wall-clock step backwards must not freeze it (nor a step forwards
    end it early)."""
    from phc.core.endpoint import Endpoint
    from phc.devices.virtual.device import VirtualDevice

    light = VirtualDevice("light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=None)
    devices = {"light": light}
    fired: list[float] = []

    class RecordingAction(LogAction):
        def perform(self, devices):
            fired.append(1.0)

    task = Task("cooldown", min_interval=60.0, actions=[RecordingAction()])

    assert task.run(1_000_000.0, devices, 100.0) is True
    assert len(fired) == 1
    # Wall clock jumps back an hour; only 10 monotonic seconds have passed,
    # so the cooldown is still in effect and must still block.
    assert task.run(1_000_000.0 - 3600.0, devices, 110.0) is False
    assert len(fired) == 1
    # Once the cooldown genuinely elapses on the monotonic clock, it fires.
    assert task.run(1_000_000.0 - 3600.0, devices, 161.0) is True
    assert len(fired) == 2
