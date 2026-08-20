"""Task system: conditions that gate actions, and the actions (set, toggle,
log, create_task, kill_task, script) a Task can perform against devices."""

import fnmatch
import logging
import math

from phc.core import scripting
from phc.core.device import Device
from phc.core.registry import register_task_kind

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
    """Gates on a device endpoint's change event (changed) and/or current
    state (value), independently ANDed together. None = no constraint.

    changed=True fires the tick event happens; changed=False fires only
    when no event occurs. value=X checks current state (level: holds every
    tick at X; or edge: holds only the transition tick if changed=True).

    Scheduler timing: get_event() is freshly computed each tick, so timing
    gating observes the PREVIOUS tick's state changes."""

    def __init__(self, *, device_id: str, endpoint_key: str,
                 changed: bool | None = None, value=None):
        self.device_id = device_id
        self.endpoint_key = endpoint_key
        self.changed = changed
        self.value = value

    def evaluate(self, devices: dict[str, Device]) -> bool:
        """True if both filters hold (each trivially holding if unset --
        see class docstring): `changed`, against get_event(); `value`,
        against the endpoint's current get()."""
        device = devices[self.device_id]
        if self.changed is None:
            cond_changed = True
        else:
            event = device.get_event(self.endpoint_key)
            cond_changed = (event is not None) if self.changed else (event is None)
        cond_value = self.value is None or device.get(self.endpoint_key) == self.value
        return cond_changed and cond_value


class ExprCondition:
    """Gates a Task on a restricted-Python boolean expression (see
    phc.core.scripting) instead of Condition's single-endpoint `changed` flag --
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
                                           task_tag=self._task_tag, writable=False)
        for name, ref in self._refs.items():
            device_id, endpoint_key = resolve_endpoint_ref(ref)
            namespace[name] = scripting.EndpointRef(device_id, endpoint_key, devices, self._task_tag)
        return bool(scripting.evaluate_expression(self._compiled, namespace))


class Action:
    """Base class for a task's effect. One concrete subclass per `kind`,
    registered via @register_task_kind, analogous to Endpoint subclasses
    registered via @register_endpoint_kind.

    device_id/endpoint_key are the single device/endpoint this action
    targets, resolved by phc.core.config._build_action from the YAML `device:`
    key -- None if that key was omitted, whether because this kind never
    targets a device (CreateTaskAction/KillTaskAction/ScriptAction and every
    extension action, which act on the task list or a named extension
    instance instead) or because a device-oriented kind's device was left
    out (e.g. LogAction's device-less, unformatted message form). A kind
    that always needs a real device (e.g. SetAction/ToggleAction) simply
    isn't built to tolerate None here -- omitting `device:` for one of
    those surfaces as a runtime error from perform(), not a config-load
    one, since _build_action no longer knows ahead of time which kinds
    require it.

    flat/tasks/extensions/task_tag/sticky_endpoints are the live config-
    build context, passed to every action unconditionally regardless of
    kind -- CreateTaskAction/KillTaskAction/ScriptAction and the extension
    actions use them; the ordinary device-oriented actions ignore them.
    `tasks` is the live TaskRegistry, which also carries that same context
    (see TaskRegistry); the individual kwargs are kept alongside it because
    they are the published constructor API every extension's own action
    kind already accepts."""

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
    """Set the target endpoint/device to `value`, a raw value (e.g. 0, 1,
    22.5) or display text (e.g. "on", "off") -- translated into the
    endpoint's raw value via Endpoint.from_text() (see phc/core/endpoint.py),
    so a `values`-mapped endpoint accepts either its raw keys or their text
    labels. An endpoint with no declared type/values passes `value` through
    unchanged, same as writing it directly via device.set().

    `expr` is the dynamic alternative to a literal `value` -- a restricted-
    Python expression (see phc.core.scripting), re-evaluated fresh every time
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
                                               task_tag=self.task_tag, writable=False)
            for name, ref in self.refs.items():
                device_id, endpoint_key = resolve_endpoint_ref(ref)
                namespace[name] = scripting.EndpointRef(device_id, endpoint_key, devices, self.task_tag)
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
        """If device_id is None (no `device:` given), log `message` as-is --
        no state/text to format in, so a template placeholder would raise.

        If endpoint_key is None (device given without an endpoint, e.g.
        `device: "meteo-bern"`), device.get(None)/get_text(None) already
        report every endpoint (and child) as a dict -- so `state`/`text`
        print the whole device's state rather than a single value.

        `state` is the raw value (as before); `text` is the endpoint's
        formatted display text (see Endpoint.to_text), e.g. "on" for a
        `values`-mapped endpoint whose raw state is 1."""
        message = self.params.get("message", "")
        if self.device_id is not None:
            device = devices[self.device_id]
            state = device.get(self.endpoint_key)
            text = device.get_text(self.endpoint_key)
            message = message.format(state=state, text=text)
        logger.info(message)


