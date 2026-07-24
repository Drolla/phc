"""Tests for core.scheduler: the Scheduler's tick loop, tasks, and device polling."""

import logging

import pytest

from core.config import load_system
from core.scheduler import Scheduler
from core.task import Condition, LogAction, Task, ToggleAction
from pathlib import Path
from tests.conftest import fetch_sync


EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "virtual_system.yaml"


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
    from devices.virtual.device import VirtualDevice
    from core.endpoint import Endpoint

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

    living_light = system.devices["living_light"]
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

    desk_lamp = system.devices["house.desk_lamp"]
    desk_lamp.set(75, name="brightness")
    scheduler.tick(now=0.0)

    assert desk_lamp.get("brightness") == 75
    assert desk_lamp.get("power") == 0
    assert desk_lamp.get_text("power") == "off"


def test_scheduler_runs_blink_task_and_reschedules():
    from devices.virtual.device import VirtualDevice
    from core.endpoint import Endpoint

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


def test_scheduler_runs_condition_task_only_on_change_tick(task_log):
    from devices.virtual.device import VirtualDevice
    from core.endpoint import Endpoint

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)

    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    task = Task("report", due_time=float("-inf"), condition=condition,
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


def test_scheduler_create_task_action_spawns_task_that_later_fires():
    from devices.virtual.device import VirtualDevice
    from core.endpoint import Endpoint
    from core.task import CreateTaskAction

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    flat = {"living_light": light}

    tasks: list[Task] = []
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    specs = {
        "tag": "clear_alert",
        "time": "+1s",
        "action": {"kind": "set", "device": "living_light.state", "value": "off"},
    }
    trigger = Task("raise_alert", due_time=float("-inf"), condition=condition,
                    actions=[CreateTaskAction(specs=specs, flat=flat, tasks=tasks)])
    tasks.append(trigger)

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


def test_scheduler_create_task_replaces_prior_same_tag_task_on_retrigger():
    from devices.virtual.device import VirtualDevice
    from core.endpoint import Endpoint
    from core.task import CreateTaskAction

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    flat = {"living_light": light}

    tasks: list[Task] = []
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    specs = {
        "tag": "clear_alert",
        "time": "+1s",
        "action": {"kind": "set", "device": "living_light.state", "value": "off"},
    }
    trigger = Task("raise_alert", due_time=float("-inf"), condition=condition,
                    actions=[CreateTaskAction(specs=specs, flat=flat, tasks=tasks)])
    tasks.append(trigger)

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


def test_scheduler_newly_created_task_visible_within_same_tick_if_already_due():
    from devices.virtual.device import VirtualDevice
    from core.endpoint import Endpoint
    from core.task import CreateTaskAction

    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)
    flat = {"living_light": light}

    tasks: list[Task] = []
    condition = Condition(device_id="living_light", endpoint_key="state", changed=True)
    # Nested spec is a condition-driven task (always due), not time-driven,
    # so it is trivially "due" the instant it's appended -- confirms
    # CPython's index-based list iteration makes an appended task visible
    # within the SAME tick's remaining pass 2 loop.
    specs = {
        "tag": "clear_alert",
        "condition": {"device": "living_light.state", "changed": False},
        "action": {"kind": "set", "device": "living_light.state", "value": "off"},
    }
    trigger = Task("raise_alert", due_time=float("-inf"), condition=condition,
                    actions=[CreateTaskAction(specs=specs, flat=flat, tasks=tasks)])
    tasks.append(trigger)

    scheduler = Scheduler(flat, tasks=tasks)

    light.set("on")
    scheduler.tick(now=0.0)
    scheduler.tick(now=1.0)  # create_task fires; spawned task is unconditionally due,
    # and (per CPython's index-based list iteration) runs within this SAME
    # tick's pass 2, staging a pending "off" write on the virtual device's
    # own buffer -- but VirtualDevice only drains that buffer via receive(),
    # which only runs on the device's own next due fetch (matching the
    # one-tick lag documented in test_scheduler_runs_blink_task_and_reschedules).
    assert "clear_alert" in [t.tag for t in tasks]

    scheduler.tick(now=2.0)
    assert light.get() == "off"
