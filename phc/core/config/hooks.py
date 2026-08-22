"""The per-tick hooks the loader synthesizes from a config.

Advances rule-referenced endpoints' sticky windows, and samples declared
endpoint histories on their own cadence.

Both are plain callables handed to the Scheduler's tick_hooks (see
phc.core.scheduler), so nothing in the scheduler needs to know why they
exist.
"""

import time

from phc.core.device import Device
from phc.core.endpoint import Endpoint
from phc.core.errors import ConfigError


def _make_sticky_tick_hook(sticky_endpoints: set):
    """Build the sticky-window tick hook (see Scheduler pass 4).

    Advances every rule-referenced endpoint's sticky min/max window each
    tick -- the same mechanism phc.extensions.logdb uses for its own
    subscriptions (see LogDbInstance.on_tick), but for
    condition.expr's/kind:script's sticky()/reset_sticky() (see
    phc.core.task._build_rule_namespace). Closes over the SAME set
    object _build_condition/_build_action populate, so a task created
    later at runtime (via create_task) that adds to it is picked up
    automatically on the next tick -- no re-registration needed."""
    def _hook(devices: dict[str, Device]) -> None:
        for endpoint in sticky_endpoints:
            endpoint.update_log_value()
    return _hook


class _HistoryRecord:
    """One endpoint's history-sampling schedule.

    Holds the Endpoint to sample, its resolved sampling interval
    (seconds), and the monotonic time it is next due. Built once by
    _collect_history_records() and driven by _make_history_tick_hook()'s
    tick hook -- a plain __slots__ record, since this is internal
    bookkeeping, not part of any public API."""

    __slots__ = ("endpoint", "interval", "next_due")

    def __init__(self, endpoint: Endpoint, interval: float):
        self.endpoint = endpoint
        self.interval = interval
        # -inf so the very first tick hook invocation is immediately due,
        # matching Device._last_run (see phc.core.device). Must not be 0.0:
        # next_due is compared against time.monotonic(), whose zero point
        # is arbitrary and on some platforms is the boot time -- 0.0 would
        # still be "already due", but only by accident.
        self.next_due = float("-inf")


def _collect_history_records(flat: dict[str, Device]) -> list[_HistoryRecord]:
    """Build one _HistoryRecord per `history:`-declaring endpoint.

    Covers every endpoint of every device in `flat`. An endpoint's own
    history_interval (declared `interval:`) wins; otherwise the owning
    device's resolved update_interval is used, so declaring just
    `history: N` gives "one sample per poll" for free. Raises ConfigError
    for an endpoint on a device with no update: interval and no explicit
    history interval -- there would be no cadence to sample it on.
    Called once, after `roots` is built (see load_system), which is the
    first point every device's resolved update_interval is known."""
    records = []
    for qualified_id, device in flat.items():
        for endpoint in device.endpoints.values():
            if endpoint.history_size == 0:
                continue
            interval = endpoint.history_interval
            if interval is None:
                interval = device.update_interval
            if interval is None:
                raise ConfigError(
                    f"device {qualified_id!r}: endpoint {endpoint.key!r} declares "
                    f"history: but the device has no update: interval to sample on "
                    f"-- give the history an explicit interval "
                    f"(history: {{size: {endpoint.history_size}, interval: 5m}}) or "
                    f"set update: on the device")
            records.append(_HistoryRecord(endpoint, interval))
    return records


def _make_history_tick_hook(records: list[_HistoryRecord]):
    """Build the history-sampling tick hook (see Scheduler pass 4).

    Samples every history-declaring endpoint's CURRENT committed value
    into its buffer, once per its resolved interval -- the history
    counterpart to _make_sticky_tick_hook, but on a per-endpoint cadence
    rather than every tick. Unlike _make_sticky_tick_hook's
    sticky_endpoints set, `records` is fixed at load time and never
    grows at runtime: unlike a task, a device (and hence its endpoints'
    history declarations) cannot be created after startup, so there is
    no live-set-aliasing trick needed here.

    A single time.monotonic() read per hook invocation, then a linear scan
    over `records` -- negligible even at scale. Monotonic, like every
    other interval in the system (see phc.core.scheduler.Scheduler's class
    docstring): sampling a smoothing buffer is a pure cadence, with no
    wall-clock meaning that an NTP/DST step should be allowed to disturb.
    A record's next_due only advances when Endpoint.record_history()
    actually appended a sample (it skips None/non-numeric/NaN, see that
    method), so a device that hasn't been polled yet, or whose last read
    failed, is retried every tick instead of losing a whole interval.
    Deliberately not Task.mark_run()'s whole-multiples catch-up scheme: a
    burst is structurally impossible here (this hook runs at most once per
    tick, appending at most one sample), and a stable sampling grid has no
    value for a smoothing buffer -- so a plain `next_due = mono + interval`
    is enough."""
    def _hook(devices: dict[str, Device]) -> None:
        mono = time.monotonic()
        for record in records:
            if mono < record.next_due:
                continue
            if record.endpoint.record_history():
                record.next_due = mono + record.interval
    return _hook
