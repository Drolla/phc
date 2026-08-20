"""Pure construction of one JSON-serializable snapshot of the running
system's internals -- the scheduler's task queue, the device poll queue,
and every selected endpoint's live state/event/last_valid_state -- kept
free of any aiohttp/HTTP concern so it is unit-testable on its own (see
phc/extensions/web_ui/widgets.py's describe_endpoint()/describe_device() for
the same separation). Called once per tick by
phc.extensions.debug_portal.extension.DebugPortalInstance.on_tick()."""

from phc.core.device import Device
from phc.core.task import Task


def _task_sort_key(due_in: float | None, mode: str) -> tuple[int, float]:
    """Planned-execution-order sort key: soonest numeric due_in first, then
    condition-gated tasks with no due_time countdown (re-evaluated every
    tick, so due "now" in spirit but not a countdown), then tasks that will
    never fire again (due_time exhausted, repeat <= 0, no condition to
    re-arm them)."""
    if due_in is not None:
        return (0, due_in)
    if mode in ("cond", "cond+time"):
        return (1, 0.0)
    return (2, 0.0)


def _describe_task(task: Task, now: float, now_mono: float) -> dict:
    """Mirrors phc.core.scheduler.Scheduler._log_task_countdown's own due-time
    classification, but as structured fields rather than a single debug
    log line. See docs/configuration.md#tasks for `mode`'s cond/time/
    cond+time meanings. `due_in` is None for a task with no due_time
    schedule, or an exhausted one (due_time=+inf) -- `mode` disambiguates
    the two on the client.

    Needs both clocks: `due_in` counts down to an absolute wall-clock
    due_time, while `cooldown` measures against Task.last_fired, which is
    monotonic (see phc.core.task.Task.run)."""
    has_condition = task.condition is not None
    has_due_time = task.due_time is not None
    mode = "cond+time" if (has_condition and has_due_time) else "cond" if has_condition else "time"
    if not has_due_time or task.due_time == float("inf"):
        due_in = None
    else:
        due_in = max(0.0, task.due_time - now)
    repeat = task.repeat if (has_due_time and task.repeat is not None and task.repeat > 0) else None
    cooldown = max(0.0, task.min_interval - (now_mono - task.last_fired)) if task.min_interval else 0.0
    return {
        "tag": task.tag,
        "mode": mode,
        "due_in": due_in,
        "repeat": repeat,
        "cooldown": cooldown,
    }


def _describe_device(qualified_id: str, device: Device, now_mono: float) -> dict:
    """One row of the device poll queue -- only ever called for a device
    with an update_interval (see build_snapshot's own filter), so
    next_due() is always finite here.

    Takes the MONOTONIC clock: Device.next_due() is expressed in whatever
    clock the Scheduler drives due()/mark_run() with, which is
    time.monotonic() (see phc.core.scheduler.Scheduler's class docstring).
    Subtracting a wall-clock reading from it would yield a meaningless
    difference of two unrelated epochs."""
    health = device.health
    return {
        "id": qualified_id,
        "interval": device.update_interval,
        "due_in": max(0.0, device.next_due() - now_mono),
        # A device whose fetches are failing keeps its last-good endpoint
        # values, so the endpoint table alone cannot show that it has
        # stopped answering -- see phc.core.health.
        "healthy": health.healthy,
        "failures": health.consecutive_failures,
        "last_error": health.last_error,
    }


def _describe_endpoint(qualified_id: str, endpoint_key: str, endpoint, now: float) -> dict:
    """`state`/`last_valid` are sent as repr() strings, not to_text():
    to_text() applies the endpoint's `values:`/unit display mapping, which
    hides the actual value -- the wrong thing to show in a debugger -- and
    repr() is the only representation that tells a None state apart from
    the string "None" or "" on the wire. `event` is a plain JSON null (not
    a repr'd "None") when no event fired this tick, so the client's
    highlight logic is a single truthy check rather than a string
    comparison."""
    event = endpoint.get_event()
    update_time = endpoint.get_update_time()
    return {
        "key": f"{qualified_id}/{endpoint_key}",
        "device": qualified_id,
        "endpoint": endpoint_key,
        "state": repr(endpoint.get()),
        "last_valid": repr(endpoint.get_last_valid_state()),
        "event": repr(event) if event is not None else None,
        "age": None if update_time == 0.0 else max(0.0, now - update_time),
    }


def build_snapshot(system, devices: dict[str, Device], pairs: list[tuple[str, str]], *,
                    tick: int, now: float, period: float,
                    now_mono: float | None = None) -> dict:
    """Build one tick's full snapshot: `system.tasks` in planned-execution
    order, every scheduled device (has an update_interval) by next poll
    time, and every selector-matched (device, endpoint) pair in `pairs`.
    No history/diffing -- called fresh each tick and hand it straight to
    phc.extensions.debug_portal.server.SseHub.broadcast(); nothing here is
    retained across calls. `period` is the caller's own measured wall-clock
    delta since the previous call (an overrun indicator: the actual tick
    period including the heartbeat sleep, not this tick's processing time
    alone).

    `now` is wall-clock (task due_times, endpoint ages); `now_mono` is the
    monotonic clock the Scheduler drives device intervals and task
    cooldowns with (see phc.core.scheduler.Scheduler). It defaults to `now`
    only so a caller with a single synthetic timeline (the tests) stays
    simple -- the live caller passes both."""
    if now_mono is None:
        now_mono = now
    tasks = [_describe_task(task, now, now_mono) for task in system.tasks]
    tasks.sort(key=lambda t: _task_sort_key(t["due_in"], t["mode"]))

    device_rows = [_describe_device(qid, device, now_mono) for qid, device in devices.items()
                   if device.update_interval is not None]
    device_rows.sort(key=lambda d: d["due_in"])

    endpoints = []
    for qualified_id, endpoint_key in pairs:
        device = devices.get(qualified_id)
        if device is None:
            continue
        endpoints.append(_describe_endpoint(qualified_id, endpoint_key,
                                             device.endpoint(endpoint_key), now))

    return {
        "tick": tick,
        "time": now,
        "heartbeat": system.heartbeat,
        "period": period,
        "tasks": tasks,
        "devices": device_rows,
        "endpoints": endpoints,
    }
