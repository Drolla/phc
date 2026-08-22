"""Tests for phc.core.clock.Now: the two clock readings one tick is driven
with, carried as a single value."""

import dataclasses
import time

import pytest

from phc.core.clock import Now


def test_fields_keep_the_two_clocks_apart():
    now = Now(1000.0, 5.0)
    assert now.wall == 1000.0
    assert now.mono == 5.0


def test_at_gives_both_clocks_the_same_instant():
    """The single-number shorthand: one synthetic timeline, which is what
    lets a test drive ticks at explicit times."""
    assert Now.at(5.0) == Now(5.0, 5.0)


def test_coerce_passes_a_now_through_unchanged():
    now = Now(1.0, 2.0)
    assert Now.coerce(now) is now


@pytest.mark.parametrize("value", [3.0, 3])
def test_coerce_expands_a_bare_number(value):
    """int as well as float: coerce tests `isinstance(value, Now)`, not
    `isinstance(value, float)`, so an int is not silently passed through as
    if it were already a Now."""
    assert Now.coerce(value) == Now(3.0, 3.0)


def test_is_frozen():
    """A tick's timestamps are a fact about that tick -- every pass and hook
    it reaches must agree on them."""
    now = Now(1.0, 2.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        now.wall = 2.0


def test_capture_reads_each_clock_exactly_once(monkeypatch):
    """Not just tidiness: a caller faking time with a bare iterator (see
    tests/test_debug_portal_extension.py) raises StopIteration rather than
    failing cleanly if the number of reads changes, and the heartbeat grid
    depends on capture() not consuming a reading it does not use."""
    calls = []
    monkeypatch.setattr(time, "time", lambda: calls.append("wall") or 1000.0)
    monkeypatch.setattr(time, "monotonic", lambda: calls.append("mono") or 5.0)

    now = Now.capture()

    assert calls == ["wall", "mono"]
    assert now == Now(1000.0, 5.0)
