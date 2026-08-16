"""Command-line entry point for Pylon Home Control (PHC).

Parses the command line, loads a system YAML config into a `core.config.System`,
and runs it on a `core.scheduler.Scheduler` until interrupted. `--debug-portal-port`
additionally lets extensions.debug_portal's live view be attached to this run
without needing an extensions.debug_portal: entry in the config file itself.
"""

import argparse
import logging
import signal
import sys

import yaml

from core.config import ConfigError, System, load_system
from core.scheduler import Scheduler
from extensions.debug_portal.extension import DebugPortalInstance
from extensions.debug_portal.extension import configure as configure_debug_portal

logger = logging.getLogger("phc")


def _parse_log_level_module(value: str) -> tuple[str, str]:
    """Parse one `--log-level-module NAME=LEVEL` argument into (name, level)."""
    name, sep, level = value.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(
            f"invalid --log-level-module value {value!r}: expected NAME=LEVEL")
    return name, level


def _resolve_debug_portal_instance(system: System, port: int | None,
                                    selectors: list[str]) -> DebugPortalInstance | None:
    """Build a DebugPortalInstance from --debug-portal-port/--debug-portal-selector,
    bound to `system` -- or None if `port` wasn't given (the flag's absence is
    the common case: no debug portal for this run at all). Host is fixed to
    extensions/debug_portal/extension.yaml's own loopback default; only
    port/selectors are exposed on the command line.

    Raises ValueError if the system YAML already configures its own
    extensions.debug_portal.<instance> -- rather than silently running a
    second, independent debug portal that might collide with the first one's
    port, this asks the caller (main(), via parser.error()) to remove one."""
    if port is None:
        return None
    existing = sorted(key for key in system.extensions if key.startswith("debug_portal."))
    if existing:
        raise ValueError(
            f"--debug-portal-port was given, but the config file already configures "
            f"extensions.debug_portal ({', '.join(existing)}); remove one")
    params = {
        "host": "127.0.0.1",  # matches extensions/debug_portal/extension.yaml's own default
        "port": port,
        "selectors": selectors or ["*"],
        "shutdown_timeout": 5,
    }
    instance = configure_debug_portal(params, system.devices, "debug_portal.cli")
    instance.on_bind(system)
    return instance


def main(argv=None):
    """Parse arguments, load and run the configured system until stopped."""
    parser = argparse.ArgumentParser(prog="phc")
    parser.add_argument("--config", required=True, help="path to the system YAML config")
    parser.add_argument("--log-level", metavar="LEVEL",
                         help="default logging level (DEBUG, INFO, WARNING, ERROR); "
                              "overrides each stream destination's levels.default in the "
                              "config file's log: section (file destinations are unaffected)")
    parser.add_argument("--log-level-module", metavar="NAME=LEVEL", action="append",
                         type=_parse_log_level_module, default=[],
                         help="logging level for one module's logger (e.g. scheduler=DEBUG); "
                              "overrides that stream destination's levels.<name> in the "
                              "config file's log: section (file destinations are unaffected); "
                              "repeatable")
    parser.add_argument("--debug-portal-port", type=int, metavar="PORT",
                         help="start extensions.debug_portal's live view on this port for "
                              "this run, even if --config has no extensions.debug_portal: "
                              "entry of its own (loopback-only; conflicts with one that's "
                              "already there)")
    parser.add_argument("--debug-portal-selector", metavar="PATTERN", action="append",
                         default=[],
                         help="'<device-glob>/<endpoint-glob>' selector for "
                              "--debug-portal-port's endpoint table (same syntax as "
                              "extensions.logdb's own selectors; repeatable); everything "
                              "('*') if omitted")
    args = parser.parse_args(argv)

    if args.debug_portal_selector and args.debug_portal_port is None:
        parser.error("--debug-portal-selector requires --debug-portal-port")

    log_levels_override = dict(args.log_level_module)
    if args.log_level is not None:
        log_levels_override["default"] = args.log_level

    # ConfigError covers invalid/inconsistent config content (see
    # core.config); OSError covers a missing/unreadable --config path
    # itself (FileNotFoundError, PermissionError, ...); yaml.YAMLError
    # covers malformed YAML syntax. All three are user-facing "the config
    # file is broken" problems, not internal bugs, so they're reported as a
    # plain error message rather than a Python traceback -- anything else
    # still raises normally.
    try:
        system = load_system(args.config, log_levels_override=log_levels_override)
    except (ConfigError, OSError, yaml.YAMLError) as exc:
        parser.error(str(exc))

    try:
        debug_portal_instance = _resolve_debug_portal_instance(
            system, args.debug_portal_port, args.debug_portal_selector)
    except ValueError as exc:
        parser.error(str(exc))

    tick_hooks = system.tick_hooks
    start_hooks = system.start_hooks
    stop_hooks = system.stop_hooks
    if debug_portal_instance is not None:
        tick_hooks = tick_hooks + [debug_portal_instance.on_tick]
        start_hooks = start_hooks + [debug_portal_instance.on_start]
        stop_hooks = stop_hooks + [debug_portal_instance.on_stop]

    scheduler = Scheduler(system.devices, tasks=system.tasks, heartbeat=system.heartbeat,
                          max_workers=system.max_workers, fetch_timeout=system.fetch_timeout,
                          tick_hooks=tick_hooks,
                          start_hooks=start_hooks, stop_hooks=stop_hooks)

    logger.info("started: %d device(s), %d scheduled, %d task(s), heartbeat=%.3fs",
                len(system.devices), len(system.scheduled_devices()), len(system.tasks),
                system.heartbeat)

    # Scheduler.stop() is thread/signal-safe and wakes the heartbeat sleep
    # immediately (see its docstring), so a plain signal.signal handler is
    # enough here -- and unlike loop.add_signal_handler it works on Windows
    # too, and needs no reference to a loop the Scheduler creates lazily.
    def _stop(_signum, _frame):
        logger.info("shutting down")
        scheduler.stop()

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    scheduler.run_forever()


if __name__ == "__main__":
    sys.exit(main())
