"""Tests for phc.core.endpoint: the two-phase set/update_state state model."""

import pytest

from phc.core.endpoint import Endpoint


def test_initial_state_is_none():
    ep = Endpoint("state")
    assert ep.get() is None
    assert ep.get_event() is None
    assert ep.get_update_time() == 0.0


def test_set_does_not_apply_until_update_state():
    ep = Endpoint("state")
    ep.set("on")
    assert ep.get() is None
    ep.update_state()
    assert ep.get() == "on"


def test_event_fires_on_new_valid_value():
    ep = Endpoint("state")
    ep.set("on")
    ep.update_state()
    assert ep.get_event() == "on"


def test_event_clears_next_cycle_if_unchanged():
    ep = Endpoint("state")
    ep.set("on")
    ep.update_state()
    ep.set("on")
    ep.update_state()
    assert ep.get_event() is None
    assert ep.get() == "on"


def test_event_fires_again_when_value_changes():
    ep = Endpoint("state")
    ep.set("on")
    ep.update_state()
    ep.set("off")
    ep.update_state()
    assert ep.get_event() == "off"
    assert ep.get() == "off"


def test_none_or_empty_string_does_not_produce_event_but_still_updates_state():
    ep = Endpoint("state")
    ep.set("on")
    ep.update_state()
    ep.set("")
    ep.update_state()
    assert ep.get_event() is None
    assert ep.get() == ""


def test_update_time_advances_on_change_only():
    ep = Endpoint("state")
    ep.set("on")
    ep.update_state()
    t1 = ep.get_update_time()
    assert t1 > 0.0
    ep.set("on")
    ep.update_state()
    assert ep.get_update_time() == t1


def test_last_valid_state_is_none_before_any_value():
    ep = Endpoint("state")
    assert ep.get_last_valid_state() is None


def test_last_valid_state_tracks_get_event():
    ep = Endpoint("state")
    ep.set(5)
    ep.update_state()
    assert ep.get_last_valid_state() == 5
    assert ep.get_event() == 5


def test_last_valid_state_holds_through_none_then_matches_state_with_no_event():
    ep = Endpoint("state")
    ep.set(5)
    ep.update_state()
    ep.set(None)
    ep.update_state()
    assert ep.get() is None
    assert ep.get_event() is None
    assert ep.get_last_valid_state() == 5
    ep.set(5)
    ep.update_state()
    assert ep.get() == 5
    assert ep.get_event() is None
    assert ep.get_last_valid_state() == 5


def test_state_property_matches_get_set():
    ep = Endpoint("state")
    ep.state = "on"
    ep.update_state()
    assert ep.state == "on"
    assert ep.event == "on"


def test_last_valid_state_property_matches_getter():
    ep = Endpoint("state")
    ep.state = "on"
    ep.update_state()
    assert ep.last_valid_state == "on"


def test_writable_readable_and_params_defaults():
    ep = Endpoint("state")
    assert ep.readable is True
    assert ep.writable is False
    assert ep.params == {}

    ep2 = Endpoint("temp", readable=True, writable=False, params={"column": "tre200s0"})
    assert ep2.params == {"column": "tre200s0"}


# ---------- type / unit / values ----------

def test_type_unit_values_default_to_none():
    ep = Endpoint("state")
    assert ep.value_type is None
    assert ep.unit is None
    assert ep.values is None


def test_invalid_type_raises():
    with pytest.raises(ValueError):
        Endpoint("state", value_type="bogus")


def test_min_max_default_to_none():
    ep = Endpoint("state")
    assert ep.min is None
    assert ep.max is None


def test_min_max_stored_independent_of_value_type():
    ep = Endpoint("state", min=0, max=100)
    assert ep.min == 0
    assert ep.max == 100


def test_min_greater_than_max_raises():
    with pytest.raises(ValueError):
        Endpoint("state", value_type="int", min=10, max=0)


def test_min_or_max_alone_is_allowed():
    ep = Endpoint("state", value_type="int", min=0)
    assert ep.min == 0
    assert ep.max is None