class TaskRegistry:
    """The live set of Tasks, plus the context needed to build more at
    runtime.

    Replaces the bare `list` that used to be passed around and mutated in
    place by the Scheduler, by every Action, and by extensions.timer -- three
    layers sharing one unencapsulated object, each doing its own
    append/remove and its own scan-for-tag.

    It also breaks the last import cycle in `phc.core`. Creating a Task from
    a spec dict is the config loader's job, but `create_task`/`kill_task`
    need it at *runtime*, from inside this module -- which used to mean
    task.py reaching into `phc.core.config._build_task` through a
    function-local import of a private name. The builder is now injected
    here instead (see `build_task`), so the dependency runs one way:
    config imports task, never the reverse.

    Deliberately iterable and equipped with `remove()`, because that is the
    entire interface `phc.core.scheduler` needs -- a plain list satisfies it
    too, so the Scheduler works unchanged with either, and every test that
    hands it a literal list of Tasks keeps working.
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

    # ---------- container protocol (what the Scheduler and callers use) ----------

    def __iter__(self):
        return iter(self._tasks)

    def __len__(self) -> int:
        return len(self._tasks)

    def __getitem__(self, index):
        return self._tasks[index]

    def __contains__(self, task) -> bool:
        return task in self._tasks

    def remove(self, task: "Task") -> None:
        """Drop `task` from the live set (the Scheduler's one-shot retirement
        path). Raises ValueError if it isn't present, same as list.remove."""
        self._tasks.remove(task)

    def add(self, task: "Task") -> None:
        """Append an already-built Task."""
        self._tasks.append(task)

    def by_tag(self, tag: str) -> "Task | None":
        """The task with this exact tag, or None. Tags are unique within a
        registry -- create() replaces rather than duplicates."""
        return next((t for t in self._tasks if t.tag == tag), None)

    # ---------- runtime creation / removal ----------

    def create(self, specs: dict) -> "Task":
        """Build `specs` (same shape as a top-level tasks[] entry) into a
        Task and (re-)register it, replacing any existing task with the same
        tag rather than duplicating it -- so a repeatedly-firing trigger
        re-arms the same spawned task instead of accumulating one per
        firing. Backs CreateTaskAction and the script sandbox's
        create_task() (see _build_rule_namespace).

        `specs` may give `template: <name>` instead of a full literal task
        definition, resolved by name against this registry's `task_specs`
        (the top-level `task_specs:` section) -- see CreateTaskAction."""
        if self._build_task is None:
            raise ValueError(
                "this TaskRegistry cannot build tasks: no build_task was supplied "
                "(a registry built by phc.core.config.load_system always has one)")
        if "template" in specs:
            task_specs = self.task_specs or {}
            name = specs["template"]
            try:
                specs = task_specs[name]
            except KeyError:
                raise ValueError(f"create_task: unknown template {name!r}; "
                                  f"available: {sorted(task_specs)}") from None

        new_task = self._build_task(specs, self)
        existing = self.by_tag(new_task.tag)
        if existing is not None:
            self._tasks.remove(existing)
            logger.info("%s: replacing existing task", new_task.tag)
        self._tasks.append(new_task)
        logger.info("%s created", new_task.tag)
        return new_task

    def kill(self, patterns) -> int:
        """Remove every task whose tag matches any of `patterns` (fnmatch
        glob, same convention as phc.core.selectors -- an exact tag is also a
        valid glob), logging each removal. Returns the number removed. Backs
        KillTaskAction and the script sandbox's kill_task()."""
        to_remove = [t for t in self._tasks
                     if any(fnmatch.fnmatchcase(t.tag, p) for p in patterns)]
        for t in to_remove:
            self._tasks.remove(t)
            logger.info("%s killed", t.tag)
        return len(to_remove)


