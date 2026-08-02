"""Tests for extensions.debug_portal.snapshot.build_snapshot(): pure,
no-HTTP construction of one tick's JSON-serializable snapshot. Uses a
minimal hand-built `flat` dict of VirtualDevice/Endpoint instances and
core.task.Task objects, mirroring tests/test_logdb_extension.py's style --
no full YAML system needed."""

from core.device import Device
from core.endpoint import Endpoint
from core.task import Condition, Task
from devices.virtual.device import VirtualDevice
from extensions.debug_portal.snapshot import build_snapshot


class FakeSystem:
    """Stand-in for core.config.System: build_snapshot only ever reads
    .tasks and .heartbeat off it."""

    def __init__(self, tasks=None, heartbeat=1.0):
        self.tasks = tasks if tasks is not None else []
        self.heartbeat = heartbeat


def _commit(endpoint: Endpoint, value):
    endpoint.set(value)
    endpoint.update_state()


# ---------- endpoint rows ----------

def test_endpoint_values_sent_as_repr_strings():
    ep = Endpoint("state", value_type="int")
    device = VirtualDevice("lamp", endpoints=[ep])
    _commit(ep, 5)
    system = FakeSystem()
    snapshot = build_snapshot(system, {"lamp": device}, [("lamp", "state")],
                               tick=1, now=100.0, period=1.0)

    row = snapshot["endpoints"][0]
    assert row["key"] == "lamp/state"
    assert row["state"] == "5"
    assert row["last_valid"] == "5"
    assert row["event"] == "5"


def test_none_and_empty_string_repr_distinguishable():
    ep = Endpoint("state")
    device = VirtualDevice("lamp", endpoints=[ep])
    system = FakeSystem()

    # Never set: get() is None.
    snapshot = build_snapshot(system, {"lamp": device}, [("lamp", "state")],
                               tick=1, now=100.0, period=1.0)
    assert snapshot["endpoints"][0]["state"] == "None"

    # Explicitly set to "" -- update_state() still commits it (see
    # Endpoint.update_state()), distinct from an untouched None.
    _commit(ep, "")
    snapshot = build_snapshot(system, {"lamp": device}, [("lamp", "state")],
                               tick=2, now=101.0, period=1.0)
    assert snapshot["endpoints"][0]["state"] == "''"


def test_event_present_for_exactly_one_tick():
    ep = Endpoint("state", value_type="int")
    device = VirtualDevice("lamp", endpoints=[ep])
    system = FakeSystem()
    pairs = [("lamp", "state")]

    _commit(ep, 5)
    snapshot = build_snapshot(system, {"lamp": device}, pairs, tick=1, now=100.0, period=1.0)
    assert snapshot["endpoints"][0]["event"] == "5"

    # No new set() -- update_state() clears the event even though state holds.
    ep.update_state()
    snapshot = build_snapshot(system, {"lamp": device}, pairs, tick=2, now=101.0, period=1.0)
    assert snapshot["endpoints"][0]["event"] is None
    assert snapshot["endpoints"][0]["state"] == "5"


def test_state_changes_without_event_show_in_last_valid_disagreement():
    """5 -> None -> 5: get() moves twice, but get_event() stays None both
    times, since the second 5 never differs from _last_valid_state -- the
    exact case get_last_valid_state() was added to make visible (see
    core/endpoint.py)."""
    ep = Endpoint("state", value_type="int")
    device = VirtualDevice("lamp", endpoints=[ep])
    system = FakeSystem()
    pairs = [("lamp", "state")]

    _commit(ep, 5)
    build_snapshot(system, {"lamp": device}, pairs, tick=1, now=100.0, period=1.0)

    _commit(ep, None)
    snapshot = build_snapshot(system, {"lamp": device}, pairs, tick=2, now=101.0, period=1.0)
    row = snapshot["endpoints"][0]
    assert row["state"] == "None"
    assert row["event"] is None
    assert row["last_valid"] == "5"

    _commit(ep, 5)
    snapshot = build_snapshot(system, {"lamp": device}, pairs, tick=3, now=102.0, period=1.0)
    row = snapshot["endpoints"][0]
    assert row["state"] == "5"
    assert row["event"] is None  # no event: 5 matches last_valid_state already
    assert row["last_valid"] == "5"


def test_endpoint_age_is_none_before_first_update():
    ep = Endpoint("state")
    device = VirtualDevice("lamp", endpoints=[ep])
    system = FakeSystem()
    snapshot = build_snapshot(system, {"lamp": device}, [("lamp", "state")],
                               tick=1, now=100.0, period=1.0)
    assert snapshot["endpoints"][0]["age"] is None


