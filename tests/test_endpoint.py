"""Tests for core.endpoint: the two-phase set/update_state state model."""

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
