"""System YAML loading: parses a system config file into a System (devices,
tasks, and scheduler settings), merging each device's params/endpoints
against its module's declared module.yaml along the way."""

import importlib
from pathlib import Path

import yaml

from core.device import Device
from core.endpoint import LOG_AGGREGATIONS, VALUE_TYPES, Endpoint
from core.intervals import parse_duration, parse_time
from core.logging_setup import configure_logging
from core.registry import (discover_extensions, discover_modules, get_device_class,
                            get_endpoint_class, get_task_kind_class)
from core.task import Task, Condition, resolve_endpoint_ref

_DEVICES_DIR = Path(__file__).resolve().parent.parent / "devices"
_EXTENSIONS_DIR = Path(__file__).resolve().parent.parent / "extensions"


class ConfigError(Exception):
    """Raised for any invalid or inconsistent system YAML."""


class ModuleDescriptor:
    """Parsed module.yaml for one device module."""

    def __init__(self, name: str, raw: dict):
        self.name = name
        self.description = raw.get("description", "")
        self.parameters = raw.get("parameters") or []
        self.endpoints = raw.get("endpoints") or []
        self.update = raw.get("update", None)


_module_descriptors: dict[str, ModuleDescriptor] = {}


def _load_module_descriptor(module_name: str) -> ModuleDescriptor:
    """Return the cached ModuleDescriptor for `module_name`, loading and
    parsing its module.yaml on first use."""
    if module_name in _module_descriptors:
        return _module_descriptors[module_name]
    module_yaml_path = _DEVICES_DIR / module_name / "module.yaml"
    if not module_yaml_path.exists():
        raise ConfigError(f"module {module_name!r} has no module.yaml at {module_yaml_path}")
    with open(module_yaml_path, "r") as f:
        raw = yaml.safe_load(f) or {}
    descriptor = ModuleDescriptor(module_name, raw)
    _module_descriptors[module_name] = descriptor
    return descriptor


def _resolve_module_params(module: ModuleDescriptor, modules_config: dict) -> dict:
    """Resolve `scope: module` parameter values for one module type, from the
    top-level `modules.<name>.params` config. Computed once per module name
    per load_system() call (see _build_device's resolved_module_params_cache)
    -- unlike _module_descriptors, this cannot be a cross-call global cache
    since modules_config is specific to the one system YAML being loaded."""
    module_params = dict((modules_config.get(module.name) or {}).get("params") or {})
    declared = {p["name"]: p for p in module.parameters if p.get("scope", "device") == "module"}

    resolved = {}
    for name, spec in declared.items():
        override = spec.get("override", "allowed")
        if name in module_params:
            if override == "none":
                raise ConfigError(
                    f"module {module.name!r}: parameter {name!r} is not overridable "
                    f"(override: none) but modules.{module.name}.params sets it")
            resolved[name] = module_params.pop(name)
        elif override == "required":
            raise ConfigError(
                f"module {module.name!r}: parameter {name!r} is required but not supplied "
                f"in modules.{module.name}.params")
        else:
            resolved[name] = spec.get("default")

    if module_params:
        unknown = ", ".join(repr(k) for k in module_params)
        raise ConfigError(f"module {module.name!r}: unrecognized parameter(s) {unknown}")

    return resolved


class ExtensionDescriptor:
    """Parsed extension.yaml for one extension package. Structurally like
    ModuleDescriptor, but every extension instance (e.g. one named logdb
    entry) is merged independently against the same descriptor -- there is
    no module/device scope split since extensions aren't devices."""

    def __init__(self, name: str, raw: dict):
        self.name = name
        self.description = raw.get("description", "")
        self.parameters = raw.get("parameters") or []


_extension_descriptors: dict[str, ExtensionDescriptor] = {}


def _load_extension_descriptor(name: str) -> ExtensionDescriptor:
    """Return the cached ExtensionDescriptor for `name`, loading and parsing
    its extension.yaml on first use."""
    if name in _extension_descriptors:
        return _extension_descriptors[name]
    extension_yaml_path = _EXTENSIONS_DIR / name / "extension.yaml"
    if not extension_yaml_path.exists():
        raise ConfigError(f"extension {name!r} has no extension.yaml at {extension_yaml_path}")
    with open(extension_yaml_path, "r") as f:
        raw = yaml.safe_load(f) or {}
    descriptor = ExtensionDescriptor(name, raw)
    _extension_descriptors[name] = descriptor
    return descriptor