def test_untyped_to_text_and_from_text_pass_through():
    ep = Endpoint("state")
    ep.set("off")
    ep.update_state()
    assert ep.to_text() == "off"
    assert ep.from_text("off") == "off"
    assert ep.from_text(1) == 1


def test_to_text_returns_empty_string_for_none():
    ep = Endpoint("state")
    assert ep.get() is None
    assert ep.to_text() == ""


def test_numeric_to_text_appends_unit():
    ep = Endpoint("temperature", value_type="float", unit="°C")
    ep.set(23.4)
    ep.update_state()
    assert ep.to_text() == "23.4 °C"


def test_numeric_from_text_strips_unit_and_coerces():
    ep = Endpoint("temperature", value_type="float", unit="°C")
    assert ep.from_text("23.4 °C") == 23.4
    assert ep.from_text("23.4") == 23.4


def test_int_from_text_coerces():
    ep = Endpoint("count", value_type="int")
    assert ep.from_text("42") == 42


# ---------- format ----------

def test_float_format_defaults_to_one_decimal():
    ep = Endpoint("temperature", value_type="float")
    assert ep.format == ".1f"
    ep.set(3.140000000000001)
    ep.update_state()
    assert ep.to_text() == "3.1"


def test_float_format_rounds_rather_than_truncates():
    ep = Endpoint("temperature", value_type="float")
    ep.set(3.98)
    ep.update_state()
    assert ep.to_text() == "4.0"


def test_non_float_value_types_default_to_no_format():
    assert Endpoint("count", value_type="int").format is None
    assert Endpoint("flag", value_type="bool").format is None
    assert Endpoint("state").format is None


def test_explicit_format_overrides_float_default():
    ep = Endpoint("temperature", value_type="float", format=".2f")
    ep.set(3.14159)
    ep.update_state()
    assert ep.to_text() == "3.14"


def test_empty_string_format_opts_out_of_rounding():
    ep = Endpoint("temperature", value_type="float", format="")
    ep.set(3.14159)
    ep.update_state()
    assert ep.to_text() == "3.14159"


def test_format_applies_before_unit_is_appended():
    ep = Endpoint("temperature", value_type="float", unit="°C")
    ep.set(23.449)
    ep.update_state()
    assert ep.to_text() == "23.4 °C"


def test_formatted_text_round_trips_through_from_text():
    ep = Endpoint("temperature", value_type="float", unit="°C")
    ep.set(23.449)
    ep.update_state()
    assert ep.from_text(ep.to_text()) == 23.4


def test_explicit_int_format_applies():
    ep = Endpoint("count", value_type="int", format="04d")
    ep.set(7)
    ep.update_state()
    assert ep.to_text() == "0007"


def test_bool_to_text_and_from_text():
    ep = Endpoint("flag", value_type="bool")
    ep.set(True)
    ep.update_state()
    assert ep.to_text() == "true"
    assert ep.from_text("true") is True
    assert ep.from_text("false") is False


def test_values_mapping_to_text_uses_label():
    ep = Endpoint("state", value_type="int", values={0: "off", 1: "on"})
    ep.set(1)
    ep.update_state()
    assert ep.to_text() == "on"


def test_values_mapping_from_text_accepts_label_or_raw():
    ep = Endpoint("state", value_type="int", values={0: "off", 1: "on"})
    assert ep.from_text("on") == 1
    assert ep.from_text("On") == 1
    assert ep.from_text(1) == 1
    assert ep.from_text(0) == 0


def test_to_text_accepts_explicit_value_argument():
    ep = Endpoint("state", value_type="int", values={0: "off", 1: "on"})
    assert ep.to_text(0) == "off"


# ---------- sticky log values ----------

def _commit(ep, value):
    ep.set(value)
    ep.update_state()


def test_log_aggregation_defaults_to_max():
    ep = Endpoint("state")
    assert ep.log_aggregation == "max"


def test_invalid_log_aggregation_raises():
    with pytest.raises(ValueError):
        Endpoint("state", log_aggregation="bogus")


def test_get_log_value_none_before_subscribing():
    ep = Endpoint("state")
    assert ep.get_log_value("logger") is None