def test_endpoint_skipped_when_device_missing():
    """A pair referencing a device id not present in `devices` is silently
    skipped, matching extensions.logdb.LogDbInstance.on_tick's own
    device.get()-is-None guard."""
    system = FakeSystem()
    snapshot = build_snapshot(system, {}, [("ghost", "state")], tick=1, now=100.0, period=1.0)
    assert snapshot["endpoints"] == []


# ---------- device poll queue ----------

def test_device_rows_only_include_scheduled_devices():
    scheduled = Device("a", update_interval=10.0)
    unscheduled = Device("b")
    system = FakeSystem()
    snapshot = build_snapshot(system, {"a": scheduled, "b": unscheduled}, [],
                               tick=1, now=100.0, period=1.0)
    assert [d["id"] for d in snapshot["devices"]] == ["a"]
    assert snapshot["devices"][0]["interval"] == 10.0


def test_device_rows_sorted_by_due_in():
    soon = Device("soon", update_interval=10.0)
    soon.mark_run(95.0)  # due at 105.0 -> due_in 5.0 at now=100.0
    later = Device("later", update_interval=100.0)
    later.mark_run(50.0)  # due at 150.0 -> due_in 50.0 at now=100.0
    system = FakeSystem()
    snapshot = build_snapshot(system, {"soon": soon, "later": later}, [],
                               tick=1, now=100.0, period=1.0)
    assert [d["id"] for d in snapshot["devices"]] == ["soon", "later"]
    assert snapshot["devices"][0]["due_in"] == 5.0
    assert snapshot["devices"][1]["due_in"] == 50.0


# ---------- task queue ----------

def test_time_driven_task_due_in_and_repeat():
    task = Task("blink", due_time=103.0, repeat=86400.0, actions=[])
    system = FakeSystem(tasks=[task])
    snapshot = build_snapshot(system, {}, [], tick=1, now=100.0, period=1.0)
    row = snapshot["tasks"][0]
    assert row["mode"] == "time"
    assert row["due_in"] == 3.0
    assert row["repeat"] == 86400.0


def test_one_shot_task_due_time_exhausted_shows_never():
    task = Task("once", due_time=float("inf"), repeat=0.0, actions=[])
    system = FakeSystem(tasks=[task])
    snapshot = build_snapshot(system, {}, [], tick=1, now=100.0, period=1.0)
    row = snapshot["tasks"][0]
    assert row["mode"] == "time"
    assert row["due_in"] is None
    assert row["repeat"] is None


def test_condition_driven_task_has_no_due_in():
    condition = Condition(device_id="d", endpoint_key="k", changed=True)
    task = Task("watch", due_time=float("-inf"), condition=condition, actions=[])
    system = FakeSystem(tasks=[task])
    snapshot = build_snapshot(system, {}, [], tick=1, now=100.0, period=1.0)
    row = snapshot["tasks"][0]
    assert row["mode"] == "cond"
    assert row["due_in"] is None
    assert row["repeat"] is None


def test_task_cooldown_reflects_min_interval_and_last_fired():
    task = Task("debounced", due_time=100.0, min_interval=10.0, actions=[])
    task.run(97.0, {})  # last_fired = 97.0
    system = FakeSystem(tasks=[task])
    snapshot = build_snapshot(system, {}, [], tick=1, now=100.0, period=1.0)
    assert snapshot["tasks"][0]["cooldown"] == 7.0


def test_tasks_sorted_soonest_time_then_condition_then_never():
    soon = Task("soon", due_time=100.4, actions=[])
    condition_task = Task("cond_task", due_time=float("-inf"),
                           condition=Condition(device_id="d", endpoint_key="k"), actions=[])
    never = Task("never", due_time=float("inf"), actions=[])
    system = FakeSystem(tasks=[never, condition_task, soon])
    snapshot = build_snapshot(system, {}, [], tick=1, now=100.0, period=1.0)
    assert [t["tag"] for t in snapshot["tasks"]] == ["soon", "cond_task", "never"]


# ---------- envelope ----------

def test_snapshot_envelope_fields():
    system = FakeSystem(heartbeat=2.5)
    snapshot = build_snapshot(system, {}, [], tick=42, now=1000.0, period=2.6)
    assert snapshot["tick"] == 42
    assert snapshot["time"] == 1000.0
    assert snapshot["heartbeat"] == 2.5
    assert snapshot["period"] == 2.6