def _merge_extension_params(descriptor: ExtensionDescriptor, instance_config: dict,
                             instance_label: str) -> dict:
    """Merge one extension instance's params against its extension.yaml's
    declared parameters, honoring each parameter's `override` rule and
    `default` -- same validation shape as _merge_params, but there is no
    module/device scope split to handle. Raises ConfigError on an
    unrecognized, missing-but-required, or not-overridable parameter,
    naming `instance_label` (e.g. "logdb.house_log") in the message."""
    instance_config = dict(instance_config or {})
    declared = {p["name"]: p for p in descriptor.parameters}

    merged = {}
    for name, spec in declared.items():
        override = spec.get("override", "allowed")
        if name in instance_config:
            if override == "none":
                raise ConfigError(
                    f"extension instance {instance_label!r}: parameter {name!r} is not "
                    f"overridable (override: none) but instance config sets it")
            merged[name] = instance_config.pop(name)
        elif override == "required":
            raise ConfigError(
                f"extension instance {instance_label!r}: parameter {name!r} is required "
                f"but not supplied")
        else:
            merged[name] = spec.get("default")

    if instance_config:
        unknown = ", ".join(repr(k) for k in instance_config)
        raise ConfigError(f"extension instance {instance_label!r}: unrecognized parameter(s) {unknown}")

    return merged


def _load_extensions(raw: dict, flat: dict[str, Device]) -> dict[str, object]:
    """Build every extensions.<name>.<instance>: entry in the system YAML.
    raw['extensions'] is {extension_name: {instance_name: params, ...}, ...}
    -- an extension may have zero, one, or several named instances (e.g.
    multiple independently-configured/scheduled logdb files). Each
    instance's params are merged against extensions/<name>/extension.yaml,
    then handed to that package's configure(params, flat, instance_key)
    entry point (instance_key = "<extension_name>.<instance_name>", used
    both as the registry key below and as the sticky-log subscriber id --
    see core.endpoint.Endpoint.subscribe_log). The returned object is
    registered under instance_key so a hand-authored task action
    (kind: log_db, instance: "logdb.house_log") can look it up, and -- if
    it exposes an on_tick(devices) method -- is auto-collected as a
    Scheduler tick hook (see load_system()). Absence of extensions: (or of
    a given extension's key) means nothing of that kind is active --
    mirrors devices:/tasks: only building what's explicitly declared."""
    registry: dict[str, object] = {}
    for ext_name, instances in (raw.get("extensions") or {}).items():
        if not isinstance(instances, dict):
            raise ConfigError(f"extensions.{ext_name}: expected a mapping of instance name -> params")
        descriptor = _load_extension_descriptor(ext_name)
        module = importlib.import_module(f"extensions.{ext_name}.extension")
        configure = getattr(module, "configure", None)
        if configure is None:
            raise ConfigError(f"extension {ext_name!r} has no configure() entry point")
        for instance_name, instance_config in instances.items():
            key = f"{ext_name}.{instance_name}"
            params = _merge_extension_params(descriptor, instance_config or {}, key)
            registry[key] = configure(params, flat, key)
    return registry


def _merge_params(module: ModuleDescriptor, instance_params: dict, device_id: str,
                   resolved_module_params: dict | None = None) -> dict:
    """Merge one device instance's `params` against its module's declared
    parameters: device-scoped params come from `instance_params` (honoring
    each parameter's `override` rule and `default`), module-scoped params
    come from the already-resolved `resolved_module_params`. Raises
    ConfigError on an unrecognized, missing-but-required, or
    not-overridable parameter."""
    instance_params = dict(instance_params or {})
    resolved_module_params = resolved_module_params or {}
    declared = {p["name"]: p for p in module.parameters}

    merged = {}
    for name, spec in declared.items():
        if spec.get("scope", "device") == "module":
            if name in instance_params:
                raise ConfigError(
                    f"device {device_id!r}: parameter {name!r} is module-scoped "
                    f"(scope: module) and cannot be set in this device's params; "
                    f"set it under modules.{module.name}.params instead")
            merged[name] = resolved_module_params.get(name, spec.get("default"))
            continue

        override = spec.get("override", "allowed")
        if name in instance_params:
            if override == "none":
                raise ConfigError(
                    f"device {device_id!r}: parameter {name!r} is not overridable "
                    f"(override: none) but instance config sets it")
            merged[name] = instance_params.pop(name)
        elif override == "required":
            raise ConfigError(
                f"device {device_id!r}: parameter {name!r} is required but not supplied")
        else:
            merged[name] = spec.get("default")

    if instance_params:
        unknown = ", ".join(repr(k) for k in instance_params)
        raise ConfigError(f"device {device_id!r}: unrecognized parameter(s) {unknown}")

    return merged


