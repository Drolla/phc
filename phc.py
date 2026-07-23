"""Command-line entry point for Pylon Home Control (PHC).

Parses the command line, loads a system YAML config into a `core.config.System`,
and runs it on a `core.scheduler.Scheduler` until interrupted.
"""

import argparse
import logging
import signal
import sys

from core.config import load_system
from core.scheduler import Scheduler

logger = logging.getLogger("phc")


def _parse_log_level_module(value: str) -> tuple[str, str]:
    """Parse one `--log-level-module NAME=LEVEL` argument into (name, level)."""
    name, sep, level = value.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(
            f"invalid --log-level-module value {value!r}: expected NAME=LEVEL")
    return name, level


def main(argv=None):
    """Parse arguments, load and run the configured system until stopped."""
    parser = argparse.ArgumentParser(prog="phc")
    parser.add_argument("--config", required=True, help="path to the system YAML config")
    parser.add_argument("--log-level", metavar="LEVEL",
                         help="default logging level (DEBUG, INFO, WARNING, ERROR); "
                              "overrides log_levels.default in the config file")
    parser.add_argument("--log-level-module", metavar="NAME=LEVEL", action="append",
                         type=_parse_log_level_module, default=[],
                         help="logging level for one module's logger (e.g. scheduler=DEBUG); "
                              "overrides log_levels.<name> in the config file; repeatable")
    args = parser.parse_args(argv)

    log_levels_override = dict(args.log_level_module)
    if args.log_level is not None:
        log_levels_override["default"] = args.log_level

    system = load_system(args.config, log_levels_override=log_levels_override)
    scheduler = Scheduler(system.devices, tasks=system.tasks, heartbeat=system.heartbeat,
                          max_workers=system.max_workers, fetch_timeout=system.fetch_timeout)

    logger.info("phc started: %d device(s), %d scheduled, %d task(s), heartbeat=%.3fs",
                len(system.devices), len(system.scheduled_devices()), len(system.tasks),
                system.heartbeat)

    def _stop(_signum, _frame):
        logger.info("shutting down")
        scheduler.stop()

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    scheduler.run_forever()


if __name__ == "__main__":
    sys.exit(main())
