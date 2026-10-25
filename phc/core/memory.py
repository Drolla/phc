"""Startup memory breakdown on the "phc.memory" logger.

Logs PHC's resident memory (RSS, top's RES) and its growth since the
previous report, at DEBUG. Shared dependencies count toward whichever
plugin imports them first, and freed memory CPython keeps still counts,
so a report says where memory appeared, not who owns it.

The baseline is read when this module is first imported -- phc.cli does
so before the rest of PHC -- but only logged once load_system() has
configured logging.
"""

import logging

import psutil

logger = logging.getLogger("phc.memory")

_process = psutil.Process()
_baseline = _last = _process.memory_info().rss


def log_baseline() -> None:
    """Report the RSS read at import, before most of PHC was loaded."""
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("%5.1f MB %8s  process start", _baseline / 1e6, "")


def log_rss(label: str) -> None:
    """Report current RSS and its growth since the previous report."""
    global _last
    if not logger.isEnabledFor(logging.DEBUG):
        return
    rss = _process.memory_info().rss
    # Fixed-width numbers first, so successive lines align in a column.
    logger.debug("%5.1f MB %+5.1f MB  %s", rss / 1e6, (rss - _last) / 1e6, label)
    _last = rss