def _merge_endpoints(module: ModuleDescriptor, instance_endpoints: list, device_id: str) -> list[Endpoint]:
    """Build this device instance's Endpoint objects: start from the module's
    declared endpoints, overlay any instance-level overrides (by `key`) and
    append instance-only endpoints not declared by the module. Returns
    (endpoints, seeds), where `seeds` are (Endpoint, default_value) pairs to
    apply once the device is constructed."""
    instance_by_key = {e["key"]: e for e in (instance_endpoints or [])}
    module_keys = [e["key"] for e in module.endpoints]

    merged_specs = []
    for spec in module.endpoints:
        key = spec["key"]
        merged = dict(spec)
        if key in instance_by_key:
            instance_spec = instance_by_key[key]
            merged.update(instance_spec)
            if "parameters" in spec or "parameters" in instance_spec:
                merged["parameters"] = {**spec.get("parameters", {}), **instance_spec.get("parameters", {})}
        merged_specs.append(merged)

    for key, spec in instance_by_key.items():
        if key not in module_keys:
            merged_specs.append(dict(spec))

    endpoints = []
    seeds = []
    for spec in merged_specs:
        value_type = spec.get("type")
        if value_type is not None and value_type not in VALUE_TYPES:
            raise ConfigError(
                f"device {device_id!r}: endpoint {spec['key']!r} has invalid type "
                f"{value_type!r}, expected one of {VALUE_TYPES}")
        log_aggregation = spec.get("log_aggregation", "max")
        if log_aggregation not in LOG_AGGREGATIONS:
            raise ConfigError(
                f"device {device_id!r}: endpoint {spec['key']!r} has invalid log_aggregation "
                f"{log_aggregation!r}, expected one of {LOG_AGGREGATIONS}")
        cls = get_endpoint_class(spec.get("kind"))
        ep = cls(
            spec["key"],
            readable=spec.get("readable", True),
            writable=spec.get("writable", False),
            parameters=spec.get("parameters"),
            description=spec.get("description", ""),
            value_type=value_type,
            unit=spec.get("unit"),
            values=spec.get("values"),
            log_aggregation=log_aggregation,
        )
        endpoints.append(ep)
        if "default" in spec:
            seeds.append((ep, spec["default"]))

    return endpoints, seeds


def _resolve_interval(module: ModuleDescriptor, instance_entry: dict,
                       intervals_map: dict) -> float | None:
    """Resolve a device's update interval: instance `update` overrides the
    module's default; a named value is looked up in `intervals_map` (the
    system YAML's top-level `intervals:`) before being parsed as a duration.
    Returns None if unset (the device is then never auto-polled)."""
    value = instance_entry.get("update", module.update)
    if value is None:
        return None
    if isinstance(value, str) and value in intervals_map:
        value = intervals_map[value]
    return parse_duration(value)


