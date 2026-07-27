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
log:
  - dest: stdout
    levels:
      default: INFO
      scheduler: INFO
devices: []
""")

    import logging

    from core.config import load_system
    from core.logging_setup import _LevelMapFilter

    def _resolved(name, level=logging.INFO):
        root = logging.getLogger("phc")
        record = logging.LogRecord(name, level, __file__, 0, "msg", None, None)
        return all(f.filter(record) for h in root.handlers for f in h.filters
                   if isinstance(f, _LevelMapFilter))

    # No CLI overrides: config file values apply as-is.
    load_system(system_yaml, log_levels_override={})
    assert logging.getLogger("phc").getEffectiveLevel() == logging.INFO
    assert _resolved("phc.scheduler", logging.INFO) is True
    assert _resolved("phc.scheduler", logging.DEBUG) is False

    # CLI --log-level overrides default; --log-level-module overrides just that key.
    load_system(system_yaml, log_levels_override={"default": "ERROR", "scheduler": "DEBUG"})
    assert logging.getLogger("phc").getEffectiveLevel() == logging.DEBUG
    assert _resolved("phc.tasks", logging.WARNING) is False
    assert _resolved("phc.tasks", logging.ERROR) is True
    assert _resolved("phc.scheduler", logging.DEBUG) is True
