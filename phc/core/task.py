"""Task system: conditions, actions, and the live task registry.

Conditions gate actions; actions (set, toggle, log, create_task,
kill_task, script) are what a Task performs against devices; the
registry holds the live task set.

Expression conditions and scripted actions share one namespace of callables
(_build_rule_namespace), so the two surfaces cannot drift apart -- see
docs/scripting.md for the full expr:/code: API reference.
"""

import fnmatch
import logging
import math

from phc.core import scripting
from phc.core.clock import Now
from phc.core.device import Device
from phc.core.registry import register_task_kind

logger = logging.getLogger("phc.tasks")


def resolve_endpoint_ref(ref: str, *, allow_bare: bool = False) -> tuple[str, str | None]:
    """Split "house.desk_lamp.power" into ("house.desk_lamp", "power").

    Splits on the LAST dot (qualified device ids join ancestors with
    "."), so this is correct at any nesting depth.

    `allow_bare` lets a dotless `ref` name a whole device, returned as
    (ref, None) -- callers then use whole-device semantics
    (Device.get()/set()/get_event() with name=None). Conditions require a
    dot: get_event(None) on a multi-endpoint device returns a dict, never
    None, so a "changed" gate would incorrectly always hold."""
    if "." not in ref:
        if allow_bare:
            return ref, None
        raise ValueError(f"invalid device/endpoint reference {ref!r}: expected 'device.endpoint'")
    device_id, _, endpoint_key = ref.rpartition(".")
    return device_id, endpoint_key


class Condition:
    """Gates on a device endpoint's change event and/or current state.

    `changed`/`value`, independently ANDed together. None = no
    constraint.

    changed=True fires on the tick the event happens; changed=False fires
    only when no event occurs. value=X checks current state (level: holds
    every tick at X; or edge: only the transition tick, if changed=True).

    get_event() is freshly computed each tick, so gating observes the
    PREVIOUS tick's state changes."""

    def __init__(self, *, device_id: str, endpoint_key: str,
                 changed: bool | None = None, value=None):
        self.device_id = device_id
        self.endpoint_key = endpoint_key
        self.changed = changed
        self.value = value

    def evaluate(self, devices: dict[str, Device]) -> bool:
        """True if both filters hold; an unset filter holds trivially."""
        device = devices[self.device_id]
        if self.changed is not None:
            if (device.get_event(self.endpoint_key) is not None) != self.changed:
                return False
        return self.value is None or device.get(self.endpoint_key) == self.value


class ExprCondition:
    """Gates a Task on a restricted-Python boolean expression.

    See phc.core.scripting. Rather than Condition's single-endpoint
    `changed` flag -- e.g. combining several devices' state with
    `and`/`or`, or reading a sticky min/max window. Duck-typed to
    Condition's evaluate(devices) signature.

    Built with `writable=False`: a condition may be evaluated every tick
    regardless of whether its task ends up firing, so it is
    side-effect-free by construction, not by convention."""

    def __init__(self, *, compiled: scripting.Compiled, refs: dict[str, str],
                 task_tag: str, flat: dict[str, Device]):
        self._compiled = compiled
        self._refs = refs
        self._task_tag = task_tag
        self._flat = flat

    def evaluate(self, devices: dict[str, Device]) -> bool:
        namespace = _build_rule_namespace(devices=devices, flat=self._flat, tasks=None,
                                           task_tag=self._task_tag, writable=False,
                                           refs=self._refs)
        return bool(scripting.evaluate_expression(self._compiled, namespace))


