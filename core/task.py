"""Task system: conditions that gate actions, and the actions (set, toggle,
log, create_task, kill_task, script) a Task can perform against devices."""

import fnmatch
import logging
import math

from core import scripting
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


class ExprCondition:
    """Gates a Task on a restricted-Python boolean expression (see
    core.scripting) instead of Condition's single-endpoint `changed` flag --
    e.g. combining several devices' state with `and`/`or`/comparisons, or
    reading a sticky min/max window. Duck-typed to the same evaluate(devices)
    signature as Condition (Task.run() only ever calls
    self.condition.evaluate(devices)), so no shared base class is needed.

    `refs` (optional) binds short names to endpoints for the attribute-access
    form (`sensor_a.state`); the same endpoints are also reachable inline via
    `state("house.motion1.state")` etc. without any `refs:` entry -- see
    _build_rule_namespace. Built with `writable=False` (no set_state/
    create_task/kill_task/reset_sticky): a condition may be evaluated every
    tick regardless of whether its task ends up firing, so it stays
    side-effect-free by construction, not by convention."""

    def __init__(self, *, compiled: scripting.Compiled, refs: dict[str, str],
                 task_tag: str, flat: dict[str, Device]):
        self._compiled = compiled
        self._refs = refs
        self._task_tag = task_tag
        self._flat = flat

    def evaluate(self, devices: dict[str, Device]) -> bool:
        namespace = _build_rule_namespace(devices=devices, flat=self._flat, tasks=None,
                                           extensions=None, sticky_endpoints=None,
                                           task_tag=self._task_tag, writable=False)
        for name, ref in self._refs.items():
            device_id, endpoint_key = resolve_endpoint_ref(ref)
            namespace[name] = scripting.EndpointRef(device_id, endpoint_key, devices, self._task_tag)
        return bool(scripting.evaluate_expression(self._compiled, namespace))


class Action:
    """Base class for a task's effect. One concrete subclass per `kind`,
    registered via @register_task_kind, analogous to Endpoint subclasses
    registered via @register_endpoint_kind.

    requires_device: True for the common case of an action that targets one
    device/endpoint (resolved by core.config._build_action from the YAML
    `device:` key before construction). An action with no single target
    (e.g. CreateTaskAction, which acts on the task list itself) sets this
    False, opting out of that device resolution -- _build_action then
    passes it `flat`/`tasks`/`extensions`/`task_tag`/`sticky_endpoints`
    instead of `device_id`/`endpoint_key`."""

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
    unchanged, same as writing it directly via device.set().

    `expr` is the dynamic alternative to a literal `value` -- a restricted-
    Python expression (see core.scripting), re-evaluated fresh every time
    this action fires, e.g. to derive one endpoint's value from another's
    current state instead of writing a fixed constant. Exactly one of
    `value`/`expr` must be given. `refs` (optional, expr-only) binds short
    names to endpoints for the attribute-access form, same as
    ExprCondition/ScriptAction. The expr's result is passed to set_text()
    exactly like a literal `value` would be -- so a `values`-mapped
    endpoint whose labels aren't on/off needs the expr itself to produce
    the right raw value/label (e.g. via a ternary), same as a literal
    `value: true` would need today."""

    def __init__(self, *, value=None, expr: str | None = None,
                 refs: dict[str, str] | None = None,
                 flat: dict[str, Device] | None = None,
                 task_tag: str | None = None, **kwargs):
        super().__init__(**kwargs)
        if (value is None) == (expr is None):
            raise ValueError("requires exactly one of 'value' or 'expr'")
        self.compiled = scripting.compile_expression(expr) if expr is not None else None
        self.refs = refs or {}
        self._flat = flat
        self._task_tag = task_tag
        self.params["value"] = value

    def perform(self, devices: dict[str, Device]) -> None:
        if self.compiled is not None:
            namespace = _build_rule_namespace(devices=devices, flat=self._flat, tasks=None,
                                               extensions=None, sticky_endpoints=None,
                                               task_tag=self._task_tag, writable=False)
            for name, ref in self.refs.items():
                device_id, endpoint_key = resolve_endpoint_ref(ref)
                namespace[name] = scripting.EndpointRef(device_id, endpoint_key, devices, self._task_tag)
            value = scripting.evaluate_expression(self.compiled, namespace)
        else:
            value = self.params["value"]
        devices[self.device_id].set_text(value, name=self.endpoint_key)


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


def register_task(specs: dict, flat: dict[str, Device], tasks: list["Task"],
                   extensions: dict[str, object], sticky_endpoints: set) -> "Task":
    """Build `specs` (same shape as a top-level tasks[] entry) into a Task
    and (re-)register it in the live `tasks` list, replacing any existing
    task with the same tag rather than duplicating it -- so a repeatedly-
    firing trigger re-arms the same spawned task instead of accumulating one
    per firing. Shared by CreateTaskAction and the script sandbox's
    create_task() function (see _build_rule_namespace)."""
    from core.config import _build_task  # local import: avoid config<->task import cycle

    new_task = _build_task(specs, flat, tasks, extensions, sticky_endpoints)
    existing = next((t for t in tasks if t.tag == new_task.tag), None)
    if existing is not None:
        tasks.remove(existing)
        logger.info("task %s: replacing existing task", new_task.tag)
    tasks.append(new_task)
    logger.info("task %s created", new_task.tag)
    return new_task


def kill_tasks(patterns, tasks: list["Task"]) -> int:
    """Remove every task from `tasks` whose tag matches any of `patterns`
    (fnmatch glob, same convention as core.selectors -- an exact tag is also
    a valid glob), logging each removal. Returns the number removed. Shared
    by KillTaskAction and the script sandbox's kill_task() function."""
    to_remove = [t for t in tasks if any(fnmatch.fnmatchcase(t.tag, p) for p in patterns)]
    for t in to_remove:
        tasks.remove(t)
        logger.info("task %s killed", t.tag)
    return len(to_remove)


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
                 extensions: dict | None = None, sticky_endpoints: set | None = None, **params):
        super().__init__(device_id="", endpoint_key="", **params)
        self._specs = specs
        self._flat = flat
        self._tasks = tasks
        self._extensions = extensions if extensions is not None else {}
        self._sticky_endpoints = sticky_endpoints if sticky_endpoints is not None else set()

    def perform(self, devices: dict[str, Device]) -> None:
        register_task(self._specs, self._flat, self._tasks, self._extensions, self._sticky_endpoints)