@register_task_kind("create_task")
class CreateTaskAction(Action):
    """Builds a new Task from a nested `specs:` task definition (same shape
    as a top-level tasks[] entry), or from a named `template:` looked up in
    the top-level `task_specs:` section, and appends it to the live task
    list -- so a firing task can spawn a follow-up task at runtime (e.g.
    "turn this off again in 1s") instead of it having to be pre-declared as
    an independent, permanently-resident task in YAML. Exactly one of
    `specs`/`template` is required. Either way, the spec is parsed and
    validated lazily, the first time this fires -- a broken spec raises
    then, caught like any other action failure by the Scheduler's per-task
    exception handler, not at config-load time.

    If a task with the same tag already exists, it is replaced rather than
    duplicated -- so a repeatedly-firing trigger (e.g. a condition that can
    hold many times over the process's lifetime) re-arms the same spawned
    task instead of accumulating one per firing."""

    def __init__(self, *, specs: dict | None = None, template: str | None = None,
                 tasks: "TaskRegistry", **params):
        super().__init__(tasks=tasks, **params)
        if (specs is None) == (template is None):
            raise ValueError("create_task: specify exactly one of 'specs' or 'template'")
        self._specs = specs if specs is not None else {"template": template}
        self._tasks = tasks

    def perform(self, devices: dict[str, Device]) -> None:
        self._tasks.create(self._specs)


@register_task_kind("kill_task")
class KillTaskAction(Action):
    """Removes every task whose tag matches any of `tags` (fnmatch glob) from
    the live task list -- the declarative counterpart to create_task, and
    the structural equivalent of the previous Tcl system's `KillJob`."""

    def __init__(self, *, tags: list[str], tasks: "TaskRegistry", **params):
        super().__init__(tasks=tasks, **params)
        self._tags = tags
        self._tasks = tasks

    def perform(self, devices: dict[str, Device]) -> None:
        self._tasks.kill(self._tags)


def _selector_refs(pattern: str, flat: dict[str, Device]) -> list[str]:
    """The `devices(pattern)` namespace function: expand a selector pattern
    (see phc.core.selectors) into "<qualified_id>.<endpoint_key>" ref strings,
    usable as a `for` target or passed straight to state()/changed()/etc."""
    from phc.core.selectors import resolve_selectors  # local import: avoid cycle (see phc/core/scripting.py)

    return [f"{device_id}.{endpoint_key}" for device_id, endpoint_key in resolve_selectors([pattern], flat)]


def _pool(refs, devices: dict[str, Device], task_tag: str) -> list:
    """Concatenate the recorded history of one ref (a "device.endpoint"
    string) or a list of refs into a single pooled list -- backs
    history()/fractile()/median()/average(). Pooling multiple refs is what
    lets a script combine several sensors' buffers into one sorted set,
    e.g. two temperature sensors' 4-sample histories pooled into one
    8-value fractile (see docs/scripting.md). Raises ValueError (via
    EndpointRef.history) naming the first ref that never declared
    `history:`."""
    ref_list = [refs] if isinstance(refs, str) else refs
    pool = []
    for ref in ref_list:
        device_id, endpoint_key = resolve_endpoint_ref(ref)
        pool.extend(scripting.EndpointRef(device_id, endpoint_key, devices, task_tag).history)
    return pool


def _fractile(pool: list, f: float):
    """The value at relative position `f` (0.0-1.0) in `pool` once sorted --
    f=0 is the smallest recorded sample, f=0.5 the middle one, f=1 the
    largest. Ported from THC's VHistory_Get (`round(($n-1)*$f)` under
    Tcl's round-half-away-from-zero); `int(x + 0.5)` reproduces that
    exactly for f in [0, 1], unlike Python's round() (banker's rounding),
    which only agrees with Tcl at some fractions/pool sizes -- e.g. they
    diverge at f=0.5 on an even-length pool. Always returns an actually
    recorded sample, never an interpolated value. None on an empty pool --
    the same "nothing observed yet" convention as state()/sticky(). Raises
    ValueError if f is outside [0, 1] -- a negative f would otherwise index
    from the end of the sorted list, silently returning a wrong value
    instead of failing loudly."""
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
                           writable: bool) -> dict:
    """The one place that defines what a condition's `expr:` or a script
    action's `code:` can call -- both ExprCondition.evaluate() and
    ScriptAction.perform() build their namespace here, the former with
    `writable=False`, so the two surfaces can never drift apart in what they
    expose and a new function only needs to be added once. See
    docs/scripting.md for the full expr:/code: API reference.

    Always available (read-only): state/changed/text/event/sticky/history/
    available/age (each takes a "device.endpoint" ref string),
    fractile/median/average
    (each takes one ref OR a list of refs, pooling their recorded history --
    see _pool), and devices(pattern) (a selector, see phc.core.selectors). Only
    when `writable` (scripted actions): set_state, create_task/kill_task,
    reset_sticky, and log. `sticky`/`reset_sticky` are keyed by `task_tag` as
    the Endpoint.subscribe_log() subscriber id (see phc.core.endpoint) --
    phc.core.config subscribes every referenced endpoint under that same id at
    config-build time."""

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
        # used to be handed separately.
        "create_task": lambda spec: tasks.create(spec),
        "kill_task": lambda *tags: tasks.kill(tags),
        "reset_sticky": _reset_sticky,
        "log": lambda msg: logger.info(msg),
    })
    return namespace


