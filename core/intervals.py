"""Parsing for duration ("10m", "1h30m") and time ("+1D", "11:30", ISO 8601)
strings used throughout system YAML (update intervals, task `time`/`repeat`)."""

import calendar
import math
import re
from datetime import datetime, timedelta

_TOKEN_RE = re.compile(r"(\d+)\s*(Y|M|W|D|ms|h|m|s)")

_FIXED_UNIT_SECONDS = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "D": 86400.0,
    "W": 7 * 86400.0,
}

_HMS_ONLY_UNITS = {"h", "m", "s"}

_HHMMSS_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")


def _tokenize_compact(spec: str) -> list[tuple[int, str]]:
    """Parse a compact "1Y2M3D4h5m6s"-style string into (amount, unit)
    pairs. The whole string must be consumed by consecutive tokens (no
    leftover characters), and each unit may appear at most once."""
    tokens = []
    seen_units = set()
    pos = 0
    for match in _TOKEN_RE.finditer(spec):
        if match.start() != pos:
            raise ValueError(f"cannot parse {spec!r} at position {pos}")
        amount, unit = match.groups()
        if unit in seen_units:
            raise ValueError(f"duplicate unit {unit!r} in {spec!r}")
        seen_units.add(unit)
        tokens.append((int(amount), unit))
        pos = match.end()
    if pos != len(spec) or not tokens:
        raise ValueError(f"invalid duration/time string: {spec!r}")
    return tokens


def parse_duration(value) -> float:
    """Parse a duration into seconds. Accepts a plain int/float (seconds),
    or a compact string combining fixed-length units: ms, s, m, h, D
    (day), W (week) -- e.g. "10s", "1h30m", "2D12h". Calendar units (Y, M)
    are not valid here since they aren't a fixed number of seconds; use
    parse_time for those."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"invalid duration: {value!r}")
    tokens = _tokenize_compact(value)
    total = 0.0
    for amount, unit in tokens:
        if unit not in _FIXED_UNIT_SECONDS:
            raise ValueError(f"duration {value!r} cannot contain a {unit!r} (calendar) unit")
        total += amount * _FIXED_UNIT_SECONDS[unit]
    return total


def _add_calendar(dt: datetime, years: int = 0, months: int = 0) -> datetime:
    """Add a number of calendar years/months to dt, preserving time-of-day
    and clamping the day to the last valid day of the resulting month
    (e.g. adding 1 month to Jan 31 yields Feb 28/29, not an error)."""
    total_months = dt.month - 1 + months
    year = dt.year + years + total_months // 12
    month = total_months % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _parse_hhmmss(spec: str) -> tuple[int, int, int] | None:
    """Parse an "HH:MM[:SS]" string into (hour, minute, second), or None if
    `spec` doesn't match that shape."""
    match = _HHMMSS_RE.match(spec)
    if not match:
        return None
    hour, minute, second = match.groups()
    return int(hour), int(minute), int(second or 0)


def parse_time(spec: str, repeat=None) -> float:
    """Parse an absolute or relative time into a Unix timestamp.

    spec forms:
      - a plain integer string: a literal Unix timestamp
      - "+<compact>" (e.g. "+1Y2M3D4h"): now, shifted by the given
        calendar (Y, M) and/or fixed (W, D, h, m, s, ms) offset
      - "<compact h/m/s only>" (e.g. "11h59m59s"): today at that
        time-of-day; rolled forward to tomorrow if already in the past
      - "HH:MM[:SS]": today at that time-of-day, no automatic rollforward
      - an ISO 8601 date/datetime string (via datetime.fromisoformat):
        no automatic rollforward

    If `repeat` is given (anything parse_duration accepts) and the
    resulting timestamp is already in the past, it is advanced by whole
    multiples of `repeat` until it is >= now.
    """
    if not spec:
        raise ValueError("time spec must not be empty")

    now = datetime.now()
    relative = spec[0] == "+"
    if relative:
        spec = spec[1:]

    if relative:
        tokens = _tokenize_compact(spec)
        years = months = 0
        seconds = 0.0
        for amount, unit in tokens:
            if unit == "Y":
                years += amount
            elif unit == "M":
                months += amount
            else:
                seconds += amount * _FIXED_UNIT_SECONDS[unit]
        result = _add_calendar(now, years=years, months=months) + timedelta(seconds=seconds)
        timestamp = result.timestamp()

    elif spec.lstrip("-").isdigit():
        timestamp = float(int(spec))

    else:
        try:
            tokens = _tokenize_compact(spec)
        except ValueError:
            tokens = None

        if tokens is not None and all(unit in _HMS_ONLY_UNITS for _, unit in tokens):
            pieces = {"h": 0, "m": 0, "s": 0}
            for amount, unit in tokens:
                pieces[unit] = amount
            result = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
                hours=pieces["h"], minutes=pieces["m"], seconds=pieces["s"])
            timestamp = result.timestamp()
            if timestamp < now.timestamp():
                timestamp += 86400.0

        else:
            hms = _parse_hhmmss(spec)
            if hms is not None:
                hour, minute, second = hms
                result = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
                timestamp = result.timestamp()
            else:
                try:
                    result = datetime.fromisoformat(spec)
                except ValueError:
                    raise ValueError(f"cannot parse time: {spec!r}") from None
                timestamp = result.timestamp()

    if repeat is not None:
        now_ts = now.timestamp()
        if timestamp < now_ts:
            interval = parse_duration(repeat)
            if interval <= 0:
                raise ValueError(f"repeat interval must be positive, got {interval}")
            timestamp += math.ceil((now_ts - timestamp) / interval) * interval

    return timestamp