class Action:
    """Base class for a task's effect.

    One concrete subclass per `kind`, registered via @register_task_kind.

    `device_id`/`endpoint_key` are None when a kind doesn't target a
    device (CreateTaskAction/KillTaskAction/ScriptAction, extension
    actions) or when a device-oriented kind's `device:` was left out (e.g.
    LogAction's unformatted form) -- a kind that always needs one
    (SetAction/ToggleAction) simply isn't built to tolerate None, so
    omitting `device:` there fails at perform(), not at config load.

    `flat`/`tasks`/`extensions`/`task_tag`/`sticky_endpoints` are the live
    config-build context, passed to every action regardless of kind;
    ordinary device-oriented actions just ignore them."""

    kind: str = "generic"

    def __init__(self, *, device_id: str | None = None, endpoint_key: str | None = None,
                 flat: dict[str, Device] | None = None, tasks: "TaskRegistry | None" = None,
                 extensions: dict[str, object] | None = None, task_tag: str | None = None,
                 sticky_endpoints: set | None = None, **params):
        self.device_id = device_id
        self.endpoint_key = endpoint_key
        self.flat = flat
        self.tasks = tasks
        self.extensions = extensions
        self.task_tag = task_tag
        self.sticky_endpoints = sticky_endpoints
        self.params = params

    def perform(self, devices: dict[str, Device]) -> None:
        """Carry out this action's effect against the live device set."""
        raise NotImplementedError


@register_task_kind("set")
class SetAction(Action):
    """Set the target endpoint/device to `value`.

    A raw value or display text, translated via Endpoint.from_text() so
    a `values`-mapped endpoint accepts either form.

    `expr` is the dynamic alternative: a restricted-Python expression (see
    phc.core.scripting), re-evaluated fresh each firing and passed to
    set_text() exactly like a literal `value` would be. Exactly one of
    `value`/`expr` is required; `refs` (expr-only) binds short names to
    endpoints for the attribute-access form."""

    def __init__(self, *, value=None, expr: str | None = None,
                 refs: dict[str, str] | None = None, **kwargs):
        super().__init__(**kwargs)
        if (value is None) == (expr is None):
            raise ValueError("requires exactly one of 'value' or 'expr'")
        self.compiled = scripting.compile_expression(expr) if expr is not None else None
        self.refs = refs or {}
        self.params["value"] = value

    def perform(self, devices: dict[str, Device]) -> None:
        if self.compiled is not None:
            namespace = _build_rule_namespace(devices=devices, flat=self.flat, tasks=None,
                                               task_tag=self.task_tag, writable=False,
                                               refs=self.refs)
            value = scripting.evaluate_expression(self.compiled, namespace)
        else:
            value = self.params["value"]
        devices[self.device_id].set_text(value, name=self.endpoint_key)


@register_task_kind("toggle")
class ToggleAction(Action):
    """Flip the target endpoint between its two declared `values`.

    E.g. 0/1 for a {0: "off", 1: "on"} mapping. Falls back to the
    literal strings "on"/"off" for an endpoint with no such two-entry
    mapping (including a bare device reference with no single endpoint
    to inspect)."""

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
    """Log `message`, formatted against the target's current value.

    `message` is a str.format() template with `state`/`text` available
    (the target endpoint's/device's current value)."""

    def perform(self, devices: dict[str, Device]) -> None:
        """If device_id is None, log `message` as-is.

        No `device:` given means no state/text to format in, so a
        template placeholder would raise.

        If endpoint_key is None (device given without an endpoint, e.g.
        `device: "meteo-bern"`), device.get(None)/get_text(None) already
        report every endpoint (and child) as a dict -- so `state`/`text`
        print the whole device's state rather than a single value.

        `state` is the raw value; `text` is the endpoint's formatted
        display text (see Endpoint.to_text), e.g. "on" for a
        `values`-mapped endpoint whose raw state is 1."""
        message = self.params.get("message", "")
        if self.device_id is not None:
            device = devices[self.device_id]
            state = device.get(self.endpoint_key)
            text = device.get_text(self.endpoint_key)
            message = message.format(state=state, text=text)
        logger.info(message)


