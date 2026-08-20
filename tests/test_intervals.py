"""Tests for phc.core.intervals: duration and time string parsing."""

from datetime import datetime, timedelta

import pytest

from phc.core.intervals import parse_duration, parse_time

# ---------- parse_duration ----------

def test_parse_duration_plain_number_passthrough():
    assert parse_duration(5) == 5.0
    assert parse_duration(2.5) == 2.5


def test_parse_duration_single_unit_backwards_compatible():
    assert parse_duration("10s") == 10.0
    assert parse_duration("500ms") == 0.5
    assert parse_duration("3m") == 180.0
    assert parse_duration("2h") == 7200.0


def test_parse_duration_compound():
    assert parse_duration("1h30m") == 5400.0
    assert parse_duration("1h1m1s") == 3661.0
    assert parse_duration("2D12h") == 2 * 86400.0 + 12 * 3600.0


def test_parse_duration_week_and_day_units():
    assert parse_duration("1W") == 7 * 86400.0
    assert parse_duration("1D") == 86400.0


def test_parse_duration_rejects_calendar_units():
    with pytest.raises(ValueError):
        parse_duration("1Y")
    with pytest.raises(ValueError):
        parse_duration("1M")


def test_parse_duration_rejects_duplicate_unit():
    with pytest.raises(ValueError):
        parse_duration("1h2h")


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError):
        parse_duration("not-a-duration")
    with pytest.raises(ValueError):
        parse_duration("10x")
    with pytest.raises(ValueError):
        parse_duration("10s garbage")


def test_parse_duration_rejects_non_string_non_number():
    with pytest.raises(ValueError):
        parse_duration(None)
    with pytest.raises(ValueError):
        parse_duration(["10s"])


# ---------- parse_time: absolute forms ----------

def test_parse_time_integer_literal_timestamp():
    assert parse_time("1700000000") == 1700000000.0


def test_parse_time_hhmmss_today_no_rollforward():
    now = datetime.now()
    target = now.replace(hour=0, minute=0, second=1, microsecond=0)
    if target < now:
        # already past midnight+1s; still expect no rollforward (may be in the past)
        pass
    result = parse_time("00:00:01")
    result_dt = datetime.fromtimestamp(result)
    assert result_dt.hour == 0 and result_dt.minute == 0 and result_dt.second == 1
    assert result_dt.date() == now.date()


def test_parse_time_hhmm_without_seconds():
    result = parse_time("09:05")
    result_dt = datetime.fromtimestamp(result)
    assert result_dt.hour == 9 and result_dt.minute == 5 and result_dt.second == 0


def test_parse_time_compact_hms_rolls_forward_if_past():
    now = datetime.now()
    past_time = (now - timedelta(hours=1)).time()
    spec = f"{past_time.hour}h{past_time.minute}m{past_time.second}s"
    result = parse_time(spec)
    assert result > now.timestamp()
    result_dt = datetime.fromtimestamp(result)
    assert result_dt.date() == (now + timedelta(days=1)).date()


def test_parse_time_compact_hms_future_today_no_rollforward():
    now = datetime.now()
    future_time = (now + timedelta(hours=1)).time()
    spec = f"{future_time.hour}h{future_time.minute}m{future_time.second}s"
    result = parse_time(spec)
    result_dt = datetime.fromtimestamp(result)
    assert result_dt.date() == now.date()


def test_parse_time_iso_date():
    result = parse_time("2026-01-15")
    result_dt = datetime.fromtimestamp(result)
    assert (result_dt.year, result_dt.month, result_dt.day) == (2026, 1, 15)


def test_parse_time_iso_datetime():
    result = parse_time("2026-01-15T09:05:10")
    result_dt = datetime.fromtimestamp(result)
    assert result_dt.hour == 9 and result_dt.minute == 5 and result_dt.second == 10


def test_parse_time_iso_date_in_past_not_rolled_forward():
    result = parse_time("2020-01-01")
    result_dt = datetime.fromtimestamp(result)
    assert result_dt.year == 2020


