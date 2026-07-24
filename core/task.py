"""Task system: conditions that gate actions, and the actions (set, toggle,
log, create_task) a Task can perform against devices."""

import logging
import math

from core.device import Device
from core.registry import register_task_kind

logger = logging.getLogger("phc.tasks")


def resolve_endpoint_ref(ref: str, *, allow_bare: bool = False) -> tuple[str, str | None]:
    """Split "house.desk_lamp.power" into ("house.desk_lamp", "power").

    Endpoint keys are always the final dotted segment, and qualified device
    ids are only ever built by joining ancestor ids with "." -- so splitting
    on the LAST dot is correct and unambiguous regardless of nesting depth.

    If `allow_bare` is True, a `ref` with no dot names a device with no
    specific endpoint -- returned as (ref, None). Callers then fall back to
    whole-device semantics (Device.get()/set()/get_event() with name=None),
    e.g. LogAction reporting every endpoint when none is named. Conditions
    still require a dot (allow_bare defaults to False) since get_event(None)
    on a multi-endpoint device returns a dict, which is never None -- a
    "changed" gate would incorrectly always hold."""
    if "." not in ref:
        if allow_bare:
            return ref, None
        raise ValueError(f"invalid device/endpoint reference {ref!r}: expected 'device.endpoint'")
    device_id, _, endpoint_key = ref.rpartition(".")
    return device_id, endpoint_key


class Condition:
    """Gates a Task's action on a device endpoint's change event.

    Since the Scheduler's pass 3 commits update_state() for every device
    every tick (not just polled/due ones -- a device's state may also
    change via a task's own action), get_event() is always freshly
    computed as of the PREVIOUS tick's commit by the time a task runs."""

    def __init__(self, *, device_id: str, endpoint_key: str, changed: bool = True):
        self.device_id = device_id
        self.endpoint_key = endpoint_key
        self.changed = changed

    def evaluate(self, devices: dict[str, Device]) -> bool:
        """True if the condition holds: always True when `changed` is False,
        else True only if the referenced endpoint has a change event this tick."""
        if not self.changed:
            return True
        device = devices[self.device_id]
        return device.get_event(self.endpoint_key) is not None


class Action:
    """Base class for a task's effect. One concrete subclass per `kind`,
    registered via @register_task_kind, analogous to Endpoint subclasses
    registered via @register_endpoint_kind.

    requires_device: True for the common case of an action that targets one
    device/endpoint (resolved by core.config._build_action from the YAML
    `device:` key before construction). An action with no single target
    (e.g. CreateTaskAction, which acts on the task list itself) sets this
    False, opting out of that device resolution -- _build_action then
    passes it `flat`/`tasks`/`extensions` instead of `device_id`/
    `endpoint_key`."""

    kind: str = "generic"
    requires_device: bool = True

    def __init__(self, *, device_id: str, endpoint_key: str | None, **params):
        self.device_id = device_id
        self.endpoint_key = endpoint_key
        self.params = params

    def perform(self, devices: dict[str, Device]) -> None:
        """Carry out this action's effect against the live device set."""
        raise NotImplementedError


@register_task_kind("set")
class SetAction(Action):
    """Set the target endpoint/device to `value`, a raw value (e.g. 0, 1,
    22.5) or display text (e.g. "on", "off") -- translated into the
    endpoint's raw value via Endpoint.from_text() (see core/endpoint.py),
    so a `values`-mapped endpoint accepts either its raw keys or their text
    labels. An endpoint with no declared type/values passes `value` through
    unchanged, same as writing it directly via device.set()."""

    def perform(self, devices: dict[str, Device]) -> None:
        devices[self.device_id].set_text(self.params["value"], name=self.endpoint_key)


@register_task_kind("toggle")
class ToggleAction(Action):
    """Flip the target endpoint between its two declared `values` (e.g.
    0/1 for a {0: "off", 1: "on"} mapping), or between the literal strings
    "on"/"off" as a fallback for an endpoint with no such two-entry mapping
    (including a bare device reference with no single endpoint to inspect)."""

    def perform(self, devices: dict[str, Device]) -> None:
        device = devices[self.device_id]
        ep = device.endpoint(self.endpoint_key) if self.endpoint_key else None
        if ep is not None and ep.values is not None and len(ep.values) == 2:
            other = next(raw for raw in ep.values if raw != ep.get())
            device.set(other, name=self.endpoint_key)
            return
        current = device.get(self.endpoint_key)
        device.set("off" if current == "on" else "on", name=self.endpoint_key)