def _build_device(entry: dict, intervals_map: dict, parent_qualified_id: str | None,
                   flat: dict[str, Device], modules_config: dict,
                   resolved_module_params_cache: dict[str, dict]) -> Device:
    """Recursively build one `devices:` YAML entry (and its children) into a
    Device tree, registering every device by qualified id in `flat` as it
    goes. Raises ConfigError on a duplicate qualified id."""
    device_id = entry["id"]
    module_name = entry["module"]
    module = _load_module_descriptor(module_name)
    device_cls = get_device_class(module_name)

    if module_name not in resolved_module_params_cache:
        resolved_module_params_cache[module_name] = _resolve_module_params(module, modules_config)
    resolved_module_params = resolved_module_params_cache[module_name]

    params = _merge_params(module, entry.get("params", {}), device_id, resolved_module_params)
    endpoints, seeds = _merge_endpoints(module, entry.get("endpoints", []), device_id)
    update_interval = _resolve_interval(module, entry, intervals_map)

    qualified_id = f"{parent_qualified_id}.{device_id}" if parent_qualified_id else device_id

    children = [
        _build_device(child_entry, intervals_map, qualified_id, flat, modules_config,
                      resolved_module_params_cache)
        for child_entry in entry.get("children", [])
    ]

    device = device_cls(
        device_id,
        name=entry.get("name", ""),
        params=params,
        endpoints=endpoints,
        children=children,
        update_interval=update_interval,
        parent_qualified_id=parent_qualified_id,
    )

    for ep, default_value in seeds:
        ep.set(default_value)
        ep.update_state()

    if qualified_id in flat:
        raise ConfigError(f"duplicate device id: {qualified_id!r}")
    flat[qualified_id] = device

    return device


def _build_condition(spec: dict | None, flat: dict[str, Device], task_tag: str) -> Condition | None:
    """Build a task's `condition:` YAML entry into a Condition, or None if
    absent. Raises ConfigError if the referenced device doesn't exist."""
    if spec is None:
        return None
    device_id, endpoint_key = resolve_endpoint_ref(spec["device"])
    if device_id not in flat:
        raise ConfigError(f"task {task_tag!r}: condition device {device_id!r} not found")
    return Condition(device_id=device_id, endpoint_key=endpoint_key, changed=spec.get("changed", True))


def _build_action(spec: dict, flat: dict[str, Device], task_tag: str, tasks: list[Task],
                   extensions: dict[str, object]):
    """Build one `action:`/`actions[]` YAML entry into an Action instance,
    dispatching on `kind` (via the task-kind registry). An action kind with
    `requires_device = False` (e.g. create_task, log_db) has no single
    target device/endpoint -- it's built from `flat`/`tasks`/`extensions`
    instead, so it can act on the task list itself or look up a named
    extension instance (see core.config._load_extensions). Raises
    ConfigError on a missing/unregistered kind, an action device that
    doesn't exist, or (for a non-device kind) a spec missing one of that
    kind's own required constructor arguments (e.g. create_task's
    `specs`)."""
    kind = spec.get("kind")
    if kind is None:
        raise ConfigError(f"task {task_tag!r}: action requires a 'kind'")
    try:
        action_cls = get_task_kind_class(kind)
    except KeyError as exc:
        raise ConfigError(f"task {task_tag!r}: {exc}") from None

    extra = {k: v for k, v in spec.items() if k not in ("kind", "device")}

    if not getattr(action_cls, "requires_device", True):
        try:
            return action_cls(flat=flat, tasks=tasks, extensions=extensions, **extra)
        except TypeError as exc:
            raise ConfigError(f"task {task_tag!r}: invalid {kind!r} action: {exc}") from None

    # allow_bare=True: an action's device may omit the endpoint (e.g.
    # "meteo-bern" rather than "meteo-bern.temperature") -- the Action
    # subclass then falls back to whole-device get()/set() semantics.
    # Conditions (_build_condition, above) intentionally do NOT allow this.
    device_id, endpoint_key = resolve_endpoint_ref(spec["device"], allow_bare=True)
    if device_id not in flat:
        raise ConfigError(f"task {task_tag!r}: action device {device_id!r} not found")
    return action_cls(device_id=device_id, endpoint_key=endpoint_key, **extra)


