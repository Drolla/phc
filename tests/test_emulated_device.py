"""Tests for phc.devices.emulated_device: the latency injection this module
already had (a light regression guard, since virtual_latency's rename to
emulated_device must not change behavior), plus the new `simulate:` value
generation (toggle/drift) and its interaction with an explicit write."""

import time

import pytest

from phc.core.endpoint import Endpoint
from phc.devices.emulated_device.device import EmulatedDevice
from tests.conftest import fetch_sync

_NO_LATENCY = {
    "read_latency": 0, "read_jitter": 0, "read_spike_probability": 0,
    "read_spike_latency": 0, "read_spike_jitter": 0,
    "write_latency": 0, "write_jitter": 0, "write_spike_probability": 0,
    "write_spike_latency": 0, "write_spike_jitter": 0,
}


def _device(endpoints, **extra_params):
    return EmulatedDevice("d", params={**_NO_LATENCY, **extra_params}, endpoints=endpoints)


# ---------- unchanged echo/latency behavior (regression guard for the rename) ----------

def test_endpoint_with_no_simulate_only_echoes_writes():
    ep = Endpoint("value", writable=True)
    d = _device([ep])
    d.set(5, name="value")
    fetch_sync(d)
    d.update_state()
    assert d.get(name="value") == 5

    # A second fetch with no new write reports nothing for this endpoint --
    # same as VirtualDevice's plain echo, not a re-report of the old value.
    fetch_sync(d)
    d.update_state()
    assert d.get(name="value") == 5  # unchanged, not reset to None


def test_write_delay_is_still_applied():
    ep = Endpoint("value", writable=True)
    d = _device([ep], write_latency=0.05, write_jitter=0)
    start = time.monotonic()
    d.set(1, name="value")
    assert time.monotonic() - start >= 0.05


# ---------- simulate: drift ----------

def test_drift_stays_within_min_and_max():
    ep = Endpoint("battery", value_type="int", min=0, max=100,
                  params={"simulate": {"kind": "drift", "step": 50}})
    ep.set(50)
    ep.update_state()
    d = _device([ep])
    for _ in range(200):
        fetch_sync(d)
        d.update_state()
        assert 0 <= d.get(name="battery") <= 100


def test_drift_walks_freely_when_unbounded():
    ep = Endpoint("temperature", value_type="float",
                  params={"simulate": {"kind": "drift", "step": 5}})
    ep.set(20.0)
    ep.update_state()
    d = _device([ep])
    values = []
    for _ in range(20):
        fetch_sync(d)
        d.update_state()
        values.append(d.get(name="temperature"))
    assert len(set(values)) > 1


def test_drift_starts_from_the_endpoints_current_seeded_value():
    ep = Endpoint("battery", value_type="int", min=99, max=100,
                  params={"simulate": {"kind": "drift", "step": 1}})
    ep.set(100)
    ep.update_state()
    d = _device([ep])
    fetch_sync(d)
    d.update_state()
    assert d.get(name="battery") in (99, 100)


# ---------- simulate: toggle ----------

def test_toggle_flips_on_with_given_probability(monkeypatch):
    ep = Endpoint("state", value_type="bool", values={False: "clear", True: "motion"},
                  params={"simulate": {"kind": "toggle", "on_probability": 1.0,
                                        "auto_clear_after": "30s"}})
    ep.set(False)
    ep.update_state()
    d = _device([ep])
    fetch_sync(d)
    d.update_state()
    assert d.get(name="state") is True
    assert d.get_text(name="state") == "motion"


def test_toggle_stays_off_with_zero_probability():
    ep = Endpoint("state", value_type="bool",
                  params={"simulate": {"kind": "toggle", "on_probability": 0.0}})
    ep.set(False)
    ep.update_state()
    d = _device([ep])
    for _ in range(20):
        fetch_sync(d)
        d.update_state()
        assert d.get(name="state") is False


def test_toggle_clears_after_auto_clear_delay():
    ep = Endpoint("state", value_type="bool",
                  params={"simulate": {"kind": "toggle", "on_probability": 1.0,
                                        "auto_clear_after": 0.05}})
    ep.set(False)
    ep.update_state()
    d = _device([ep])
    fetch_sync(d)
    d.update_state()
    assert d.get(name="state") is True

    time.sleep(0.06)
    fetch_sync(d)
    d.update_state()
    assert d.get(name="state") is False


def test_unrecognized_simulate_kind_raises():
    ep = Endpoint("state", value_type="bool", params={"simulate": {"kind": "bogus"}})
    d = _device([ep])
    with pytest.raises(ValueError, match="bogus"):
        fetch_sync(d)


# ---------- writable + simulate together ----------

def test_explicit_write_wins_over_simulation_for_that_tick():
    ep = Endpoint("state", writable=True, value_type="bool",
                  params={"simulate": {"kind": "toggle", "on_probability": 0.0}})
    ep.set(False)
    ep.update_state()
    d = _device([ep])
    d.set(True, name="state")
    fetch_sync(d)
    d.update_state()
    assert d.get(name="state") is True