def test_update_log_value_noop_without_subscribers():
    ep = Endpoint("state")
    _commit(ep, 5)
    ep.update_log_value()  # must not raise
    assert ep.get_log_value("logger") is None


def test_update_log_value_noop_while_state_is_none():
    ep = Endpoint("state")
    ep.subscribe_log("logger")
    ep.update_log_value()
    assert ep.get_log_value("logger") is None


def test_update_log_value_tracks_running_max_by_default():
    ep = Endpoint("state", value_type="int")
    ep.subscribe_log("logger")
    _commit(ep, 3)
    ep.update_log_value()
    _commit(ep, 7)
    ep.update_log_value()
    _commit(ep, 2)
    ep.update_log_value()
    assert ep.get_log_value("logger") == 7


def test_update_log_value_tracks_running_min_when_configured():
    ep = Endpoint("state", value_type="int", log_aggregation="min")
    ep.subscribe_log("logger")
    _commit(ep, 3)
    ep.update_log_value()
    _commit(ep, 7)
    ep.update_log_value()
    _commit(ep, 2)
    ep.update_log_value()
    assert ep.get_log_value("logger") == 2


def test_invalidate_log_value_resets_to_none_and_starts_fresh_window():
    ep = Endpoint("state", value_type="int")
    ep.subscribe_log("logger")
    _commit(ep, 5)
    ep.update_log_value()
    ep.invalidate_log_value("logger")
    assert ep.get_log_value("logger") is None

    _commit(ep, 1)
    ep.update_log_value()
    assert ep.get_log_value("logger") == 1


def test_multiple_subscribers_track_independent_sticky_values():
    ep = Endpoint("state", value_type="int")
    ep.subscribe_log("fast_logger")
    ep.subscribe_log("slow_logger")

    _commit(ep, 3)
    ep.update_log_value()
    ep.invalidate_log_value("fast_logger")  # fast_logger samples every tick

    _commit(ep, 9)
    ep.update_log_value()
    ep.invalidate_log_value("fast_logger")

    # fast_logger only ever saw one value at a time since each invalidation
    assert ep.get_log_value("fast_logger") is None
    # slow_logger accumulated the running max across both ticks, untouched
    # by fast_logger's invalidations
    assert ep.get_log_value("slow_logger") == 9


def test_subscribe_log_is_idempotent_and_does_not_reset_sticky_value():
    ep = Endpoint("state", value_type="int")
    ep.subscribe_log("logger")
    _commit(ep, 5)
    ep.update_log_value()
    ep.subscribe_log("logger")
    assert ep.get_log_value("logger") == 5


def test_name_defaults_to_empty_string():
    ep = Endpoint("state")
    assert ep.name == ""


def test_name_is_distinct_from_description():
    ep = Endpoint("state", name="Corridor Light", description="A relay-driven switch")
    assert ep.name == "Corridor Light"
    assert ep.description == "A relay-driven switch"


# ---------- read_transform / write_transform ----------

def test_read_write_transform_default_to_none_and_are_identity():
    ep = Endpoint("state", value_type="float")
    ep.set_raw(23.4)
    ep.update_state()
    assert ep.get() == 23.4
    assert ep.to_raw(23.4) == 23.4


def test_read_transform_applies_calibration_offset_on_set_raw():
    ep = Endpoint("temp", value_type="float", read_transform="value - 1.5")
    ep.set_raw(20.0)
    ep.update_state()
    assert ep.get() == 18.5


def test_read_transform_skipped_for_none_raw_value():
    ep = Endpoint("temp", value_type="float", read_transform="value - 1.5")
    ep.set_raw(None)
    ep.update_state()
    assert ep.get() is None


def test_read_transform_inverts_binary_polarity():
    ep = Endpoint("motion", value_type="int", read_transform="1 - value")
    ep.set_raw(0)
    ep.update_state()
    assert ep.get() == 1
    ep.set_raw(1)
    ep.update_state()
    assert ep.get() == 0


def test_write_transform_applies_to_raw_value():
    ep = Endpoint("temp", value_type="float", writable=True, write_transform="value + 1.5")
    assert ep.to_raw(18.5) == 20.0


