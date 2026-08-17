"""Building tasks: a `tasks:` entry into a Task, its `condition:` into a
Condition/ExprCondition, and each `action:` into a registered Action kind.

`_build_task` is what a TaskRegistry calls to create tasks at runtime too
(see phc.core.task.TaskRegistry) -- it is injected there rather than
imported, which is what keeps phc.core.task free of any dependency on this
package.
"""

from phc.core import scripting
from phc.core.device import Device
from phc.core.errors import ConfigError
from phc.core.intervals import parse_duration, parse_time
from phc.core.registry import get_task_kind_class
from phc.core.task import Condition, ExprCondition, Task, TaskRegistry, resolve_endpoint_ref


def _subscribe_referenced_endpoints(paths: set[str], flat: dict[str, Device], task_tag: str,
                                     sticky_endpoints: set, context: str) -> None:
    """Validate and sticky-subscribe every "device.endpoint" path an expr or
    script references -- shared by `condition: {expr}`, a `script` action's
    `code`, and a `set` action's `expr`, so the three surfaces can't drift
    in how they validate/subscribe referenced endpoints. `context` (e.g.
    "condition"/"action") only changes the ConfigError wording below."""
    for ref in paths:
        device_id, endpoint_key = resolve_endpoint_ref(ref)
        if device_id not in flat:
            raise ConfigError(f"task {task_tag!r}: {context} device {device_id!r} not found")
        endpoint = flat[device_id].endpoint(endpoint_key)
        endpoint.subscribe_log(task_tag)
        sticky_endpoints.add(endpoint)


def _build_condition(spec: dict | None, task_tag: str,
                      registry: TaskRegistry) -> Condition | ExprCondition | None:
    """Build a task's `condition:` YAML entry into a Condition or
    ExprCondition, or None if absent. Requires exactly one of `device` (the
    {device, changed, value} shorthand -- see Condition's docstring for how
    `changed`/`value` combine) or `expr` (a restricted-Python boolean
    expression, see phc.core.scripting). Raises ConfigError if a referenced
    device doesn't exist or `expr` violates the sandbox whitelist.

    For `expr`, every endpoint it references -- via `refs:` or inline as a
    string literal (`state("house.motion1.state")`, see
    scripting.Compiled.referenced_paths) -- is subscribed for sticky log
    tracking under `task_tag` and added to `sticky_endpoints`, so
    load_system()'s tick hook keeps its sticky() window advancing every
    tick regardless of when/whether this condition is evaluated."""
    if spec is None:
        return None
    flat, sticky_endpoints = registry.flat, registry.sticky_endpoints
    has_device = "device" in spec
    has_expr = "expr" in spec
    if has_device == has_expr:
        raise ConfigError(f"task {task_tag!r}: condition requires exactly one of 'device' or 'expr'")

    if has_expr:
        try:
            compiled = scripting.compile_expression(spec["expr"])
        except scripting.ScriptError as exc:
            raise ConfigError(f"task {task_tag!r}: {exc}") from None
        refs = spec.get("refs", {})
        _subscribe_referenced_endpoints(set(refs.values()) | compiled.referenced_paths,
                                         flat, task_tag, sticky_endpoints, context="condition")
        return ExprCondition(compiled=compiled, refs=refs, task_tag=task_tag, flat=flat)

    device_id, endpoint_key = resolve_endpoint_ref(spec["device"])
    if device_id not in flat:
        raise ConfigError(f"task {task_tag!r}: condition device {device_id!r} not found")
    return Condition(device_id=device_id, endpoint_key=endpoint_key,
                      changed=spec.get("changed"), value=spec.get("value"))


