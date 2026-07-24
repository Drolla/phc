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


def test_writable_readable_and_parameters_defaults():
    ep = Endpoint("state")
    assert ep.readable is True
    assert ep.writable is False
    assert ep.parameters == {}

    ep2 = Endpoint("temp", readable=True, writable=False, parameters={"column": "tre200s0"})
    assert ep2.parameters == {"column": "tre200s0"}


# ---------- type / unit / values ----------

def test_type_unit_values_default_to_none():
    ep = Endpoint("state")
    assert ep.value_type is None
    assert ep.unit is None
    assert ep.values is None


def test_invalid_type_raises():
    with pytest.raises(ValueError):
        Endpoint("state", value_type="bogus")


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
