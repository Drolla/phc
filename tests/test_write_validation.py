"""Opt-in range enforcement on writes (`on_invalid:`).

`min`/`max` were always stored but never enforced -- fine as the UI hint
they were introduced for, less fine when a task computes a setpoint and
sends 95 to a valve that accepts 0-100 but a thermostat that accepts 5-30.
Enforcement is opt-in so every config that declared min/max as a hint
keeps its exact behaviour.
"""

import pytest

from phc.core.config import ConfigError, load_system
from phc.core.endpoint import Endpoint


def _endpoint(mode, **kwargs):
    return Endpoint("brightness", writable=True, value_type="int",
                     min=0, max=100, on_invalid=mode, **kwargs)


# ---------- the three modes ----------

def test_pass_is_the_default_and_changes_nothing():
    """The historical behaviour: min/max are a hint, writes go through."""
    assert Endpoint("b", min=0, max=100).on_invalid == "pass"
    assert _endpoint("pass").to_raw(150) == 150
    assert _endpoint("pass").to_raw(-20) == -20


def test_clamp_writes_the_nearest_in_range_value():
    assert _endpoint("clamp").to_raw(150) == 100
    assert _endpoint("clamp").to_raw(-20) == 0
    assert _endpoint("clamp").to_raw(42) == 42


def test_reject_raises_so_the_write_never_reaches_hardware():
    with pytest.raises(ValueError, match="above the allowed maximum"):
        _endpoint("reject").to_raw(150)
    with pytest.raises(ValueError, match="below the allowed minimum"):
        _endpoint("reject").to_raw(-20)
    assert _endpoint("reject").to_raw(42) == 42


def test_a_one_sided_range_only_checks_that_side():
    lower_only = Endpoint("b", writable=True, min=5, on_invalid="clamp")
    assert lower_only.to_raw(1) == 5
    assert lower_only.to_raw(10_000) == 10_000


# ---------- what is deliberately not checked ----------

def test_none_passes_through_untouched():
    """None is how a failed read is spelled; a range check on it is
    meaningless."""
    assert _endpoint("reject").to_raw(None) is None


def test_non_numeric_values_pass_through():
    """A `values:`-mapped or string endpoint has no meaningful range."""
    text = Endpoint("mode", writable=True, value_type="str", min=0, max=100,
                     on_invalid="reject")
    assert text.to_raw("auto") == "auto"


def test_a_bool_is_not_range_checked():
    """bool is numerically 0/1 in Python, but a switch is not a range."""
    flag = Endpoint("on", writable=True, value_type="bool", min=10, max=20,
                     on_invalid="reject")
    assert flag.to_raw(True) is True


# ---------- ordering against write_transform ----------

def test_the_range_is_checked_before_the_write_transform():
    """min/max describe the logical value a config author writes and a UI
    shows; the transform converts that into whatever the hardware wants.
    Checking after it would compare a raw protocol value against logical
    bounds -- here an inverted 0-100 endpoint, where a valid 90 becomes a
    raw 10 that no longer resembles the declared range."""
    inverted = Endpoint("level", writable=True, value_type="int", min=0, max=100,
                         on_invalid="reject", write_transform="100 - value")
    assert inverted.to_raw(90) == 10          # in range, then transformed
    with pytest.raises(ValueError):
        inverted.to_raw(150)


def test_clamping_happens_before_the_transform_too():
    inverted = Endpoint("level", writable=True, value_type="int", min=0, max=100,
                         on_invalid="clamp", write_transform="100 - value")
    assert inverted.to_raw(150) == 0          # clamped to 100, then inverted


# ---------- construction and config validation ----------

def test_an_unknown_mode_is_rejected_at_construction():
    with pytest.raises(ValueError, match="invalid on_invalid"):
        Endpoint("b", min=0, max=1, on_invalid="explode")


def test_enforcement_without_any_bound_is_rejected():
    """`on_invalid: reject` with nothing to check against is a config
    mistake that would otherwise silently do nothing."""
    with pytest.raises(ValueError, match="needs a min and/or max"):
        Endpoint("b", writable=True, on_invalid="reject")


def test_on_invalid_is_settable_from_yaml(tmp_path):
    config = tmp_path / "system.yaml"
    config.write_text("""
heartbeat: 1s
devices:
  - id: lamp
    module: virtual
    endpoints:
      - key: brightness
        writable: true
        type: int
        min: 0
        max: 100
        on_invalid: clamp
""", encoding="utf-8")
    system = load_system(config)
    endpoint = system.devices["lamp"].endpoint("brightness")
    assert endpoint.on_invalid == "clamp"
    assert endpoint.to_raw(150) == 100


def test_an_invalid_on_invalid_in_yaml_is_a_config_error(tmp_path):
    config = tmp_path / "system.yaml"
    config.write_text("""
heartbeat: 1s
devices:
  - id: lamp
    module: virtual
    endpoints:
      - key: brightness
        writable: true
        type: int
        min: 0
        max: 100
        on_invalid: explode
""", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid on_invalid"):
        load_system(config)


def test_a_rejected_write_does_not_reach_the_device(tmp_path):
    """End to end through Device.set(): the point of `reject` is that the
    value never gets transmitted."""
    from phc.devices.virtual.device import VirtualDevice

    endpoint = Endpoint("brightness", writable=True, value_type="int",
                         min=0, max=100, on_invalid="reject")
    device = VirtualDevice("lamp", endpoints=[endpoint])

    with pytest.raises(ValueError):
        device.set(150, name="brightness")
    assert device.get("brightness") is None, "nothing was staged or transmitted"

    device.set(80, name="brightness")
    assert device._pending == {"brightness": 80}