@register_task_kind("log")
class LogAction(Action):
    """Log `message` (a str.format() template with `state`/`text` available),
    formatted against the target endpoint's/device's current value."""

    def perform(self, devices: dict[str, Device]) -> None:
        """If endpoint_key is None (device given without an endpoint, e.g.
        `device: "meteo-bern"`), device.get(None)/get_text(None) already
        report every endpoint (and child) as a dict -- so `state`/`text`
        print the whole device's state rather than a single value.

        `state` is the raw value (as before); `text` is the endpoint's
        formatted display text (see Endpoint.to_text), e.g. "on" for a
        `values`-mapped endpoint whose raw state is 1."""
        device = devices[self.device_id]
        state = device.get(self.endpoint_key)
        text = device.get_text(self.endpoint_key)
        message = self.params.get("message", "").format(state=state, text=text)
        logger.info(message)


@register_task_kind("create_task")
class CreateTaskAction(Action):
    """Builds a new Task from a nested `specs:` task definition (same shape
    as a top-level tasks[] entry) and appends it to the live task list --
    so a firing task can spawn a follow-up task at runtime (e.g. "turn this
    off again in 1s") instead of it having to be pre-declared as an
    independent, permanently-resident task in YAML. The nested spec is
    parsed and validated lazily, the first time this fires -- a broken spec
    raises then, caught like any other action failure by the Scheduler's
    per-task exception handler, not at config-load time.

    If a task with the same tag already exists, it is replaced rather than
    duplicated -- so a repeatedly-firing trigger (e.g. a condition that can
    hold many times over the process's lifetime) re-arms the same spawned
    task instead of accumulating one per firing."""

    requires_device = False

    def __init__(self, *, specs: dict, flat: dict[str, Device], tasks: list["Task"],
                 extensions: dict | None = None, **params):
        super().__init__(device_id="", endpoint_key="", **params)
        self._specs = specs
        self._flat = flat
        self._tasks = tasks
        self._extensions = extensions if extensions is not None else {}

    def perform(self, devices: dict[str, Device]) -> None:
        """Parse `specs` into a Task and (re-)register it in the live task list."""
        from core.config import _build_task  # local import: avoid config<->task import cycle

        new_task = _build_task(self._specs, self._flat, self._tasks, self._extensions)
        existing = next((t for t in self._tasks if t.tag == new_task.tag), None)
        if existing is not None:
            self._tasks.remove(existing)
            logger.info("task %s: replacing existing task", new_task.tag)
        self._tasks.append(new_task)
        logger.info("task %s created", new_task.tag)


class Task:
    """A scheduled or condition-driven unit of work.

    Two firing modes, both driven by the Scheduler's tick(now):
      - Condition-driven (condition is not None): due() is always True;
        run() only performs the actions if condition.evaluate() is True.
        `repeat`/`due_time` are not used for scheduling in this mode. Since
        the Scheduler runs tasks before committing this tick's fetch (see
        core/scheduler.py), a `changed` condition can only observe what was
        committed by the PREVIOUS tick -- it reacts to a device's change
        one tick after that value was actually fetched, never the same tick.
      - Time-driven (condition is None): due() fires once due_time is
        reached. If repeat > 0, mark_run() advances due_time by whole
        multiples of repeat (mirrors parse_time's own repeat-rollforward,
        so a stalled process catches up instead of firing a burst). If
        repeat <= 0, the task fires at most once (due_time -> +inf)."""

    def __init__(self, tag: str, *, description: str = "", due_time: float,
                 repeat: float = 0.0, condition: Condition | None = None,
                 actions: list[Action]):
        self.tag = tag
        self.description = description
        self.due_time = due_time
        self.repeat = repeat
        self.condition = condition
        self.actions = actions

    def due(self, now: float) -> bool:
        """True if run() should be called this tick: always True for
        condition-driven tasks (the condition itself gates whether actions
        fire), else True once `now` reaches due_time."""
        if self.condition is not None:
            return True
        return now >= self.due_time

    def run(self, now: float, devices: dict[str, Device]) -> bool:
        """Perform all actions, in order, if due. Returns True iff the
        actions ran this call (always True for time-driven tasks; for
        condition-driven tasks, only when the condition holds). The
        condition/due-ness is checked once per call, not per action --
        every action in the list fires unconditionally once that gate
        passes."""
        if self.condition is not None and not self.condition.evaluate(devices):
            return False
        for action in self.actions:
            action.perform(devices)
        return True

    def mark_run(self, now: float) -> None:
        """Advance due_time past `now` for a time-driven task (see class
        docstring); no-op for condition-driven tasks."""
        if self.condition is not None:
            return
        if self.repeat and self.repeat > 0:
            self.due_time += self.repeat * math.ceil((now - self.due_time + 1e-9) / self.repeat)
        else:
            self.due_time = float("inf")
