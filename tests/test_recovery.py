"""Tests for phc.extensions.recovery.recovery: the YAML-backed RecoveryStore
(in isolation from the extension/scheduler-hook wiring layer)."""

import logging

import pytest
import yaml

from phc.extensions.recovery.recovery import RecoveryStore


@pytest.fixture
def recovery_log(caplog):
    """caplog, but attached directly to the "phc.recovery" logger,
    bypassing propagate=False set by configure_logging() (invoked via
    load_system() in other tests in this session) -- see
    tests/test_logdb.py's logdb_log fixture for the same pattern."""
    recovery_logger = logging.getLogger("phc.recovery")
    recovery_logger.addHandler(caplog.handler)
    recovery_logger.setLevel(logging.WARNING)
    try:
        with caplog.at_level("WARNING", logger="phc.recovery"):
            yield caplog
    finally:
        recovery_logger.removeHandler(caplog.handler)


def _path(tmp_path):
    return tmp_path / "recovery.yaml"


# ---------- load() ----------

def test_load_missing_file_returns_empty_dict(tmp_path):
    store = RecoveryStore(_path(tmp_path))
    assert store.load() == {}


def test_load_empty_file_returns_empty_dict(tmp_path):
    path = _path(tmp_path)
    path.write_text("")
    assert RecoveryStore(path).load() == {}


def test_load_corrupt_yaml_returns_empty_dict_and_logs_warning(tmp_path, recovery_log):
    path = _path(tmp_path)
    path.write_text("a: [unterminated")
    assert RecoveryStore(path).load() == {}
    assert str(path) in recovery_log.text


def test_load_non_mapping_yaml_returns_empty_dict_and_logs_warning(tmp_path, recovery_log):
    path = _path(tmp_path)
    path.write_text("- 1\n- 2\n- 3\n")
    assert RecoveryStore(path).load() == {}
    assert str(path) in recovery_log.text


# ---------- write() / load() roundtrip ----------

def test_write_then_load_roundtrips_values(tmp_path):
    path = _path(tmp_path)
    RecoveryStore(path).write({"a.b/c": 1, "a.b/d": "on"})
    assert RecoveryStore(path).load() == {"a.b/c": 1, "a.b/d": "on"}


def test_write_overwrites_previous_content_not_merges(tmp_path):
    path = _path(tmp_path)
    store = RecoveryStore(path)
    store.write({"x": 1})
    store.write({"y": 2})
    assert store.load() == {"y": 2}


def test_write_creates_parent_directories(tmp_path):
    path = tmp_path / "state" / "sub" / "recovery.yaml"
    RecoveryStore(path).write({})
    assert path.exists()


def test_write_leaves_no_tmp_file_behind(tmp_path):
    path = _path(tmp_path)
    RecoveryStore(path).write({"a": 1})
    assert not path.with_name(path.name + ".tmp").exists()


def test_write_is_atomic_failed_write_does_not_corrupt_existing_file(tmp_path, monkeypatch):
    path = _path(tmp_path)
    store = RecoveryStore(path)
    store.write({"a": 1})

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(yaml, "safe_dump", _boom)
    with pytest.raises(RuntimeError):
        store.write({"a": 2})

    # A fresh RecoveryStore, to prove it's the FILE that's intact, not
    # some in-memory state on the original instance.
    assert RecoveryStore(path).load() == {"a": 1}
