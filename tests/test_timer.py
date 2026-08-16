"""Tests for phc.extensions.timer.timer: TimerDef validation, TimerStore (in
isolation from the extension/scheduler-hook wiring layer), and
expired_one_shot()'s catch_up boundary."""

import logging

import pytest
import yaml

from phc.extensions.timer.timer import TimerDef, TimerStore, expired_one_shot


@pytest.fixture
def timer_log(caplog):
    """caplog, but attached directly to the "phc.timer" logger, bypassing
    propagate=False set by configure_logging() -- see
    tests/test_recovery.py's recovery_log fixture for the same pattern."""
    timer_logger = logging.getLogger("phc.timer")
    timer_logger.addHandler(caplog.handler)
    timer_logger.setLevel(logging.WARNING)
    try:
        with caplog.at_level("WARNING", logger="phc.timer"):
            yield caplog
    finally:
        timer_logger.removeHandler(caplog.handler)


def _timer(**overrides):
    fields = {
        "id": 1,
        "time": 1000.0,
        "device": "lamp",
        "endpoint": "power",
        "action": "set",
        "value": "on",
        "repeat": None,
        "description": "test timer",
        "enabled": True,
    }
    fields.update(overrides)
    return TimerDef(**fields)


def _path(tmp_path):
    return tmp_path / "timers.yaml"


# ---------- TimerDef ----------

def test_timer_def_rejects_invalid_action():
    with pytest.raises(ValueError):
        _timer(action="bogus")


def test_timer_def_set_action_requires_a_value():
    with pytest.raises(ValueError):
        _timer(action="set", value=None)


def test_timer_def_toggle_action_allows_no_value():
    t = _timer(action="toggle", value=None)
    assert t.value is None


def test_timer_def_tag_combines_instance_key_and_id():
    t = _timer(id=4)
    assert t.tag("timer.house") == "timer.house.4"


# ---------- expired_one_shot() ----------

def test_expired_one_shot_true_when_missed_by_more_than_catch_up():
    t = _timer(time=1000.0, repeat=None)
    assert expired_one_shot(t, now=1000.0 + 301.0, catch_up=300.0) is True


def test_expired_one_shot_false_when_within_catch_up():
    t = _timer(time=1000.0, repeat=None)
    assert expired_one_shot(t, now=1000.0 + 299.0, catch_up=300.0) is False


def test_expired_one_shot_false_exactly_at_catch_up_boundary():
    t = _timer(time=1000.0, repeat=None)
    assert expired_one_shot(t, now=1000.0 + 300.0, catch_up=300.0) is False


def test_expired_one_shot_false_when_still_in_the_future():
    t = _timer(time=1000.0, repeat=None)
    assert expired_one_shot(t, now=900.0, catch_up=300.0) is False


def test_expired_one_shot_always_false_for_a_repeating_timer():
    t = _timer(time=1000.0, repeat="1D")
    assert expired_one_shot(t, now=1000.0 + 999999.0, catch_up=300.0) is False


# ---------- TimerStore.load() ----------

def test_load_missing_file_returns_next_id_one_and_no_timers(tmp_path):
    assert TimerStore(_path(tmp_path)).load() == (1, [])


def test_load_empty_file_returns_next_id_one_and_no_timers(tmp_path):
    path = _path(tmp_path)
    path.write_text("")
    assert TimerStore(path).load() == (1, [])


def test_load_corrupt_yaml_returns_empty_and_logs_warning(tmp_path, timer_log):
    path = _path(tmp_path)
    path.write_text("a: [unterminated")
    assert TimerStore(path).load() == (1, [])
    assert str(path) in timer_log.text


def test_load_non_mapping_yaml_returns_empty_and_logs_warning(tmp_path, timer_log):
    path = _path(tmp_path)
    path.write_text("- 1\n- 2\n")
    assert TimerStore(path).load() == (1, [])
    assert str(path) in timer_log.text


def test_load_skips_malformed_timer_entry_and_logs_warning(tmp_path, timer_log):
    path = _path(tmp_path)
    TimerStore(path).write(3, [_timer(id=1), _timer(id=2)])
    # Directly corrupt one entry in the persisted file rather than
    # constructing an invalid TimerDef (its own __post_init__ would refuse
    # to exist in the first place) -- simulates a hand-edited/older-version
    # file with a bad "action" value.
    data = yaml.safe_load(path.read_text())
    data["timers"][1]["action"] = "bogus"
    path.write_text(yaml.safe_dump(data, sort_keys=False))

    next_id, timers = TimerStore(path).load()
    assert next_id == 3
    assert [t.id for t in timers] == [1]
    assert "malformed" in timer_log.text.lower()


# ---------- TimerStore write()/load() roundtrip ----------

def test_write_then_load_roundtrips_timers(tmp_path):
    path = _path(tmp_path)
    timers = [_timer(id=1), _timer(id=2, action="toggle", value=None, repeat="1D")]
    TimerStore(path).write(3, timers)

    next_id, loaded = TimerStore(path).load()

    assert next_id == 3
    assert loaded == timers


def test_write_overwrites_previous_content_not_merges(tmp_path):
    path = _path(tmp_path)
    store = TimerStore(path)
    store.write(2, [_timer(id=1)])
    store.write(5, [_timer(id=4)])

    next_id, timers = store.load()

    assert next_id == 5
    assert [t.id for t in timers] == [4]


def test_write_creates_parent_directories(tmp_path):
    path = tmp_path / "state" / "sub" / "timers.yaml"
    TimerStore(path).write(1, [])
    assert path.exists()


def test_write_leaves_no_tmp_file_behind(tmp_path):
    path = _path(tmp_path)
    TimerStore(path).write(1, [_timer()])
    assert not path.with_name(path.name + ".tmp").exists()


def test_write_is_atomic_failed_write_does_not_corrupt_existing_file(tmp_path, monkeypatch):
    path = _path(tmp_path)
    store = TimerStore(path)
    store.write(2, [_timer(id=1)])

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(yaml, "safe_dump", _boom)
    with pytest.raises(RuntimeError):
        store.write(3, [_timer(id=2)])

    next_id, timers = TimerStore(path).load()
    assert next_id == 2
    assert [t.id for t in timers] == [1]