class TaskRegistry:
    """The live set of Tasks, plus the context to build more at runtime.

    Building a Task from a spec dict is the config loader's job, but
    `create_task`/`kill_task` need it at *runtime*, from inside this
    module. The builder is injected here instead of imported (see
    `build_task`), so the dependency runs one way: config imports task,
    never the reverse.

    Iterable and equipped with `remove()`, which is the entire interface
    `phc.core.scheduler` needs -- a plain list satisfies it too, so either
    works there.
    """

    def __init__(self, tasks: list["Task"] | None = None, *, build_task=None,
                 flat: dict[str, Device] | None = None,
                 extensions: dict[str, object] | None = None,
                 sticky_endpoints: set | None = None,
                 task_specs: dict[str, dict] | None = None):
        self._tasks: list[Task] = list(tasks or [])
        # callable(spec, registry) -> Task; supplied by the config loader.
        # None means this registry can hold tasks but not build new ones --
        # which is all a hand-constructed registry in a unit test needs.
        self._build_task = build_task
        # The build context every runtime-created Task needs resolved
        # against: the flat device tree, the configured extension
        # instances, the shared sticky-endpoint set, and the named
        # `task_specs:` templates.
        self.flat = flat if flat is not None else {}
        self.extensions = extensions if extensions is not None else {}
        self.sticky_endpoints = sticky_endpoints if sticky_endpoints is not None else set()
        self.task_specs = task_specs

    # ---------- container protocol ----------

    def __iter__(self):
        return iter(self._tasks)

    def __len__(self) -> int:
        return len(self._tasks)

    def __getitem__(self, index):
        return self._tasks[index]

    def __contains__(self, task) -> bool:
        return task in self._tasks

    def remove(self, task: "Task") -> None:
        """Drop `task` from the live set.

        The Scheduler's one-shot retirement path. Raises ValueError if
        it isn't present, same as list.remove."""
        self._tasks.remove(task)

    def add(self, task: "Task") -> None:
        """Append an already-built Task."""
        self._tasks.append(task)

    def by_tag(self, tag: str) -> "Task | None":
        """The task with this exact tag, or None.

        Tags are unique within a registry -- create() replaces rather
        than duplicates."""
        return next((t for t in self._tasks if t.tag == tag), None)

    # ---------- runtime creation / removal ----------

    def create(self, specs: dict) -> "Task":
        """Build `specs` into a Task and (re-)register it.

        `specs` is the same shape as a top-level tasks[] entry.
        Replaces any existing task with the same tag rather than
        duplicating it -- so a repeatedly-firing trigger re-arms the
        same spawned task instead of accumulating one per firing.
        `specs` may give `template: <name>` instead, resolved against
        this registry's `task_specs`."""
        if self._build_task is None:
            raise ValueError(
                "this TaskRegistry cannot build tasks: no build_task was supplied "
                "(a registry built by phc.core.config.load_system always has one)")
        if "template" in specs:
            task_specs = self.task_specs or {}
            name = specs["template"]
            if name not in task_specs:
                raise ValueError(f"create_task: unknown template {name!r}; "
                                  f"available: {sorted(task_specs)}")
            specs = task_specs[name]

        new_task = self._build_task(specs, self)
        existing = self.by_tag(new_task.tag)
        if existing is not None:
            self._tasks.remove(existing)
            logger.info("%s: replacing existing task", new_task.tag)
        self._tasks.append(new_task)
        logger.info("%s created", new_task.tag)
        return new_task

    def kill(self, patterns) -> int:
        """Remove every task whose tag matches any of `patterns`.

        fnmatch glob; an exact tag is also a valid glob. Returns the
        number removed."""
        to_remove = [t for t in self._tasks
                     if any(fnmatch.fnmatchcase(t.tag, p) for p in patterns)]
        for t in to_remove:
            self._tasks.remove(t)
            logger.info("%s killed", t.tag)
        return len(to_remove)


@register_task_kind("create_task")
class CreateTaskAction(Action):
    """Builds a Task from a nested `specs:` or a named `template:`.

    Appends it to the live task list -- so a firing task can spawn a
    follow-up (e.g. "turn this off again in 1s") without it being
    pre-declared as its own resident task in YAML. Exactly one of
    `specs`/`template` is required.

    The spec is parsed lazily, the first time this fires, so a broken spec
    raises there -- an action failure, not a config-load one."""

    def __init__(self, *, specs: dict | None = None, template: str | None = None,
                 tasks: "TaskRegistry", **params):
        super().__init__(tasks=tasks, **params)
        if (specs is None) == (template is None):
            raise ValueError("create_task: specify exactly one of 'specs' or 'template'")
        self._specs = specs if specs is not None else {"template": template}

    def perform(self, devices: dict[str, Device]) -> None:
        self.tasks.create(self._specs)