def _build_task(entry: dict, flat: dict[str, Device], tasks: list[Task],
                 extensions: dict[str, object]) -> Task:
    """Build one `tasks:` YAML entry (or a `create_task` action's nested
    `specs:`) into a Task. Requires exactly one of `condition` or `time` to
    determine how the task fires, and exactly one of `action`/`actions`."""
    tag = entry["tag"]
    repeat_spec = entry.get("repeat", 0)
    repeat_seconds = parse_duration(repeat_spec) if repeat_spec else 0.0

    condition = _build_condition(entry.get("condition"), flat, tag)

    if condition is not None:
        due_time = float("-inf")
    else:
        time_spec = entry.get("time")
        if time_spec is None:
            raise ConfigError(f"task {tag!r}: 'time' is required unless a 'condition' is given")
        due_time = parse_time(str(time_spec), repeat=repeat_seconds or None)

    has_action = "action" in entry
    has_actions = "actions" in entry
    if has_action == has_actions:
        raise ConfigError(f"task {tag!r}: specify exactly one of 'action' or 'actions'")

    if has_actions:
        action_specs = entry["actions"]
        if not isinstance(action_specs, list) or not action_specs:
            raise ConfigError(f"task {tag!r}: 'actions' must be a non-empty list")
        actions = [_build_action(spec, flat, tag, tasks, extensions) for spec in action_specs]
    else:
        actions = [_build_action(entry["action"], flat, tag, tasks, extensions)]

    return Task(
        tag,
        description=entry.get("description", ""),
        due_time=due_time,
        repeat=repeat_seconds,
        condition=condition,
        actions=actions,
    )


class System:
    """A fully-loaded system: the device tree, its flat id index, tasks, and
    scheduler settings, as built by load_system()."""

    def __init__(self, heartbeat: float, roots: list[Device], devices: dict[str, Device],
                 tasks: list[Task] | None = None, max_workers: int | None = None,
                 fetch_timeout: float | None = None, tick_hooks: list | None = None):
        self.heartbeat = heartbeat
        self.roots = roots
        self.devices = devices
        self.tasks = tasks or []
        # Concurrency controls for the Scheduler's device I/O (see Scheduler):
        # max_workers bounds the thread pool; fetch_timeout (seconds) bounds how
        # long a tick waits on any one device before moving on. Both optional.
        self.max_workers = max_workers
        self.fetch_timeout = fetch_timeout
        # Per-tick callables auto-collected from configured extension
        # instances that expose an on_tick(devices) method (see
        # _load_extensions/load_system) -- passed straight to
        # Scheduler(tick_hooks=...).
        self.tick_hooks = tick_hooks or []

    def scheduled_devices(self) -> dict[str, Device]:
        """Return the subset of `devices` that have an update_interval (i.e.
        are auto-polled by the Scheduler)."""
        return {qid: d for qid, d in self.devices.items() if d.update_interval is not None}


def load_system(path: str | Path, log_levels_override: dict | None = None) -> System:
    """Load and build a complete System from a system YAML file at `path`.

    Configures logging and discovers device modules/extensions as a side
    effect, then builds the device tree, resolves each device's
    params/endpoints/interval against its module, builds every configured
    extension instance (see _load_extensions), and builds tasks.
    `log_levels_override` (e.g. from CLI flags) is applied on top of the
    file's own `log_levels:` section.
    """
    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}

    log_levels = dict(raw.get("log_levels") or {})
    log_levels.update(log_levels_override or {})
    configure_logging(raw.get("log"), log_levels)
    discover_modules()
    discover_extensions()

    intervals_map = raw.get("intervals") or {}
    modules_config = raw.get("modules") or {}
    heartbeat = parse_duration(raw.get("heartbeat", "1s"))

    max_workers = raw.get("max_workers")
    if max_workers is not None:
        max_workers = int(max_workers)
    fetch_timeout_raw = raw.get("fetch_timeout")
    fetch_timeout = parse_duration(fetch_timeout_raw) if fetch_timeout_raw is not None else None

    flat: dict[str, Device] = {}
    resolved_module_params_cache: dict[str, dict] = {}
    roots = [
        _build_device(entry, intervals_map, None, flat, modules_config, resolved_module_params_cache)
        for entry in raw.get("devices", [])
    ]

    extensions_registry = _load_extensions(raw, flat)
    tick_hooks = [obj.on_tick for obj in extensions_registry.values() if hasattr(obj, "on_tick")]

    tasks: list[Task] = []
    for entry in raw.get("tasks", []):
        tasks.append(_build_task(entry, flat, tasks, extensions_registry))

    tags = [t.tag for t in tasks]
    duplicates = {t for t in tags if tags.count(t) > 1}
    if duplicates:
        raise ConfigError(f"duplicate task tag(s): {sorted(duplicates)}")

    return System(heartbeat=heartbeat, roots=roots, devices=flat, tasks=tasks,
                  max_workers=max_workers, fetch_timeout=fetch_timeout, tick_hooks=tick_hooks)
