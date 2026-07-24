"""Tests for core.selectors: device/endpoint selector pattern resolution."""

import pytest

from core.config import ConfigError
from core.endpoint import Endpoint
from core.selectors import resolve_selectors
from devices.virtual.device import VirtualDevice


def _house(**endpoint_kwargs):
    lamp = VirtualDevice("desk_lamp", endpoints=[
        Endpoint("power", writable=True, value_type="int", **endpoint_kwargs),
        Endpoint("brightness", writable=True, value_type="int"),
    ], parent_qualified_id="house")
    house = VirtualDevice("house", children=[lamp])
    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True, value_type="int")])
    flat = {"house": house, "house.desk_lamp": lamp, "living_light": light}
    return flat


def test_resolve_selectors_specific_device_wildcard_endpoint():
    flat = _house()
    pairs = resolve_selectors(["house.desk_lamp/*"], flat)
    assert pairs == [("house.desk_lamp", "brightness"), ("house.desk_lamp", "power")]


def test_resolve_selectors_wildcard_wildcard_matches_everything():
    flat = _house()
    pairs = resolve_selectors(["*/*"], flat)
    assert ("house.desk_lamp", "power") in pairs
    assert ("house.desk_lamp", "brightness") in pairs
    assert ("living_light", "state") in pairs


def test_resolve_selectors_bare_star_same_as_wildcard_wildcard():
    flat = _house()
    assert resolve_selectors(["*"], flat) == resolve_selectors(["*/*"], flat)


def test_resolve_selectors_device_glob_crosses_dot_boundary():
    flat = _house()
    pairs = resolve_selectors(["house.*/power"], flat)
    assert pairs == [("house.desk_lamp", "power")]


def test_resolve_selectors_non_matching_pattern_yields_empty():
    flat = _house()
    assert resolve_selectors(["nonexistent/*"], flat) == []


def test_resolve_selectors_excludes_non_readable_endpoints():
    flat = _house(readable=False)
    pairs = resolve_selectors(["house.desk_lamp/*"], flat)
    assert ("house.desk_lamp", "power") not in pairs
    assert ("house.desk_lamp", "brightness") in pairs


def test_resolve_selectors_invalid_pattern_no_slash_raises():
    with pytest.raises(ConfigError):
        resolve_selectors(["bogus"], _house())


def test_resolve_selectors_invalid_pattern_multiple_slashes_raises():
    with pytest.raises(ConfigError):
        resolve_selectors(["a/b/c"], _house())
