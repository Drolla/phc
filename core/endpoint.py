"""Endpoint: a single piece of readable/writable device state."""

import time

from core import scripting

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


class Endpoint:
    """Holds one piece of device state with a two-phase set/update_state model.

    A new value written via set() is staged as the "next" state; it only
    becomes the active state (visible via get()) once update_state() runs,
    which the PHC scheduler calls once per device cycle after fetch().

    See docs/configuration.md for the endpoint field reference (`type`,
    `unit`, `values`, `min`/`max`, `format`, `read_transform`/
    `write_transform`, and the `name` vs `description` convention)."""

    kind: str = "generic"

    def __init__(self, key: str, *, readable: bool = True, writable: bool = False,
                 params: dict | None = None, description: str = "", name: str = "",
                 value_type: str | None = None, unit: str | None = None,
                 values: dict | None = None, log_aggregation: str = "max",
                 min: float | int | None = None, max: float | int | None = None,
                 format: str | None = None, read_transform: str | None = None,
                 write_transform: str | None = None):
        if value_type is not None and value_type not in VALUE_TYPES:
            raise ValueError(f"endpoint {key!r}: invalid type {value_type!r}, "
                              f"expected one of {VALUE_TYPES}")
        if log_aggregation not in LOG_AGGREGATIONS:
            raise ValueError(f"endpoint {key!r}: invalid log_aggregation {log_aggregation!r}, "
                              f"expected one of {LOG_AGGREGATIONS}")
        if min is not None and max is not None and min > max:
            raise ValueError(f"endpoint {key!r}: min ({min!r}) is greater than max ({max!r})")
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
        self.format = format if format is not None else (
            _DEFAULT_FLOAT_FORMAT if value_type == "float" else None)

        self._next_state = None
        self._state = None
        self._last_valid_state = None
        self._event = None
        self._update_time = 0.0
        # Sticky log values: {subscriber_id: value | None}, one slot per
        # logger subscribed via subscribe_log() (see those methods below).
        # A plain dict, not a single value, because independently-scheduled
        # loggers sampling the same endpoint (e.g. two logdb instances with
        # different intervals) each need their own since-last-sample
        # min/max window.
        self._log_subscriptions: dict[str, object] = {}

    @staticmethod
    def _compile_transform(key: str, field: str, source: str | None):
        """Compile a read_transform/write_transform expression via
        core.scripting, or return None if unset. Raises ValueError (not
        ScriptError) so config.py's single `except ValueError` around
        Endpoint construction also catches a bad transform."""
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
        """Like set(), but for a value freshly read from hardware (see
        Device.fetch()): applies `read_transform` first, if declared, to
        correct/invert it (e.g. a calibration offset). None (fetch failure)
        passes through untransformed."""
        if self._read_transform is not None and raw_value is not None:
            raw_value = scripting.evaluate_expression(self._read_transform, {"value": raw_value})
        self.set(raw_value)

    def to_raw(self, value):
        """Apply `write_transform` to `value` (a logical value about to be
        written, e.g. via Device.set()/set_text()), returning what should
        actually be sent to hardware. Identity if unset or `value` is None.
        The write-path counterpart to set_raw()."""
        if self._write_transform is not None and value is not None:
            return scripting.evaluate_expression(self._write_transform, {"value": value})
        return value

    def get(self):
        """Return the current (last-committed) value."""
        return self._state

    def get_event(self):
        """Return the value that changed on the most recent update_state(), or None."""
        return self._event

    def get_update_time(self):
        """Return the time.time() of the most recent update_state() that changed the value."""
        return self._update_time

    def update_state(self):
        """Commit the staged value: promote _next_state to _state and compute this
        tick's change event (see class docstring for the two-phase model)."""
        self._event = None
        if self._next_state != self._state:
            if self._next_state not in (None, ''):
                if self._next_state != self._last_valid_state:
                    self._event = self._next_state
                    self._last_valid_state = self._next_state
            self._state = self._next_state
            self._update_time = time.time()

    def subscribe_log(self, subscriber_id: str) -> None:
        """Register `subscriber_id` (e.g. "logdb.house_log") for sticky
        min/max tracking on this endpoint (see update_log_value()).
        Idempotent -- re-subscribing an already-subscribed id does not
        reset its current sticky value."""
        self._log_subscriptions.setdefault(subscriber_id, None)

    def update_log_value(self) -> None:
        """Advance every subscriber's sticky value from the CURRENT
        committed state (get()), per this endpoint's log_aggregation rule.
        Meant to be called once per tick (after that tick's state has been
        committed) by whichever logger owns each subscription -- see
        core.scheduler's tick-hooks pass. A no-op while the endpoint has
        never been set (get() is None), and for any endpoint with no
        subscribers."""
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
        """The subscriber's current sticky value, or None if nothing has
        been observed since subscribing or since the last
        invalidate_log_value()."""
        return self._log_subscriptions.get(subscriber_id)

    def invalidate_log_value(self, subscriber_id: str) -> None:
        """Reset the subscriber's sticky value to None. Meant to be called
        right after a logger reads it (get_log_value()), so the next
        update_log_value() call starts a fresh min/max window."""
        self._log_subscriptions[subscriber_id] = None

    def to_text(self, value=_UNSET) -> str:
        """Format `value` (defaults to the current state) as display text,
        per this endpoint's declared `values` mapping/`unit`/`value_type`/
        `format` (see class docstring). The standard counterpart to
        from_text()."""
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
        """Parse `text` (typically display text, but an already-raw value --
        e.g. a YAML-native 1 or "on" -- is also accepted) back into this
        endpoint's raw value, per its declared `values` mapping/`unit`/
        `value_type`. The standard counterpart to to_text() -- `int(s)`/
        `float(s)` already parse the fixed-precision text a `format` like
        ".1f" produces, so no separate un-formatting step is needed here."""
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
