"""Tests for core.endpoint: the two-phase set/update_state state model."""

import pytest

from core.endpoint import Endpoint


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


def test_state_property_matches_get_set():
    ep = Endpoint("state")
    ep.state = "on"
    ep.update_state()
    assert ep.state == "on"
    assert ep.event == "on"


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
