"""Command-line entry point for Pylon Home Control (PHC).

Parses the command line, loads a system YAML config into a `phc.core.config.System`,
and runs it on a `phc.core.scheduler.Scheduler` until interrupted. `--debug-portal-port`
additionally lets phc.extensions.debug_portal's live view be attached to this run
without needing an extensions.debug_portal: entry in the config file itself.
"""

import argparse
import logging
import signal
import sys

import yaml

from phc.core.config import ConfigError, System, load_system
from phc.core.config.descriptors import _load_extension_descriptor, _load_module_descriptor
from phc.core.registry import (discover_extensions, discover_modules, extension_package,
                                module_package, registered_extensions, registered_modules)
from phc.core.scheduler import Scheduler
from phc.extensions.debug_portal.extension import DebugPortalInstance
from phc.extensions.debug_portal.extension import configure as configure_debug_portal

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
    phc/extensions/debug_portal/extension.yaml's own loopback default; only
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
        "host": "127.0.0.1",  # matches phc/extensions/debug_portal/extension.yaml's own default
        "port": port,
        "selectors": selectors or ["*"],
        "shutdown_timeout": 5,
    }
    instance = configure_debug_portal(params, system.devices, "debug_portal.cli")
    instance.on_bind(system)
    return instance


def _validate(argv) -> int:
    """`phc validate --config X`: load the config and report, without
    running it. Everything load_system() does -- module/extension
    discovery, parameter and endpoint resolution, task and action building
    -- happens here, so this catches the same problems a real start would,
    without touching hardware or binding a port. Returns a process exit
    code, so it is usable as a pre-deploy check."""
    parser = argparse.ArgumentParser(prog="phc validate")
    parser.add_argument("--config", required=True, help="path to the system YAML config")
    args = parser.parse_args(argv)

    try:
        system = load_system(args.config)
    except (ConfigError, OSError, yaml.YAMLError) as exc:
        print(f"{args.config}: INVALID\n\n{exc}")
        return 1

    scheduled = len(system.scheduled_devices())
    print(f"{args.config}: OK")
    print(f"  {len(system.devices)} device(s), {scheduled} polled, "
          f"{len(system.tasks)} task(s), {len(system.extensions)} extension instance(s)")
    print(f"  heartbeat {system.heartbeat:g}s")
    return 0


def _describe_parameters(parameters) -> list[str]:
    """One indented line per declared parameter, for the list commands."""
    lines = []
    for spec in parameters:
        override = spec.get("override", "allowed")
        detail = "required" if override == "required" else f"default {spec.get('default')!r}"
        if override == "none":
            detail += ", not overridable"
        if spec.get("scope") == "module":
            detail += ", module-scoped"
        lines.append(f"      {spec['name']} ({detail})")
    return lines


def _list_plugins(argv, kind: str) -> int:
    """`phc list-modules` / `phc list-extensions`: what this installation
    can actually use, including anything provided by an entry point or a
    plugin_paths: directory -- the answer to "is my plugin installed, and
    what does it accept?", which otherwise required reading source."""
    parser = argparse.ArgumentParser(prog=f"phc list-{kind}s")
    parser.add_argument("--plugin-path", metavar="DIR", action="append", default=[],
                         help="also look in this directory (same layout as a system "
                              "YAML's plugin_paths:); repeatable")
    args = parser.parse_args(argv)

    if kind == "module":
        discover_modules(plugin_paths=args.plugin_path)
        names, load = registered_modules(), _load_module_descriptor
    else:
        discover_extensions(plugin_paths=args.plugin_path)
        names, load = registered_extensions(), _load_extension_descriptor

    for name in names:
        try:
            descriptor = load(name)
        except ConfigError as exc:
            print(f"{name}\n    <descriptor unreadable: {exc}>")
            continue
        summary = " ".join((descriptor.description or "").split())
        print(f"{name}    [{module_package(name) if kind == 'module' else extension_package(name)}]")
        if summary:
            print(f"    {summary[:200]}{'...' if len(summary) > 200 else ''}")
        if descriptor.parameters:
            print("    parameters:")
            print("\n".join(_describe_parameters(descriptor.parameters)))
    return 0


_SUBCOMMANDS = {
    "validate": _validate,
    "list-modules": lambda argv: _list_plugins(argv, "module"),
    "list-extensions": lambda argv: _list_plugins(argv, "extension"),
}


def main(argv=None):
    """Parse arguments, load and run the configured system until stopped.

    A leading subcommand (validate/list-modules/list-extensions) is
    dispatched instead. Checked by hand rather than with argparse
    subparsers so the original, subcommand-less `phc --config X` spelling
    keeps working as the default action."""
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] in _SUBCOMMANDS:
        return _SUBCOMMANDS[argv[0]](argv[1:])

    parser = argparse.ArgumentParser(
        prog="phc",
        epilog="subcommands: " + ", ".join(sorted(_SUBCOMMANDS))
                + " (each takes its own --help)")
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
                         help="start phc.extensions.debug_portal's live view on this port for "
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
    # phc.core.config); OSError covers a missing/unreadable --config path
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