@register_task_kind("kill_task")
class KillTaskAction(Action):
    """Removes every task whose tag matches any of `tags` (fnmatch glob).

    From the live task list -- the declarative counterpart to
    create_task."""

    def __init__(self, *, tags: list[str], tasks: "TaskRegistry", **params):
        super().__init__(tasks=tasks, **params)
        self._tags = tags

    def perform(self, devices: dict[str, Device]) -> None:
        self.tasks.kill(self._tags)


def _selector_refs(pattern: str, flat: dict[str, Device]) -> list[str]:
    """The `devices(pattern)` namespace function.

    Expands a selector pattern (see phc.core.selectors) into
    "<qualified_id>.<endpoint_key>" ref strings, usable as a `for`
    target or passed straight to state()/changed()/etc."""
    from phc.core.selectors import resolve_selectors  # local import: avoid cycle (see phc/core/scripting.py)

    return [f"{device_id}.{endpoint_key}" for device_id, endpoint_key in resolve_selectors([pattern], flat)]


def _pool(refs, devices: dict[str, Device], task_tag: str) -> list:
    """Concatenate the recorded history of one ref or a list of refs.

    Backs history()/fractile()/median()/average(), letting a script
    combine several sensors' buffers into one sorted set. Raises
    ValueError (via EndpointRef.history) for a ref with no `history:`
    declared."""
    ref_list = [refs] if isinstance(refs, str) else refs
    pool = []
    for ref in ref_list:
        device_id, endpoint_key = resolve_endpoint_ref(ref)
        pool.extend(scripting.EndpointRef(device_id, endpoint_key, devices, task_tag).history)
    return pool


def _fractile(pool: list, f: float):
    """The value at relative position `f` (0.0-1.0) in `pool` once sorted.

    An actually recorded sample, never interpolated. None on an empty
    pool. Raises ValueError if f is outside [0, 1] (a negative f would
    otherwise silently index from the end of the list).

    Uses `int(x + 0.5)`, not round(): it must round half away from zero,
    where Python's round() (banker's rounding) would diverge at f=0.5 on
    an even-length pool."""
    if not 0.0 <= f <= 1.0:
        raise ValueError(f"fractile: f must be between 0 and 1, got {f!r}")
    if not pool:
        return None
    return sorted(pool)[int((len(pool) - 1) * f + 0.5)]


def _average(pool: list):
    """Arithmetic mean of `pool`, or None if empty (see _pool/_fractile)."""
    return sum(pool) / len(pool) if pool else None


def _build_rule_namespace(*, devices: dict[str, Device], flat: dict[str, Device],
                           tasks: "TaskRegistry | None", task_tag: str,
                           writable: bool, refs: dict[str, str] | None = None) -> dict:
    """The one place defining a condition/script's callable namespace.

    See docs/scripting.md for the full reference. Read-only calls are
    always present; `writable` (scripted actions only)
    adds the mutating ones. `refs` binds short names to endpoints for the
    attribute-access form, on top of the always-available
    state("house.motion1.state") form.

    `sticky`/`reset_sticky` are keyed by `task_tag` as the
    Endpoint.subscribe_log() subscriber id."""

    def _ref(ref: str) -> scripting.EndpointRef:
        device_id, endpoint_key = resolve_endpoint_ref(ref)
        return scripting.EndpointRef(device_id, endpoint_key, devices, task_tag)

    namespace = {
        "state": lambda ref: _ref(ref).state,
        "changed": lambda ref: _ref(ref).changed,
        "text": lambda ref: _ref(ref).text,
        "event": lambda ref: _ref(ref).event,
        "sticky": lambda ref: _ref(ref).sticky,
        "available": lambda ref: _ref(ref).available,
        "age": lambda ref: _ref(ref).age,
        "history": lambda refs: _pool(refs, devices, task_tag),
        "fractile": lambda refs, f: _fractile(_pool(refs, devices, task_tag), f),
        "median": lambda refs: _fractile(_pool(refs, devices, task_tag), 0.5),
        "average": lambda refs: _average(_pool(refs, devices, task_tag)),
        "devices": lambda pattern: _selector_refs(pattern, flat),
    }
    for name, ref in (refs or {}).items():
        namespace[name] = _ref(ref)
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
        # `tasks` is the live TaskRegistry, which already carries the build
        # context (flat/extensions/sticky_endpoints/task_specs) these two
        # need.
        "create_task": lambda spec: tasks.create(spec),
        "kill_task": lambda *tags: tasks.kill(tags),
        "reset_sticky": _reset_sticky,
        "log": lambda msg: logger.info(msg),
    })
    return namespace


