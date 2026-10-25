"""Tests for phc.core.memory: the startup RSS breakdown on "phc.memory"."""

import logging
import types

import pytest

from phc.core import memory
from phc.core.config import load_system


@pytest.fixture
def memory_log(caplog):
    """caplog attached directly to "phc.memory" at DEBUG.

    Bypasses propagate=False set by configure_logging() -- see
    tests/test_task.py's task_log fixture for the same pattern. Not
    propagating itself, so a record isn't also caught by caplog's root
    handler when no earlier test has configured logging."""
    memory_logger = logging.getLogger("phc.memory")
    memory_logger.addHandler(caplog.handler)
    memory_logger.propagate = False
    try:
        with caplog.at_level("DEBUG", logger="phc.memory"):
            yield caplog
    finally:
        memory_logger.propagate = True
        memory_logger.removeHandler(caplog.handler)


def _fake_rss(monkeypatch, *values):
    """Make memory_info() return each of `values` (bytes) in turn."""
    it = iter(values)
    monkeypatch.setattr(memory, "_process",
                        types.SimpleNamespace(memory_info=lambda: types.SimpleNamespace(rss=next(it))))


def test_log_rss_reports_total_and_growth_since_previous(memory_log, monkeypatch):
    monkeypatch.setattr(memory, "_last", 10_000_000)
    _fake_rss(monkeypatch, 12_500_000, 12_000_000)
    memory.log_rss("step one")
    memory.log_rss("step two")

    assert memory_log.messages == [" 12.5 MB  +2.5 MB  step one",
                                   " 12.0 MB  -0.5 MB  step two"]


def test_log_rss_skips_measuring_when_debug_disabled(caplog, monkeypatch):
    _fake_rss(monkeypatch)   # memory_info() would raise StopIteration
    with caplog.at_level("INFO", logger="phc.memory"):
        memory.log_rss("ignored")
    assert caplog.messages == []


def test_load_system_reports_each_phase(memory_log, tmp_path):
    config = tmp_path / "system.yaml"
    config.write_text(
        "devices:\n"
        "  - { id: lamp, module: virtual, endpoints: [{ key: power, type: bool }] }\n"
        "extensions:\n"
        "  debug_portal:\n"
        "    debug: { port: 8099 }\n",
        encoding="utf-8")
    load_system(config, log_levels_override={"memory": "DEBUG"})

    labels = [message.split(" MB ")[-1].strip() for message in memory_log.messages]
    assert labels[:2] == ["process start", "PHC core loaded"]
    assert labels[-3:] == ["devices built", "extension debug_portal.debug", "system loaded"]
