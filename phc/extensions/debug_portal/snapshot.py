"""The debug portal's per-tick snapshot of the running system.

One JSON-serializable dict covering the scheduler's task queue, the device
poll queue, and every selector-matched endpoint's live state. Built fresh
each tick and handed straight to SseHub.broadcast() -- nothing is retained
between calls, so there is no history or diffing here. Free of any
aiohttp/HTTP concern, so it is unit-testable on its own.

Both clocks come from the tick's Now (see phc.core.clock): `now.wall` for
task due_times and endpoint ages, `now.mono` for device poll intervals and
task cooldowns. Subtracting one from the other would compare two unrelated
epochs.
"""

from phc.core.clock import Now
from phc.core.device import Device
from phc.core.task import Task


def _task_sort_key(due_in: float | None, mode: str) -> tuple[int, float]:
    """Planned-execution order: soonest countdown first.

    Then condition-gated tasks (re-evaluated every tick, so due "now"
    in spirit but without a countdown), then tasks that can never fire
    again."""
    if due_in is not None:
        return (0, due_in)
    return (1 if mode in ("cond", "cond+time") else 2, 0.0)


def _describe_task(task: Task, now: Now) -> dict:
    """One row of the task queue.

    See docs/configuration.md#tasks for `mode`'s cond/time/cond+time
    meanings.

    `due_in` is None both for a task with no due_time and for an exhausted
    one (due_time=+inf); `mode` disambiguates the two on the client."""
    has_condition = task.condition is not None
    has_due_time = task.due_time is not None
    mode = "cond+time" if (has_condition and has_due_time) else "cond" if has_condition else "time"
    due_in = (max(0.0, task.due_time - now.wall)
              if has_due_time and task.due_time != float("inf") else None)
    repeat = task.repeat if (has_due_time and task.repeat is not None and task.repeat > 0) else None
    cooldown = max(0.0, task.min_interval - (now.mono - task.last_fired)) if task.min_interval else 0.0
    return {
        "tag": task.tag,
        "mode": mode,
        "due_in": due_in,
        "repeat": repeat,
        "cooldown": cooldown,
    }


def _describe_device(qualified_id: str, device: Device, mono: float) -> dict:
    """One row of the device poll queue.

    Only called for a device with an update_interval (see
    build_snapshot's filter), so next_due() is finite here."""
    health = device.health
    return {
        "id": qualified_id,
        "interval": device.update_interval,
        "due_in": max(0.0, device.next_due() - mono),
        # A failing device keeps its last-good endpoint values, so the
        # endpoint table alone cannot show that it stopped answering.
        "healthy": health.healthy,
        "failures": health.consecutive_failures,
        "last_error": health.last_error,
    }


def _describe_endpoint(qualified_id: str, endpoint_key: str, endpoint, wall: float) -> dict:
    """One row of the endpoint table.

    `state`/`last_valid` are repr() strings rather than to_text(): to_text()
    applies the endpoint's `values:`/unit display mapping, which hides the
    actual value -- the wrong thing to show in a debugger -- and repr() is
    the only representation that tells a None state apart from the string
    "None" or "". `event` is a plain JSON null when no event fired, so the
    client's highlight logic is a truthy check, not a string comparison."""
    event = endpoint.get_event()
    update_time = endpoint.get_update_time()
    return {
        "key": f"{qualified_id}/{endpoint_key}",
        "device": qualified_id,
        "endpoint": endpoint_key,
        "state": repr(endpoint.get()),
        "last_valid": repr(endpoint.get_last_valid_state()),
        "event": repr(event) if event is not None else None,
        "age": None if update_time == 0.0 else max(0.0, wall - update_time),
    }


def build_snapshot(system, devices: dict[str, Device], pairs: list[tuple[str, str]], *,
                    tick: int, now: Now | float, period: float) -> dict:
    """Build one tick's snapshot.

    Tasks in planned-execution order, every scheduled device by next
    poll time, and every (device, endpoint) pair in `pairs`.

    `now` also accepts a bare number, expanded to both clocks reading that
    instant. `period` is the caller's measured wall-clock delta since the
    previous call -- an overrun indicator, covering the whole tick period
    including the heartbeat sleep."""
    now = Now.coerce(now)
    tasks = [_describe_task(task, now) for task in system.tasks]
    tasks.sort(key=lambda t: _task_sort_key(t["due_in"], t["mode"]))

    device_rows = [_describe_device(qid, device, now.mono) for qid, device in devices.items()
                   if device.update_interval is not None]
    device_rows.sort(key=lambda d: d["due_in"])

    endpoints = [_describe_endpoint(qid, key, devices[qid].endpoint(key), now.wall)
                 for qid, key in pairs if qid in devices]

    return {
        "tick": tick,
        "time": now.wall,
        "heartbeat": system.heartbeat,
        "period": period,
        "tasks": tasks,
        "devices": device_rows,
        "endpoints": endpoints,
    }