@register_task_kind("script")
class ScriptAction(Action):
    """Runs a restricted-Python script against the shared rule namespace.

    (_build_rule_namespace) when this task fires -- the escape hatch
    for multi-step scenarios spanning several reads/writes/spawns in
    one body.

    `compiled`/`refs` are public (not `_compiled`/`_refs`) so
    phc.core.config._build_action can inspect them after construction, to
    validate/subscribe every endpoint the script references."""

    def __init__(self, *, code: str, task_tag: str, flat: dict[str, Device],
                 tasks: "TaskRegistry", extensions: dict[str, object] | None = None,
                 sticky_endpoints: set | None = None, refs: dict[str, str] | None = None,
                 task_specs: dict[str, dict] | None = None, **params):
        # task_specs is accepted for signature compatibility only; the
        # TaskRegistry in `tasks` already carries the templates.
        super().__init__(task_tag=task_tag, flat=flat, tasks=tasks, extensions=extensions,
                         sticky_endpoints=sticky_endpoints, **params)
        self.compiled = scripting.compile_script(code)
        self.refs = refs or {}

    def perform(self, devices: dict[str, Device]) -> None:
        namespace = _build_rule_namespace(devices=devices, flat=self.flat, tasks=self.tasks,
                                           task_tag=self.task_tag, writable=True,
                                           refs=self.refs)
        scripting.run_script(self.compiled, namespace)


class Task:
    """A scheduled and/or condition-gated unit of work.

    Self-manages its own due_time/`finished` state from a single run()
    call each tick. See docs/configuration.md#tasks for the
    gating/repeat/min_interval semantics.

    `finished` (read-only) marks a task the Scheduler should drop."""

    def __init__(self, tag: str, *, description: str = "", due_time: float | None = None,
                 repeat: float | None = None, min_interval: float = 0.0,
                 condition: Condition | ExprCondition | None = None,
                 actions: list[Action]):
        self.tag = tag
        self.description = description
        self.due_time = due_time
        self.repeat = repeat
        self.min_interval = min_interval
        self.condition = condition
        self.actions = actions
        self._finished = False
        self._last_fired = float("-inf")

    def run(self, now: Now | float, devices: dict[str, Device]) -> bool:
        """Gate on due_time, condition, then min_interval, in that order.

        So a false condition or an active cooldown returns before
        `_last_fired` is touched. Runs all actions and rearms/finishes
        (see docs/configuration.md#tasks) if every gate passes. Returns
        True iff the actions ran.

        `now.wall` gates due_time; `now.mono` measures the min_interval
        cooldown, so a wall-clock step cannot freeze or skip it -- see
        phc.core.clock.Now."""
        now = Now.coerce(now)
        if self.due_time is not None and now.wall < self.due_time:
            return False
        if self.condition is not None and not self.condition.evaluate(devices):
            return False
        if self.min_interval and (now.mono - self._last_fired) < self.min_interval:
            return False
        for action in self.actions:
            action.perform(devices)
        self._last_fired = now.mono
        if self.due_time is not None:
            # See class docstring: a bare condition task (due_time still
            # None) skips this branch entirely and just stays resident.
            if self.repeat is None:
                self._finished = True
            elif self.repeat > 0:
                self.due_time += self.repeat * math.ceil((now.wall - self.due_time + 1e-9) / self.repeat)
        return True

    last_fired = property(lambda self: self._last_fired)
    finished = property(lambda self: self._finished)