@register_task_kind("kill_task")
class KillTaskAction(Action):
    """Removes every task whose tag matches any of `tags` (fnmatch glob) from
    the live task list -- the declarative counterpart to create_task, and
    the structural equivalent of the previous Tcl system's `KillJob`."""

    requires_device = False

    def __init__(self, *, tags: list[str], flat: dict[str, Device] | None = None,
                 tasks: list["Task"], extensions: dict | None = None, **params):
        super().__init__(device_id="", endpoint_key="", **params)
        self._tags = tags
        self._tasks = tasks

    def perform(self, devices: dict[str, Device]) -> None:
        kill_tasks(self._tags, self._tasks)


def _selector_refs(pattern: str, flat: dict[str, Device]) -> list[str]:
    """The `devices(pattern)` namespace function: expand a selector pattern
    (see core.selectors) into "<qualified_id>.<endpoint_key>" ref strings,
    usable as a `for` target or passed straight to state()/changed()/etc."""
    from core.selectors import resolve_selectors  # local import: avoid cycle (see core/scripting.py)

    return [f"{device_id}.{endpoint_key}" for device_id, endpoint_key in resolve_selectors([pattern], flat)]


def _build_rule_namespace(*, devices: dict[str, Device], flat: dict[str, Device],
                           tasks: list["Task"] | None, extensions: dict[str, object] | None,
                           sticky_endpoints: set | None, task_tag: str, writable: bool) -> dict:
    """The one place that defines what a condition's `expr:` or a script
    action's `code:` can call -- both ExprCondition.evaluate() and
    ScriptAction.perform() build their namespace here, the former with
    `writable=False`, so the two surfaces can never drift apart in what they
    expose and a new function only needs to be added once. See
    docs/scripting.md for the full expr:/code: API reference.

    Always available (read-only): state/changed/text/event/sticky (each
    takes a "device.endpoint" ref string) and devices(pattern) (a selector,
    see core.selectors). Only when `writable` (scripted actions): set_state,
    create_task/kill_task, reset_sticky, and log. `sticky`/`reset_sticky` are
    keyed by `task_tag` as the Endpoint.subscribe_log() subscriber id (see
    core.endpoint) -- core.config subscribes every referenced endpoint under
    that same id at config-build time."""

    def _ref(ref: str) -> scripting.EndpointRef:
        device_id, endpoint_key = resolve_endpoint_ref(ref)
        return scripting.EndpointRef(device_id, endpoint_key, devices, task_tag)

    namespace = {
        "state": lambda ref: _ref(ref).state,
        "changed": lambda ref: _ref(ref).changed,
        "text": lambda ref: _ref(ref).text,
        "event": lambda ref: _ref(ref).event,
        "sticky": lambda ref: _ref(ref).sticky,
        "devices": lambda pattern: _selector_refs(pattern, flat),
    }
    if not writable:
        return namespace

    def _set_state(ref: str, value) -> None:
        device_id, endpoint_key = resolve_endpoint_ref(ref)
        devices[device_id].set_text(value, name=endpoint_key)

    def _reset_sticky(ref: str) -> None:
        device_id, endpoint_key = resolve_endpoint_ref(ref)
        devices[device_id].endpoint(endpoint_key).invalidate_log_value(task_tag)

    namespace.update({
        "set_state": _set_state,
        "create_task": lambda spec: register_task(spec, flat, tasks, extensions, sticky_endpoints),
        "kill_task": lambda *tags: kill_tasks(tags, tasks),
        "reset_sticky": _reset_sticky,
        "log": lambda msg: logger.info(msg),
    })
    return namespace


