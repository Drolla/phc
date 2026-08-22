"""The System object and load_system(): the top of this package.

Ties every other layer together into a runnable system.
"""

from pathlib import Path

import yaml

from phc.core.config.descriptors import ModuleDescriptor, _load_module_descriptor
from phc.core.config.devices import _build_device
from phc.core.config.extensions import _load_extensions
from phc.core.config.hooks import _collect_history_records, _make_history_tick_hook, _make_sticky_tick_hook
from phc.core.config.params import _build_effective_module, _ModuleConfig, _resolve_module_config
from phc.core.config.tasks import _build_task
from phc.core.config.yamlio import _find_placeholders, _flatten_list_entries, _include_stack, _IncludeLoader
from phc.core.device import Device
from phc.core.errors import ConfigError
from phc.core.extension import check_lifecycle_hooks, collect_hook
from phc.core.intervals import parse_duration
from phc.core.logging_setup import configure_logging
from phc.core.registry import discover_extensions, discover_modules
from phc.core.task import Task, TaskRegistry


class System:
    """A fully-loaded system: device tree, tasks, and scheduler settings.

    Includes the flat id index, built by load_system()."""

    def __init__(self, heartbeat: float, roots: list[Device], devices: dict[str, Device],
                 tasks: TaskRegistry | list[Task] | None = None, max_workers: int | None = None,
                 fetch_timeout: float | None = None, tick_hooks: list | None = None,
                 start_hooks: list | None = None, stop_hooks: list | None = None,
                 extensions: dict[str, object] | None = None):
        self.heartbeat = heartbeat
        self.roots = roots
        self.devices = devices
        # Normally a TaskRegistry (load_system always builds one). A plain
        # list is accepted for a hand-constructed System in a test, and
        # wrapped so `system.tasks` has one type either way -- note the
        # wrapper cannot build new tasks, having no builder.
        self.tasks = tasks if isinstance(tasks, TaskRegistry) else TaskRegistry(tasks)
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
        # One-time async lifecycle callables auto-collected from configured
        # extension instances that expose on_start(devices)/on_stop(devices)
        # (see _load_extensions/load_system) -- passed straight to
        # Scheduler(start_hooks=..., stop_hooks=...).
        self.start_hooks = start_hooks or []
        self.stop_hooks = stop_hooks or []
        # The _load_extensions registry, keyed by "<extension_name>.<instance_name>"
        # -- exposed so a caller (e.g. phc.py's --debug-portal-port) can check
        # what the system YAML already configured before adding its own
        # extension instance on top of it.
        self.extensions = extensions or {}

    def scheduled_devices(self) -> dict[str, Device]:
        """Return the subset of `devices` that have an update_interval.

        I.e. those auto-polled by the Scheduler."""
        return {qid: d for qid, d in self.devices.items() if d.update_interval is not None}


