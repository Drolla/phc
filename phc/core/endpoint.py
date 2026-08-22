"""Endpoint: a single piece of readable/writable device state."""

import math
import time
from collections import deque

from phc.core import scripting

# Sentinel default for to_text()'s `value` param, so to_text(None) (format an
# explicit None) is distinguishable from "format the current value".
_UNSET = object()

# Endpoint value types recognized by `type:` in module.yaml/instance YAML.
# None (the default) means untyped passthrough -- see Endpoint docstring.
VALUE_TYPES = ("int", "float", "bool", "str")

_TRUTHY_TEXT = {"true", "1", "on", "yes"}

# Default `format` for a float-typed endpoint when none is declared explicitly.
_DEFAULT_FLOAT_FORMAT = ".1f"

# Aggregation rules recognized by `log_aggregation:` -- see the sticky log
# value methods below. "max" is the default: for the common case (alarms,
# "on" states), the most-alarming value seen since the last log sample is
# the one worth keeping, not whatever happened to be current at sample time.
LOG_AGGREGATIONS = ("min", "max")

# What to do with a write whose value falls outside a declared min/max.
# "pass" is the default: min/max are a display hint (e.g. a slider's
# bounds) unless a config opts into enforcement.
ON_INVALID_MODES = ("pass", "reject", "clamp")


