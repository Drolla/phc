"""Tests for phc.py's command-line argument parsing."""

import pytest

from phc import _parse_log_level_module


def test_parse_log_level_module_splits_name_and_level():
    assert _parse_log_level_module("scheduler=DEBUG") == ("scheduler", "DEBUG")


def test_parse_log_level_module_rejects_missing_equals():
    with pytest.raises(Exception):
        _parse_log_level_module("scheduler")


def test_log_level_merge_order(tmp_path, monkeypatch):
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
log: { dest: stdout }
log_levels:
  default: INFO
  scheduler: INFO
devices: []
""")

    import logging
    from core.config import load_system

    # No CLI overrides: config file values apply as-is.
    load_system(system_yaml, log_levels_override={})
    assert logging.getLogger("phc").getEffectiveLevel() == logging.INFO
    assert logging.getLogger("phc.scheduler").getEffectiveLevel() == logging.INFO

    # CLI --log-level overrides default; --log-level-module overrides just that key.
    load_system(system_yaml, log_levels_override={"default": "ERROR", "scheduler": "DEBUG"})
    assert logging.getLogger("phc").getEffectiveLevel() == logging.ERROR
    assert logging.getLogger("phc.scheduler").getEffectiveLevel() == logging.DEBUG