def load_system(path: str | Path, log_levels_override: dict | None = None) -> System:
    """Load and build a complete System from a system YAML file at `path`.

    Configures logging and discovers device modules/extensions as a side
    effect, then builds the device tree, resolves each device's
    params/endpoints/interval against its module, builds every configured
    extension instance (see _load_extensions), and builds tasks. Once the
    resulting System is assembled, calls on_bind(system) on every extension
    instance that defines it, so an instance needing the fully-built System
    (e.g. its tasks:, unavailable at that instance's own configure()-time)
    can bind to it before the Scheduler starts ticking.

    `log_levels_override` (e.g. from CLI flags) is merged into every stream
    destination's `levels:` -- see phc.core.logging_setup.configure_logging.
    """
    _include_stack.clear()
    with open(path, encoding="utf-8") as f:
        raw = yaml.load(f, Loader=_IncludeLoader) or {}

    placeholders = _find_placeholders(raw)
    if placeholders:
        listed = "\n".join(f"  - {p}" for p in placeholders)
        raise ConfigError(
            f"{path}: not runnable as-is -- it still has example "
            f"!placeholder value(s) that must be replaced with real "
            f"ones first:\n{listed}")

    if "log_levels" in raw:
        raise ConfigError(
            "log_levels: is no longer a separate top-level key -- put each destination's "
            "'levels:' under log: instead, e.g. "
            "log: [{dest: stdout, levels: {default: INFO, scheduler: DEBUG}}]")
    try:
        configure_logging(raw.get("log"), log_levels_override, Path(path).resolve().parent)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    # `plugin_paths:` are resolved relative to the system YAML's own
    # directory (like a `log:` file destination, and unlike an extension's
    # own path params), so a config that ships its private device modules
    # alongside itself stays relocatable.
    config_dir = Path(path).resolve().parent
    plugin_paths = [str((config_dir / p).resolve()) for p in (raw.get("plugin_paths") or [])]
    for plugin_path in plugin_paths:
        if not Path(plugin_path).is_dir():
            raise ConfigError(f"plugin_paths: no such directory: {plugin_path}")
    discover_modules(plugin_paths=plugin_paths)
    discover_extensions(plugin_paths=plugin_paths)

    intervals_map = raw.get("intervals") or {}
    modules_config = raw.get("modules") or {}
    heartbeat = parse_duration(raw.get("heartbeat", "1s"))

    max_workers = raw.get("max_workers")
    if max_workers is not None:
        max_workers = int(max_workers)
    fetch_timeout_raw = raw.get("fetch_timeout")
    fetch_timeout = parse_duration(fetch_timeout_raw) if fetch_timeout_raw is not None else None

    # Validate every modules.<name>: entry up front, regardless of whether a
    # device actually uses that module -- otherwise a typo'd module name
    # (e.g. "zwya" instead of "zway") is silently ignored here and only
    # surfaces later, confusingly, as "parameter 'base_url' is required" on
    # the first zway device. _load_module_descriptor raises ConfigError for
    # an unknown module name; _resolve_module_config validates its params;
    # _build_effective_module validates any modules.<name>.device_profiles/
    # endpoint_profiles overlay.
    flat: dict[str, Device] = {}
    module_config_cache: dict[str, _ModuleConfig] = {}
    effective_module_cache: dict[str, ModuleDescriptor] = {}
    for module_name in modules_config:
        module = _load_module_descriptor(module_name)
        module_config_cache[module_name] = _resolve_module_config(module, modules_config)
        effective_module_cache[module_name] = _build_effective_module(module, modules_config)

    # One scratch dict per loaded system, handed to every device built from
    # it (see Device.context): where a module keeps state shared between its
    # own instances, scoped to this System rather than to the process.
    device_context: dict = {}
    roots = [
        _build_device(entry, intervals_map, None, flat, modules_config, module_config_cache,
                      effective_module_cache, device_context)
        for entry in _flatten_list_entries(raw.get("devices", []))
    ]

    # Every device's update_interval is resolved by now, so this is the
    # first point an endpoint's history sampling cadence (its own
    # interval:, or else its device's update:) can be determined -- see
    # _collect_history_records.
    history_records = _collect_history_records(flat)

    extensions_registry = _load_extensions(raw, flat, config_dir)
    # Lifecycle hooks are found by NAME on each instance (see
    # phc.core.extension for the contract and the timing of each), so a
    # misspelled one would silently never run -- checked here instead.
    for instance_key, instance in extensions_registry.items():
        check_lifecycle_hooks(instance, instance_key)

    tick_hooks = collect_hook(extensions_registry, "on_tick")
    if history_records:
        tick_hooks.append(_make_history_tick_hook(history_records))
    # Same auto-collection as tick_hooks above, but for a resource an
    # extension instance needs to start/stop once rather than every tick
    # (e.g. phc.extensions.web_ui's aiohttp server) -- see
    # System.start_hooks/stop_hooks.
    start_hooks = collect_hook(extensions_registry, "on_start")
    stop_hooks = collect_hook(extensions_registry, "on_stop")

    # task_specs: entries are name -> raw dict, resolved lazily by a
    # `create_task` action's `template:` (see phc.core.task.CreateTaskAction) --
    # not built into Task objects here, matching the existing laziness of a
    # create_task action's own literal `specs:` (see _build_task). Only a
    # duplicate-tag check happens eagerly, to catch a copy-paste mistake at
    # load time rather than only once some template is actually used.
    task_specs: dict[str, dict] = {}
    for entry in _flatten_list_entries(raw.get("task_specs", [])):
        spec_tag = entry["tag"]
        if spec_tag in task_specs:
            raise ConfigError(f"task_specs: duplicate tag {spec_tag!r}")
        task_specs[spec_tag] = entry

    # The registry owns the live task list AND the context needed to build
    # more of them at runtime (create_task, extensions.timer). _build_task is
    # injected here as its builder, which is what lets phc.core.task create
    # tasks without importing this module -- see TaskRegistry's docstring.
    sticky_endpoints: set = set()
    tasks = TaskRegistry(build_task=_build_task, flat=flat, extensions=extensions_registry,
                          sticky_endpoints=sticky_endpoints, task_specs=task_specs)
    for entry in _flatten_list_entries(raw.get("tasks", [])):
        tasks.add(_build_task(entry, tasks))
    if sticky_endpoints:
        tick_hooks.append(_make_sticky_tick_hook(sticky_endpoints))

    tags = [t.tag for t in tasks]
    duplicates = {t for t in tags if tags.count(t) > 1}
    if duplicates:
        raise ConfigError(f"duplicate task tag(s): {sorted(duplicates)}")

    system = System(heartbeat=heartbeat, roots=roots, devices=flat, tasks=tasks,
                     max_workers=max_workers, fetch_timeout=fetch_timeout, tick_hooks=tick_hooks,
                     start_hooks=start_hooks, stop_hooks=stop_hooks, extensions=extensions_registry)

    # Extra one-time hook, auto-collected like tick_hooks/start_hooks/
    # stop_hooks above, for an extension instance that needs to see the
    # fully-built System (e.g. its tasks: -- not yet built when this
    # instance's own configure() ran, see _load_extensions) before the
    # Scheduler starts ticking. Unlike start_hooks, this runs synchronously
    # here, at load time -- no running event loop is required or assumed.
    for on_bind in collect_hook(extensions_registry, "on_bind"):
        on_bind(system)

    return system
