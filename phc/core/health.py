"""Per-device health: whether a device's I/O is currently working.

A fetch failure has always been logged and swallowed, so that one flaky
device cannot stall the tick or take down its siblings (see
phc.core.scheduler.Scheduler._await_io). The cost was that a dead device
became invisible: its endpoints simply stopped changing, which looks
exactly like a sensor whose reading is genuinely steady. Nothing in the
device state, the web UI, or a task condition could tell the difference,
and the only evidence was a line in a log nobody reads until something
has already gone wrong.

This records that outcome as state, so it can be queried (`available()`
in a task condition), displayed, and logged on transition rather than on
every occurrence.
"""

import time


class DeviceHealth:
    """One device's I/O outcome history.

    Deliberately counts CONSECUTIVE failures rather than a total: the
    useful question is "is this device working now", and a device that
    failed twice last week and has been fine since is healthy. A total
    would need a separate decay rule to answer the same thing.

    Times are monotonic (see phc.core.scheduler's two-clock note) -- they
    are only ever used to measure elapsed time, never displayed as an
    absolute timestamp.
    """

    __slots__ = ("consecutive_failures", "last_success", "last_failure", "last_error",
                 "total_failures")

    def __init__(self):
        self.consecutive_failures = 0
        self.last_success: float | None = None
        self.last_failure: float | None = None
        # str, not the exception object: it is kept for display, and
        # holding the exception would pin its whole traceback and frames
        # alive for the life of the process.
        self.last_error: str | None = None
        self.total_failures = 0

    @property
    def healthy(self) -> bool:
        """True unless the most recent I/O attempt failed.

        A device that has never been polled at all is healthy: it has not
        failed, and reporting a device with no `update:` interval as
        unhealthy forever would be actively misleading."""
        return self.consecutive_failures == 0

    def record_success(self, now: float | None = None) -> bool:
        """Note a successful I/O. Returns True if this is a RECOVERY (the
        previous attempt had failed), so the caller can log the transition
        rather than every success."""
        recovered = self.consecutive_failures > 0
        self.consecutive_failures = 0
        self.last_success = time.monotonic() if now is None else now
        return recovered

    def record_failure(self, error: str, now: float | None = None) -> bool:
        """Note a failed I/O. Returns True if this is the FIRST failure
        after a healthy period, so the caller can log the transition
        instead of repeating itself every tick for a device that has been
        unreachable for hours."""
        first = self.consecutive_failures == 0
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_failure = time.monotonic() if now is None else now
        self.last_error = error
        return first

    def __repr__(self) -> str:
        if self.healthy:
            return "<DeviceHealth healthy>"
        return (f"<DeviceHealth failing consecutive={self.consecutive_failures} "
                f"last_error={self.last_error!r}>")