class Endpoint:
    """Holds one piece of device state with a two-phase set/update_state model.

    A new value written via set() is staged as the "next" state; it only
    becomes the active state (visible via get()) once update_state() runs,
    which the PHC scheduler calls once per device cycle after fetch().

    See docs/configuration.md for the endpoint field reference (`type`,
    `unit`, `values`, `min`/`max`, `format`, `read_transform`/
    `write_transform`, `history`/`history_interval`, and the `name` vs
    `description` convention).

    `update_time` (wall clock) marks the last value CHANGE, shown as an
    absolute time by the web UI; `last_read_time`/`age` (monotonic) mark
    the last non-None READING, used only to measure elapsed time -- see
    set_raw() for why the two are distinct."""

    kind: str = "generic"

    def __init__(self, key: str, *, readable: bool = True, writable: bool = False,
                 params: dict | None = None, description: str = "", name: str = "",
                 value_type: str | None = None, unit: str | None = None,
                 values: dict | None = None, log_aggregation: str = "max",
                 min: float | int | None = None, max: float | int | None = None,
                 format: str | None = None, read_transform: str | None = None,
                 write_transform: str | None = None, history: int = 0,
                 history_interval: float | None = None, on_invalid: str = "pass"):
        if value_type is not None and value_type not in VALUE_TYPES:
            raise ValueError(f"endpoint {key!r}: invalid type {value_type!r}, "
                              f"expected one of {VALUE_TYPES}")
        if log_aggregation not in LOG_AGGREGATIONS:
            raise ValueError(f"endpoint {key!r}: invalid log_aggregation {log_aggregation!r}, "
                              f"expected one of {LOG_AGGREGATIONS}")
        if min is not None and max is not None and min > max:
            raise ValueError(f"endpoint {key!r}: min ({min!r}) is greater than max ({max!r})")
        if not isinstance(history, int) or isinstance(history, bool) or history < 0:
            raise ValueError(f"endpoint {key!r}: history must be a non-negative int, "
                              f"got {history!r}")
        if history_interval is not None and history == 0:
            raise ValueError(f"endpoint {key!r}: history_interval requires history to be set")
        if on_invalid not in ON_INVALID_MODES:
            raise ValueError(f"endpoint {key!r}: invalid on_invalid {on_invalid!r}, "
                              f"expected one of {ON_INVALID_MODES}")
        if on_invalid != "pass" and min is None and max is None:
            raise ValueError(
                f"endpoint {key!r}: on_invalid {on_invalid!r} needs a min and/or max to "
                f"check against")
        self._read_transform = self._compile_transform(key, "read_transform", read_transform)
        self._write_transform = self._compile_transform(key, "write_transform", write_transform)
        self.key = key
        self.readable = readable
        self.writable = writable
        # Module-authored facts about what this endpoint kind means (e.g.
        # which CSV column backs it). An instance may add or override
        # individual keys (merged per-key in config._merge_endpoints).
        self.params = params or {}
        self.description = description
        self.name = name
        self.value_type = value_type
        self.unit = unit
        self.values = values
        self.log_aggregation = log_aggregation
        self.min = min
        self.max = max
        self.on_invalid = on_invalid
        self.format = format if format is not None else (
            _DEFAULT_FLOAT_FORMAT if value_type == "float" else None)
        self.history_size = history
        self.history_interval = history_interval

        self._next_state = None
        self._state = None
        self._last_valid_state = None
        self._event = None
        self._update_time = 0.0
        self._last_read_time: float | None = None
        # Sticky log values: {subscriber_id: value | None}, one slot per
        # logger subscribed via subscribe_log() (see those methods below).
        # A plain dict, not a single value, because independently-scheduled
        # loggers sampling the same endpoint (e.g. two logdb instances with
        # different intervals) each need their own since-last-sample
        # min/max window.
        self._log_subscriptions: dict[str, object] = {}
        # Bounded FIFO of past committed values, advanced on a cadence by
        # phc.core.config's history tick hook (see record_history() below) --
        # deque(maxlen=0) silently drops every append, so a plain endpoint
        # (history_size == 0) needs no separate guard anywhere.
        self._history: deque = deque(maxlen=history)

    @staticmethod
    def _compile_transform(key: str, field: str, source: str | None):
        """Compile a read/write_transform expression via phc.core.scripting.

        Returns None if unset. Raises ValueError (not ScriptError) so
        config.py's single `except ValueError` around Endpoint
        construction also catches a bad transform."""
        if source is None:
            return None
        try:
            return scripting.compile_expression(source)
        except scripting.ScriptError as exc:
            raise ValueError(f"endpoint {key!r}: invalid {field}: {exc}") from None

    def set(self, new_state):
        """Stage `new_state` as the next value; not visible via get() until update_state()."""
        self._next_state = new_state

    def set_raw(self, raw_value):
        """Like set(), but for a value freshly read from hardware.

        See Device.fetch(). Applies `read_transform` first, if
        declared, to correct/invert it (e.g. a calibration offset).
        None (fetch failure) passes through untransformed.

        A non-None reading also stamps `last_read_time`, which is what
        makes staleness measurable. Note this is deliberately NOT the same
        as update_time: update_time moves only when the value CHANGES, so
        a sensor reporting a steady 20.0 for an hour has an hour-old
        update_time while being perfectly healthy -- indistinguishable,
        from update_time alone, from one that stopped answering an hour
        ago. A None reading (a device that caught its own I/O error and
        reported no value, as the weather and zway modules do) leaves the
        stamp alone, so age() keeps growing and reflects reality."""
        if self._read_transform is not None and raw_value is not None:
            raw_value = scripting.evaluate_expression(self._read_transform, {"value": raw_value})
        if raw_value is not None:
            self._last_read_time = time.monotonic()
        self.set(raw_value)

    def get_last_read_time(self):
        """Time of the most recent non-None reading.

        None if this endpoint has never produced one (see class
        docstring for the clock; see age() in docs/scripting.md)."""
        return self._last_read_time

    def get_age(self, mono: float | None = None):
        """Seconds since the last non-None reading.

        None if there has never been one. Defaults to time.monotonic()."""
        if self._last_read_time is None:
            return None
        return (time.monotonic() if mono is None else mono) - self._last_read_time

    def check_write(self, value):
        """Apply this endpoint's `on_invalid` rule to a pending write.

        Returns the value to actually write. Enforcement is opt-in: a
        task computing a setpoint could otherwise send 95 to a valve
        that accepts 0-100 but a thermostat that accepts 5-30.

        - "pass" (default): write it unchanged; min/max are a UI hint only.
        - "reject": raise ValueError, so the write never reaches hardware
          and the failure is visible rather than silently clipped.
        - "clamp": write the nearest in-range value.

        Non-numeric and None values pass through untouched: a range check
        is meaningless for them, and None is how a failed read is spelled."""
        if self.on_invalid == "pass" or value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value
        low, high = self.min, self.max
        if low is not None and value < low:
            return self._out_of_range(value, low, "below", low)
        if high is not None and value > high:
            return self._out_of_range(value, high, "above", high)
        return value

    def _out_of_range(self, value, bound, direction: str, clamped):
        """Reject or clamp one out-of-range write (see check_write)."""
        if self.on_invalid == "reject":
            raise ValueError(
                f"endpoint {self.key!r}: {value!r} is {direction} the allowed "
                f"{'minimum' if direction == 'below' else 'maximum'} {bound!r}")
        return clamped

    def to_raw(self, value):
        """Convert a logical `value` into what to send to hardware.

        Applies this endpoint's `on_invalid` range rule and then its
        `write_transform` (e.g. for a value about to be written via
        Device.set()/set_text()). Identity if neither is declared, or
        `value` is None. The write-path counterpart to set_raw().

        The range check runs BEFORE the transform: min/max describe the
        logical value a config author writes and a UI shows, while the
        transform exists to convert that into whatever the hardware wants
        (an inverted polarity, a calibration offset), so checking after it
        would compare a raw protocol value against logical bounds."""
        value = self.check_write(value)
        if self._write_transform is not None and value is not None:
            return scripting.evaluate_expression(self._write_transform, {"value": value})
        return value

    def get(self):
        """Return the current (last-committed) value."""
        return self._state

    def get_event(self):
        """Return the value that changed on the most recent update_state(), or None."""
        return self._event

    def get_last_valid_state(self):
        """Return the most recent non-None/non-empty committed value.

        I.e. the value get_event() is gated against (see
        update_state()). Useful for spotting a state change that fired
        no event: on a 5 -> None -> 5 sequence, get() moves twice but
        get_event() stays None both times, since the second 5 never
        differs from this value."""
        return self._last_valid_state

    def get_update_time(self):
        """Return the time.time() of the most recent update_state() that changed the value."""
        return self._update_time

    def update_state(self):
        """Commit the staged value: promote _next_state to _state.

        Also computes this tick's change event (see class docstring for
        the two-phase model)."""
        self._event = None
        if self._next_state != self._state:
            if self._next_state not in (None, ''):
                if self._next_state != self._last_valid_state:
                    self._event = self._next_state
                    self._last_valid_state = self._next_state
            self._state = self._next_state
            self._update_time = time.time()

    def subscribe_log(self, subscriber_id: str) -> None:
        """Register `subscriber_id` for sticky min/max tracking.

        E.g. "logdb.house_log" (see update_log_value()). Idempotent --
        re-subscribing an already-subscribed id does not reset its
        current sticky value."""
        self._log_subscriptions.setdefault(subscriber_id, None)

    def update_log_value(self) -> None:
        """Advance every subscriber's sticky value from the CURRENT state.

        Uses get(), per this endpoint's log_aggregation rule. Meant to
        be called once per tick (after that tick's state has been
        committed) by whichever logger owns each subscription -- see
        phc.core.scheduler's tick-hooks pass. A no-op while the
        endpoint has never been set (get() is None), and for any
        endpoint with no subscribers."""
        current = self._state
        if current is None:
            return
        for subscriber_id, sticky in self._log_subscriptions.items():
            if sticky is None:
                self._log_subscriptions[subscriber_id] = current
            elif self.log_aggregation == "max":
                self._log_subscriptions[subscriber_id] = max(sticky, current)
            else:
                self._log_subscriptions[subscriber_id] = min(sticky, current)

    def get_log_value(self, subscriber_id: str):
        """The subscriber's current sticky value.

        None if nothing has been observed since subscribing or since
        the last invalidate_log_value()."""
        return self._log_subscriptions.get(subscriber_id)

    def invalidate_log_value(self, subscriber_id: str) -> None:
        """Reset the subscriber's sticky value to None.

        Meant to be called right after a logger reads it
        (get_log_value()), so the next update_log_value() call starts a
        fresh min/max window."""
        self._log_subscriptions[subscriber_id] = None

    def record_history(self) -> bool:
        """Append the current committed state to the history buffer.

        Returns True iff a sample was actually appended (get() is the
        source). A no-op (returns False) while history_size == 0 (see
        _history's init comment -- this early return only skips the
        isinstance/isnan work), for a None value (not yet read), and
        for any non-numeric value. A bool is accepted (bool
        is a numeric type in Python, and a 0/1 endpoint's history is a
        legitimate median-filter debounce). A float NaN is skipped: one
        NaN in the buffer makes sorted() order arbitrary, silently
        corrupting every fractile()/median()/average() read for the rest
        of that NaN's time in the window.

        Meant to be called once per sampling interval (see phc.core.config's
        history tick hook) -- not once per tick, and not only on change:
        unlike Endpoint.update_state()'s _event (which fires only when the
        value differs), a stalled sensor's unchanged value is deliberately
        re-recorded each interval."""
        if self.history_size == 0:
            return False
        value = self._state
        if value is None or not isinstance(value, (int, float)):
            return False
        if isinstance(value, float) and math.isnan(value):
            return False
        self._history.append(value)
        return True

    def get_history(self) -> list:
        """This endpoint's history buffer, oldest sample first.

        Empty if history_size == 0 or no sample has been recorded yet."""
        return list(self._history)

    def to_text(self, value=_UNSET) -> str:
        """Format `value` as display text.

        Defaults to the current state, per this endpoint's declared
        `values` mapping/`unit`/`value_type`/`format` (see class
        docstring). The standard counterpart to from_text()."""
        if value is _UNSET:
            value = self.get()
        if value is None:
            return ""
        if self.values is not None and value in self.values:
            return str(self.values[value])
        if self.value_type == "bool":
            text = "true" if value else "false"
        elif self.format:
            text = format(value, self.format)
        else:
            text = str(value)
        if self.unit and self.value_type in ("int", "float"):
            text = f"{text} {self.unit}"
        return text

    def from_text(self, text):
        """Parse `text` back into this endpoint's raw value.

        `text` is typically display text, but an already-raw value --
        e.g. a YAML-native 1 or "on" -- is also accepted, per its
        declared `values` mapping/`unit`/`value_type`. The standard
        counterpart to to_text() -- `int(s)`/`float(s)` already parse
        the fixed-precision text a `format` like ".1f" produces, so no
        separate un-formatting step is needed here."""
        if self.values is not None:
            for raw, label in self.values.items():
                if raw == text or str(label).strip().lower() == str(text).strip().lower():
                    return raw
        if self.value_type is None:
            return text
        s = str(text).strip()
        if self.unit and s.endswith(self.unit):
            s = s[: -len(self.unit)].strip()
        if self.value_type == "int":
            return int(s)
        if self.value_type == "float":
            return float(s)
        if self.value_type == "bool":
            return s.lower() in _TRUTHY_TEXT
        return s

    state = property(get, set)
    event = property(get_event)
    update_time = property(get_update_time)
    last_valid_state = property(get_last_valid_state)
    last_read_time = property(get_last_read_time)
    age = property(get_age)