@register_task_kind("script")
class ScriptAction(Action):
    """Runs a restricted-Python script (see core.scripting) against the
    shared rule namespace (_build_rule_namespace, above) when this task
    fires -- the escape hatch for multi-step scenarios: reading/writing
    device state, spawning/killing tagged sub-tasks, and reading/resetting
    sticky min/max values from one script body, mirroring what the previous
    Tcl system's job script bodies could do.

    `compiled`/`refs` are public (not `_compiled`/`_refs`) so
    core.config._build_action can inspect `compiled.referenced_paths` and
    `refs` after construction, to validate/subscribe every endpoint this
    script references -- the same way _build_condition does for
    ExprCondition (see core.scripting's referenced_paths)."""

    requires_device = False

    def __init__(self, *, code: str, task_tag: str, flat: dict[str, Device],
                 tasks: list["Task"], extensions: dict[str, object] | None = None,
                 sticky_endpoints: set | None = None, refs: dict[str, str] | None = None,
                 **params):
        super().__init__(device_id="", endpoint_key="", **params)
        self.compiled = scripting.compile_script(code)
        self.refs = refs or {}
        self._task_tag = task_tag
        self._flat = flat
        self._tasks = tasks
        self._extensions = extensions if extensions is not None else {}
        self._sticky_endpoints = sticky_endpoints if sticky_endpoints is not None else set()

    def perform(self, devices: dict[str, Device]) -> None:
        namespace = _build_rule_namespace(devices=devices, flat=self._flat, tasks=self._tasks,
                                           extensions=self._extensions,
                                           sticky_endpoints=self._sticky_endpoints,
                                           task_tag=self._task_tag, writable=True)
        for name, ref in self.refs.items():
            device_id, endpoint_key = resolve_endpoint_ref(ref)
            namespace[name] = scripting.EndpointRef(device_id, endpoint_key, devices, self._task_tag)
        scripting.run_script(self.compiled, namespace)


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
        repeat <= 0, the task fires at most once (due_time -> +inf).

    `min_interval` (default 0: no cooldown) is a retrigger cooldown applied
    uniformly to both modes: once fired, run() won't fire again until at
    least `min_interval` seconds have passed, regardless of how often the
    condition holds or due_time is reached in between -- mirrors the
    previous Tcl system's job `-min_interval`, primarily useful for
    debouncing a `changed`/`expr` condition that could otherwise re-fire
    every tick it holds."""

    def __init__(self, tag: str, *, description: str = "", due_time: float,
                 repeat: float = 0.0, min_interval: float = 0.0,
                 condition: Condition | ExprCondition | None = None,
                 actions: list[Action]):
        self.tag = tag
        self.description = description
        self.due_time = due_time
        self.repeat = repeat
        self.min_interval = min_interval
        self.condition = condition
        self.actions = actions
        self._last_fired = float("-inf")

    def due(self, now: float) -> bool:
        """True if run() should be called this tick: always True for
        condition-driven tasks (the condition itself gates whether actions
        fire), else True once `now` reaches due_time."""
        if self.condition is not None:
            return True
        return now >= self.due_time

    def run(self, now: float, devices: dict[str, Device]) -> bool:
        """Perform all actions, in order, if due and not cooling down.
        Returns True iff the actions ran this call. The condition/due-ness
        and min_interval gates are both checked once per call, not per
        action -- every action in the list fires unconditionally once both
        gates pass."""
        if self.condition is not None and not self.condition.evaluate(devices):
            return False
        if self.min_interval and (now - self._last_fired) < self.min_interval:
            return False
        for action in self.actions:
            action.perform(devices)
        self._last_fired = now
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