# ---------- parse_time: relative "+" forms ----------

def test_parse_time_relative_seconds():
    before = datetime.now().timestamp()
    result = parse_time("+10s")
    after = datetime.now().timestamp()
    assert before + 9.5 <= result <= after + 10.5


def test_parse_time_relative_compound_fixed_units():
    before = datetime.now()
    result = parse_time("+1h1m1s")
    result_dt = datetime.fromtimestamp(result)
    expected = before + timedelta(hours=1, minutes=1, seconds=1)
    assert abs((result_dt - expected).total_seconds()) < 2


def test_parse_time_relative_years_months_calendar_arithmetic():
    before = datetime.now()
    result = parse_time("+1Y2M")
    result_dt = datetime.fromtimestamp(result)
    assert result_dt.year == before.year + 1
    expected_month = (before.month - 1 + 2) % 12 + 1
    assert result_dt.month == expected_month


def test_parse_time_relative_year_month_and_days_combined():
    before = datetime.now()
    result = parse_time("+1Y3D")
    result_dt = datetime.fromtimestamp(result)
    # year bumped by calendar arithmetic, then 3 days added on top
    calendar_only = before.replace(year=before.year + 1)
    expected = calendar_only + timedelta(days=3)
    assert abs((result_dt - expected).total_seconds()) < 2


# ---------- calendar edge cases (via relative +Y/+M) ----------

def test_add_calendar_clamps_feb_29_non_leap_year():
    from phc.core.intervals import _add_calendar
    dt = datetime(2024, 2, 29, 10, 30, 0)  # 2024 is a leap year
    result = _add_calendar(dt, years=1)  # 2025 is not
    assert (result.year, result.month, result.day) == (2025, 2, 28)
    assert (result.hour, result.minute, result.second) == (10, 30, 0)


def test_add_calendar_month_overflow_carries_year():
    from phc.core.intervals import _add_calendar
    dt = datetime(2026, 11, 15)
    result = _add_calendar(dt, months=3)
    assert (result.year, result.month, result.day) == (2027, 2, 15)


def test_add_calendar_month_underflow_negative_offset_crosses_year():
    from phc.core.intervals import _add_calendar
    dt = datetime(2026, 1, 15)
    result = _add_calendar(dt, months=-2)
    assert (result.year, result.month, result.day) == (2025, 11, 15)


def test_add_calendar_clamps_day_on_month_shrink():
    from phc.core.intervals import _add_calendar
    dt = datetime(2026, 1, 31)
    result = _add_calendar(dt, months=1)  # Feb has 28 days in 2026
    assert (result.year, result.month, result.day) == (2026, 2, 28)


# ---------- repeat rollforward ----------

def test_parse_time_repeat_no_rollforward_if_already_future():
    result = parse_time("+1h", repeat="10m")
    assert result > datetime.now().timestamp() + 3500


def test_parse_time_repeat_rolls_forward_past_time():
    past_spec = "2020-01-01T00:00:00"
    result = parse_time(past_spec, repeat="1D")
    now_ts = datetime.now().timestamp()
    assert result >= now_ts
    assert result - now_ts < 86400.0


def test_parse_time_repeat_rollforward_is_multiple_of_interval():
    past_spec = "2020-01-01T00:00:00"
    base = datetime(2020, 1, 1).timestamp()
    result = parse_time(past_spec, repeat="1h")
    assert (result - base) % 3600.0 < 1e-6 or abs((result - base) % 3600.0 - 3600.0) < 1e-6


def test_parse_time_repeat_rejects_non_positive_interval():
    with pytest.raises(ValueError):
        parse_time("2020-01-01T00:00:00", repeat="0s")


# ---------- misc ----------

def test_parse_time_empty_raises():
    with pytest.raises(ValueError):
        parse_time("")
    with pytest.raises(ValueError):
        parse_time(None)


def test_parse_time_garbage_raises():
    with pytest.raises(ValueError):
        parse_time("not-a-time")