@register_task_kind("script")
class ScriptAction(Action):
    """Runs a restricted-Python script (see phc.core.scripting) against the
    shared rule namespace (_build_rule_namespace, above) when this task
    fires -- the escape hatch for multi-step scenarios: reading/writing
    device state, spawning/killing tagged sub-tasks, and reading/resetting
    sticky min/max values from one script body, mirroring what the previous
    Tcl system's job script bodies could do.

    `compiled`/`refs` are public (not `_compiled`/`_refs`) so
    phc.core.config._build_action can inspect `compiled.referenced_paths` and
    `refs` after construction, to validate/subscribe every endpoint this
    script references -- the same way _build_condition does for
    ExprCondition (see phc.core.scripting's referenced_paths)."""

    def __init__(self, *, code: str, task_tag: str, flat: dict[str, Device],
                 tasks: list["Task"], extensions: dict[str, object] | None = None,
                 sticky_endpoints: set | None = None, refs: dict[str, str] | None = None,
                 task_specs: dict[str, dict] | None = None, **params):
        super().__init__(**params)
        self.compiled = scripting.compile_script(code)
        self.refs = refs or {}
        self._task_tag = task_tag
        self._flat = flat
        self._tasks = tasks
        self._extensions = extensions if extensions is not None else {}
        self._sticky_endpoints = sticky_endpoints if sticky_endpoints is not None else set()
        self._task_specs = task_specs

    def perform(self, devices: dict[str, Device]) -> None:
        namespace = _build_rule_namespace(devices=devices, flat=self._flat, tasks=self._tasks,
                                           task_tag=self._task_tag, writable=True)
        for name, ref in self.refs.items():
            device_id, endpoint_key = resolve_endpoint_ref(ref)
            namespace[name] = scripting.EndpointRef(device_id, endpoint_key, devices, self._task_tag)
        scripting.run_script(self.compiled, namespace)


class Task:
    """A scheduled and/or condition-gated unit of work, self-managing its
    own due_time/`finished` state from a single run() call each tick. See
    docs/configuration.md#tasks for the gating/repeat/min_interval
    semantics and the due_time defaulting matrix.

    `finished` is read-only: Indicates that the task is executed and not
    required anymore."""

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

    def run(self, now: float, devices: dict[str, Device],
            now_mono: float | None = None) -> bool:
        """Gate on due_time, condition, then min_interval (each checked once
        per call, not per action) -- a fixed short-circuit chain, so a false
        condition or a cooldown still in effect returns before
        min_interval's own bookkeeping (`_last_fired`) is touched. Runs all
        actions and rearms/finishes (see docs/configuration.md#tasks) if
        every gate passes. Returns True iff the actions ran.

        Two clocks: `now` is wall-clock, since due_time is an absolute time
        of day (see phc.core.intervals.parse_time); `now_mono` is monotonic and
        measures only the min_interval cooldown, so a wall-clock step
        backwards can't freeze a cooldown for hours (nor a step forwards
        end one early). `now_mono` defaults to `now`, keeping a
        single-clock caller working unchanged -- see
        phc.core.scheduler.Scheduler's class docstring."""
        if now_mono is None:
            now_mono = now
        if self.due_time is not None and now < self.due_time:
            return False
        if self.condition is not None and not self.condition.evaluate(devices):
            return False
        if self.min_interval and (now_mono - self._last_fired) < self.min_interval:
            return False
        for action in self.actions:
            action.perform(devices)
        self._last_fired = now_mono
        if self.due_time is not None:
            # See class docstring: a bare condition task (due_time still
            # None) skips this branch entirely and just stays resident.
            if self.repeat is None:
                self._finished = True
            elif self.repeat > 0:
                self.due_time += self.repeat * math.ceil((now - self.due_time + 1e-9) / self.repeat)
        return True

    last_fired = property(lambda self: self._last_fired)
    finished = property(lambda self: self._finished)
