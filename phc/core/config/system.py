"""The System object and load_system(): the top of this package.

load_system() runs the system YAML through each top-level section's
owner, in phase order: settings (logging, plugin discovery, scheduler,
intervals) -> devices -> extensions -> tasks -> bind. Each owner declares
the keys it parses as CONFIG_KEYS; any other top-level key is an error.
"""

from pathlib import Path

from phc.core import intervals, logging_setup, memory, registry, scheduler
from phc.core.config.yamlio import _find_placeholders, load_yaml
from phc.core.device import Device
from phc.core.device import config as device_config
from phc.core.device.config import load_devices
from phc.core.endpoint.config import _collect_history_records, _make_history_tick_hook
from phc.core.errors import ConfigError
from phc.core.extension import check_lifecycle_hooks, collect_hook
from phc.core.extension import config as extension_config
from phc.core.extension.config import _load_extensions
from phc.core.logging_setup import configure_logging
from phc.core.registry import discover_plugins
from phc.core.scheduler import parse_settings
from phc.core.task import Task, TaskRegistry
from phc.core.task import config as task_config
from phc.core.task.config import load_tasks

_TOP_LEVEL_KEYS = (*logging_setup.CONFIG_KEYS, *registry.CONFIG_KEYS, *scheduler.CONFIG_KEYS,
                   *intervals.CONFIG_KEYS, *device_config.CONFIG_KEYS,
                   *extension_config.CONFIG_KEYS, *task_config.CONFIG_KEYS)


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
        # A plain list (hand-built System in a test) is wrapped; that wrapper
        # has no builder, so it cannot create tasks.
        self.tasks = tasks if isinstance(tasks, TaskRegistry) else TaskRegistry(tasks)
        # Scheduler I/O limits: thread pool size; per-device wait (s) per tick.
        self.max_workers = max_workers
        self.fetch_timeout = fetch_timeout
        # For the Scheduler: extensions' on_tick/on_start/on_stop, plus the
        # loader's own tick hooks (history sampling, sticky windows).
        self.tick_hooks = tick_hooks or []
        self.start_hooks = start_hooks or []
        self.stop_hooks = stop_hooks or []
        # Instances by "<extension>.<instance>", e.g. for the CLI's
        # --debug-portal-port to see what the YAML already configures.
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
    raw = load_yaml(path) or {}

    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path}: not a system config (got a {type(raw).__name__} at the top level) -- "
            f"looks like a fragment meant to be !include-d, not run with --config directly")

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
    unknown = sorted(set(raw) - set(_TOP_LEVEL_KEYS))
    if unknown:
        raise ConfigError(
            f"{path}: unknown top-level key(s) {', '.join(map(repr, unknown))} -- "
            f"known: {', '.join(_TOP_LEVEL_KEYS)}")

    # Phase 1, settings: everything later phases read.
    config_dir = Path(path).resolve().parent
    try:
        configure_logging(raw.get("log"), log_levels_override, config_dir)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    memory.log_baseline()
    memory.log_rss("PHC core loaded")
    discover_plugins(raw.get("plugin_paths"), config_dir)
    settings = parse_settings(raw)
    intervals_map = raw.get("intervals") or {}

    # Phase 2, devices.
    roots, flat = load_devices(raw, intervals_map)
    memory.log_rss("devices built")
    # Needs every device's resolved update_interval (history's default cadence).
    history_records = _collect_history_records(flat)

    # Phase 3, extensions: they may reference devices and each other.
    extensions_registry = _load_extensions(raw, flat, config_dir)
    # Hooks are found by name, so a misspelled one would silently never run.
    for instance_key, instance in extensions_registry.items():
        check_lifecycle_hooks(instance, instance_key)

    tick_hooks = collect_hook(extensions_registry, "on_tick")
    if history_records:
        tick_hooks.append(_make_history_tick_hook(history_records))
    # Once-only start/stop, e.g. web_ui's HTTP server.
    start_hooks = collect_hook(extensions_registry, "on_start")
    stop_hooks = collect_hook(extensions_registry, "on_stop")

    # Phase 4, tasks: they may reference devices and extension instances.
    tasks, task_tick_hooks = load_tasks(raw, flat, extensions_registry)
    tick_hooks.extend(task_tick_hooks)

    system = System(roots=roots, devices=flat, tasks=tasks, tick_hooks=tick_hooks,
                     start_hooks=start_hooks, stop_hooks=stop_hooks, extensions=extensions_registry,
                     **settings)

    # Phase 5, bind: hands the finished System (e.g. its tasks, not yet built
    # at configure() time) to extensions; synchronous, no event loop needed.
    for on_bind in collect_hook(extensions_registry, "on_bind"):
        on_bind(system)

    memory.log_rss("system loaded")
    return system