def _build_action(spec: dict, task_tag: str, registry: TaskRegistry):
    """Build one `action:`/`actions[]` YAML entry into an Action instance,
    dispatching on `kind` (via the task-kind registry). Every kind is built
    the same way: device_id/endpoint_key are resolved from `device:` when
    given (allow_bare=True -- an action's device may omit the endpoint,
    e.g. "meteo-bern" rather than "meteo-bern.temperature", the Action
    subclass then falling back to whole-device get()/set() semantics;
    Conditions, above, intentionally do NOT allow this), None otherwise --
    and flat/tasks/extensions/task_tag/sticky_endpoints/task_specs are
    passed to every kind unconditionally, so a kind with no single device
    target (create_task, kill_task, script, and every extension action) can
    act on the task list itself, look up a named extension instance (see
    phc.core.config._load_extensions) or a named `task_specs:` template (see
    phc.core.task.CreateTaskAction), or (kind: script, or kind: set with an
    `expr:`) run against the shared rule namespace (see
    phc.core.task._build_rule_namespace), while an ordinary device-oriented
    kind simply never touches them. Raises ConfigError on a
    missing/unregistered kind, a given device that doesn't exist, a spec
    missing one of that kind's own required constructor arguments (e.g.
    create_task's `specs`/`template`), or -- for kind: script, or kind: set
    with an `expr:` -- a code/expr block that violates the sandbox
    whitelist. A device-oriented kind (e.g. set, toggle) whose `device:` is
    missing builds without error here -- it fails at that action's own
    perform() instead, the first time it fires."""
    flat, sticky_endpoints = registry.flat, registry.sticky_endpoints
    kind = spec.get("kind")
    if kind is None:
        raise ConfigError(f"task {task_tag!r}: action requires a 'kind'")
    try:
        action_cls = get_task_kind_class(kind)
    except KeyError as exc:
        raise ConfigError(f"task {task_tag!r}: {exc}") from None

    extra = {k: v for k, v in spec.items() if k not in ("kind", "device")}

    device_id = endpoint_key = None
    if "device" in spec:
        device_id, endpoint_key = resolve_endpoint_ref(spec["device"], allow_bare=True)
        if device_id not in flat:
            raise ConfigError(f"task {task_tag!r}: action device {device_id!r} not found")

    try:
        # The individual context kwargs are kept (rather than passing the
        # registry alone) because they are the published Action constructor
        # API that every extension's own action kind already accepts --
        # see e.g. phc.extensions.logdb's LogDbAction(extensions=...).
        action = action_cls(device_id=device_id, endpoint_key=endpoint_key, flat=flat,
                             tasks=registry, extensions=registry.extensions, task_tag=task_tag,
                             sticky_endpoints=sticky_endpoints,
                             task_specs=registry.task_specs, **extra)
    except (TypeError, ValueError, scripting.ScriptError) as exc:
        raise ConfigError(f"task {task_tag!r}: invalid {kind!r} action: {exc}") from None

    # script always compiles code; set only compiles when it's given expr:
    # instead of a literal value: -- both need the same referenced-endpoint
    # validation/sticky-subscription (see _build_condition's expr handling,
    # above) before the config finishes loading.
    if kind == "script" or (kind == "set" and action.compiled is not None):
        _subscribe_referenced_endpoints(
            set(action.refs.values()) | action.compiled.referenced_paths,
            flat, task_tag, sticky_endpoints, context="action")
    return action


def _build_task(entry: dict, registry: TaskRegistry) -> Task:
    """Build one `tasks:` YAML entry (or a `create_task`/script action's
    nested spec, or a `task_specs:` entry once instantiated by a
    `template:` reference) into a Task. See docs/configuration.md#tasks
    for the `condition`/`time`/`repeat`/`min_interval` semantics and the
    due_time defaulting matrix."""
    tag = entry["tag"]
    repeat_spec = entry.get("repeat")
    repeat_seconds = parse_duration(repeat_spec) if repeat_spec is not None else None
    min_interval_spec = entry.get("min_interval", 0)
    min_interval = parse_duration(min_interval_spec) if min_interval_spec else 0.0

    condition = _build_condition(entry.get("condition"), tag, registry)

    time_spec = entry.get("time")
    if time_spec is None:
        if repeat_seconds is None and condition is not None:
            due_time = None
        else:
            due_time = parse_time("+0s", repeat=repeat_seconds if repeat_seconds else None)
    else:
        due_time = parse_time(str(time_spec), repeat=repeat_seconds if repeat_seconds else None)

    has_action = "action" in entry
    has_actions = "actions" in entry
    if has_action == has_actions:
        raise ConfigError(f"task {tag!r}: specify exactly one of 'action' or 'actions'")

    if has_actions:
        action_specs = entry["actions"]
        if not isinstance(action_specs, list) or not action_specs:
            raise ConfigError(f"task {tag!r}: 'actions' must be a non-empty list")
        actions = [_build_action(spec, tag, registry) for spec in action_specs]
    else:
        actions = [_build_action(entry["action"], tag, registry)]

    return Task(
        tag,
        description=entry.get("description", ""),
        due_time=due_time,
        repeat=repeat_seconds,
        min_interval=min_interval,
        condition=condition,
        actions=actions,
    )