def test_write_transform_skipped_for_none_value():
    ep = Endpoint("temp", value_type="float", writable=True, write_transform="value + 1.5")
    assert ep.to_raw(None) is None


def test_plain_set_does_not_apply_read_transform():
    ep = Endpoint("temp", value_type="float", read_transform="value - 1.5")
    ep.set(20.0)
    ep.update_state()
    assert ep.get() == 20.0


def test_invalid_read_transform_raises_value_error():
    with pytest.raises(ValueError):
        Endpoint("state", read_transform="__import__('os')")


def test_invalid_write_transform_raises_value_error():
    with pytest.raises(ValueError):
        Endpoint("state", write_transform="value +")


# ---------- value history ----------

def test_history_disabled_by_default():
    ep = Endpoint("temp", value_type="float")
    assert ep.history_size == 0
    assert ep.get_history() == []
    _commit(ep, 5.0)
    assert ep.record_history() is False
    assert ep.get_history() == []


def test_history_records_committed_state_oldest_first():
    ep = Endpoint("temp", value_type="float", history=4)
    for value in (1.0, 2.0, 3.0):
        _commit(ep, value)
        assert ep.record_history() is True
    assert ep.get_history() == [1.0, 2.0, 3.0]


def test_history_is_bounded_and_drops_oldest():
    ep = Endpoint("temp", value_type="float", history=3)
    for value in (1.0, 2.0, 3.0, 4.0, 5.0):
        _commit(ep, value)
        ep.record_history()
    assert ep.get_history() == [3.0, 4.0, 5.0]


def test_record_history_skips_none_and_reports_false():
    ep = Endpoint("temp", value_type="float", history=4)
    assert ep.get() is None
    assert ep.record_history() is False
    assert ep.get_history() == []


def test_record_history_skips_non_numeric_value():
    ep = Endpoint("state", history=4)
    _commit(ep, "warm")
    assert ep.record_history() is False
    assert ep.get_history() == []


def test_record_history_skips_nan():
    ep = Endpoint("temp", value_type="float", history=4)
    _commit(ep, float("nan"))
    assert ep.record_history() is False
    assert ep.get_history() == []


def test_record_history_accepts_bool_values():
    ep = Endpoint("motion", value_type="bool", history=4)
    _commit(ep, True)
    assert ep.record_history() is True
    assert ep.get_history() == [True]


def test_record_history_records_repeated_identical_values():
    # History samples current state on cadence, not on change -- a stalled
    # sensor's unchanged value is deliberately re-recorded each interval
    # (see record_history()'s docstring), unlike update_state()'s _event,
    # which only fires on an actual change.
    ep = Endpoint("temp", value_type="float", history=4)
    _commit(ep, 5.0)
    ep.record_history()
    ep.record_history()
    assert ep.get_history() == [5.0, 5.0]


def test_invalid_history_size_raises():
    with pytest.raises(ValueError):
        Endpoint("temp", history=-1)
    with pytest.raises(ValueError):
        Endpoint("temp", history=True)
    with pytest.raises(ValueError):
        Endpoint("temp", history=4.5)


def test_history_interval_without_history_raises():
    with pytest.raises(ValueError):
        Endpoint("temp", history_interval=300.0)


def test_history_size_zero_with_interval_raises():
    with pytest.raises(ValueError):
        Endpoint("temp", history=0, history_interval=300.0)


def test_history_interval_defaults_to_none():
    ep = Endpoint("temp", value_type="float", history=4)
    assert ep.history_interval is None


def test_history_interval_stored_when_declared():
    ep = Endpoint("temp", value_type="float", history=4, history_interval=300.0)
    assert ep.history_interval == 300.0


def test_history_and_sticky_track_independently():
    ep = Endpoint("temp", value_type="float", history=4)
    ep.subscribe_log("logger")
    _commit(ep, 3.0)
    ep.update_log_value()
    ep.record_history()
    _commit(ep, 7.0)
    ep.update_log_value()
    ep.record_history()
    assert ep.get_log_value("logger") == 7.0
    assert ep.get_history() == [3.0, 7.0]
