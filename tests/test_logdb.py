"""Tests for extensions.logdb.logdb: the CSV-backed, in-memory-buffered
LogDb storage engine (in isolation from the extension/task-wiring layer)."""

import logging
import math

import pytest

from extensions.logdb.logdb import LogDb


def _csv_path(tmp_path):
    return tmp_path / "log.csv"


@pytest.fixture
def logdb_log(caplog):
    """caplog, but attached directly to the "phc.logdb" logger, bypassing
    propagate=False set by configure_logging() (invoked via load_system()
    in other tests in this session) -- see tests/test_task.py's task_log
    fixture for the same pattern."""
    logdb_logger = logging.getLogger("phc.logdb")
    logdb_logger.addHandler(caplog.handler)
    logdb_logger.setLevel(logging.WARNING)
    try:
        with caplog.at_level("WARNING", logger="phc.logdb"):
            yield caplog
    finally:
        logdb_logger.removeHandler(caplog.handler)


# ---------- construction validation ----------

def test_duplicate_labels_raise():
    with pytest.raises(ValueError):
        LogDb("unused.csv", ["a", "a"])


def test_comma_in_label_raises():
    with pytest.raises(ValueError):
        LogDb("unused.csv", ["a,b"])


# ---------- basic log() / CSV content ----------

def test_first_row_is_always_full(tmp_path):
    path = _csv_path(tmp_path)
    db = LogDb(path, ["a", "b"], full_vector_interval=100)
    db.log(1.0, {"a": 1.0, "b": 2.0})
    db.close()
    lines = path.read_text().splitlines()
    assert lines[2] == "F,1.0,1.0,2.0"


def test_unchanged_value_is_blank_in_delta_row(tmp_path):
    path = _csv_path(tmp_path)
    db = LogDb(path, ["a", "b"], full_vector_interval=100)
    db.log(1.0, {"a": 1.0, "b": 2.0})
    db.log(2.0, {"a": 1.0, "b": 3.0})
    db.close()
    lines = path.read_text().splitlines()
    assert lines[3] == "D,2.0,,3.0"


def test_missing_value_written_as_nan_transition(tmp_path):
    path = _csv_path(tmp_path)
    db = LogDb(path, ["a", "b"], full_vector_interval=100)
    db.log(1.0, {"a": 1.0, "b": 2.0})
    db.log(2.0, {"a": 1.0})  # b missing this sample
    db.close()
    lines = path.read_text().splitlines()
    assert lines[3] == "D,2.0,,nan"


def test_still_missing_value_stays_blank(tmp_path):
    path = _csv_path(tmp_path)
    db = LogDb(path, ["a", "b"], full_vector_interval=100)
    db.log(1.0, {"a": 1.0})
    db.log(2.0, {"a": 1.0})
    db.close()
    lines = path.read_text().splitlines()
    assert lines[2] == "F,1.0,1.0,nan"
    assert lines[3] == "D,2.0,,"


def test_full_vector_row_recurs_every_interval(tmp_path):
    path = _csv_path(tmp_path)
    db = LogDb(path, ["a"], full_vector_interval=3)
    for i in range(6):
        db.log(float(i), {"a": float(i)})
    db.close()
    markers = [line.split(",")[0] for line in path.read_text().splitlines()[2:]]
    assert markers == ["F", "D", "D", "F", "D", "D"]


def test_unknown_label_is_ignored_with_warning(tmp_path, logdb_log):
    path = _csv_path(tmp_path)
    db = LogDb(path, ["a"], full_vector_interval=100)
    db.log(1.0, {"a": 1.0, "bogus": 5.0})
    db.close()
    assert "bogus" in logdb_log.text
    assert db.get_at(1.0) == {"time": 1.0, "a": 1.0}


# ---------- retention trim ----------

def test_max_records_trims_oldest(tmp_path):
    path = _csv_path(tmp_path)
    db = LogDb(path, ["a"], max_records=3)
    for i in range(10):
        db.log(float(i), {"a": float(i)})
    assert len(db) <= 3 * 1.1
    assert db.get_range(0, 100)[0]["time"] >= 6.0
    db.close()


def test_max_age_trims_older_than_window(tmp_path):
    path = _csv_path(tmp_path)
    db = LogDb(path, ["a"], max_age=10.0)
    for i in range(30):
        db.log(float(i), {"a": float(i)})
    kept_times = [row["time"] for row in db.get_range(0, 100)]
    assert min(kept_times) >= 29.0 - 10.0 * 1.1
    db.close()


# ---------- query API ----------

def test_get_at_empty_store_returns_none(tmp_path):
    db = LogDb(_csv_path(tmp_path), ["a"])
    assert db.get_at(1.0) is None
    db.close()


def test_get_at_returns_nearest_record(tmp_path):
    db = LogDb(_csv_path(tmp_path), ["a"], full_vector_interval=100)
    db.log(1.0, {"a": 1.0})
    db.log(5.0, {"a": 5.0})
    assert db.get_at(2.0) == {"time": 1.0, "a": 1.0}
    assert db.get_at(4.0) == {"time": 5.0, "a": 5.0}
    db.close()


def test_get_range_restricts_to_requested_labels(tmp_path):
    db = LogDb(_csv_path(tmp_path), ["a", "b"], full_vector_interval=100)
    db.log(1.0, {"a": 1.0, "b": 2.0})
    rows = db.get_range(0, 10, labels=["a"])
    assert rows == [{"time": 1.0, "a": 1.0}]
    db.close()


# ---------- restore ----------

def test_restore_round_trip(tmp_path):
    path = _csv_path(tmp_path)
    db = LogDb(path, ["a", "b"], full_vector_interval=3)
    for i in range(7):
        db.log(float(i), {"a": float(i), "b": float(i) * 2})
    db.close()

    db2 = LogDb(path, ["a", "b"], full_vector_interval=3)
    assert len(db2) == 7
    rows = db2.get_range(0, 100)
    assert [r["time"] for r in rows] == [float(i) for i in range(7)]
    assert [r["a"] for r in rows] == [float(i) for i in range(7)]
    assert [r["b"] for r in rows] == [float(i) * 2 for i in range(7)]
    db2.close()


def test_restore_honors_max_records(tmp_path):
    path = _csv_path(tmp_path)
    db = LogDb(path, ["a"], full_vector_interval=5, max_records=1000)
    for i in range(50):
        db.log(float(i), {"a": float(i)})
    db.close()

    db2 = LogDb(path, ["a"], full_vector_interval=5, max_records=10)
    rows = db2.get_range(0, 1000)
    assert len(rows) <= 10 * 1.1
    assert rows[-1]["time"] == 49.0
    db2.close()


def test_restore_across_forced_multi_chunk_backward_read(tmp_path, monkeypatch):
    import extensions.logdb.logdb as logdb_module
    monkeypatch.setattr(logdb_module, "_INITIAL_CHUNK_SIZE", 64)

    path = _csv_path(tmp_path)
    db = LogDb(path, ["a"], full_vector_interval=10, max_records=25)
    for i in range(100):
        db.log(float(i), {"a": float(i)})
    db.close()

    db2 = LogDb(path, ["a"], full_vector_interval=10, max_records=25)
    rows = db2.get_range(0, 1000)
    assert len(rows) <= 25 * 1.1
    assert rows[-1]["time"] == 99.0
    assert rows[-1]["a"] == 99.0
    db2.close()


def test_restore_carries_forward_unchanged_values_across_delta_rows(tmp_path):
    path = _csv_path(tmp_path)
    db = LogDb(path, ["a", "b"], full_vector_interval=100)
    db.log(1.0, {"a": 1.0, "b": 2.0})
    db.log(2.0, {"a": 1.0, "b": 3.0})  # a unchanged
    db.log(3.0, {"a": 1.0, "b": 3.0})  # both unchanged
    db.close()

    db2 = LogDb(path, ["a", "b"], full_vector_interval=100)
    rows = db2.get_range(0, 100)
    assert [r["a"] for r in rows] == [1.0, 1.0, 1.0]
    assert [r["b"] for r in rows] == [2.0, 3.0, 3.0]
    db2.close()


def test_restore_with_no_data_rows_yields_empty_store(tmp_path):
    path = _csv_path(tmp_path)
    db = LogDb(path, ["a"])
    db.close()

    db2 = LogDb(path, ["a"])
    assert len(db2) == 0
    db2.close()


# ---------- header growth ----------

def test_header_growth_fits_in_reserved_padding(tmp_path):
    path = _csv_path(tmp_path)
    db = LogDb(path, ["a"], full_vector_interval=100)
    db.log(1.0, {"a": 1.0})
    db.log(2.0, {"a": 2.0})
    db.close()

    db2 = LogDb(path, ["a", "b"], full_vector_interval=100)
    db2.log(3.0, {"a": 3.0, "b": 9.0})
    db2.close()

    db3 = LogDb(path, ["a", "b"], full_vector_interval=100)
    assert db3.labels == ["a", "b"]
    rows = db3.get_range(0, 100)
    assert rows[0]["time"] == 1.0 and rows[0]["a"] == 1.0 and math.isnan(rows[0]["b"])
    assert rows[-1] == {"time": 3.0, "a": 3.0, "b": 9.0}
    db3.close()


def test_header_growth_padding_exhausted_sacrifices_leading_records(tmp_path, logdb_log):
    path = _csv_path(tmp_path)
    db = LogDb(path, ["a"], full_vector_interval=100, header_reserve_bytes=20)
    for i in range(5):
        db.log(float(i), {"a": float(i)})
    db.close()

    long_label = "a_very_long_label_name_that_will_not_fit_in_the_reserved_padding_space"
    db2 = LogDb(path, ["a", long_label], full_vector_interval=100, header_reserve_bytes=20)
    assert "sacrificed" in logdb_log.text
    assert db2.labels == ["a", long_label]
    # every original record was consumed to make room -- the file had no
    # data left over big enough to survive as its own valid (F-anchored) row
    assert len(db2) == 0
    db2.close()

    # header itself is intact and further writes work normally
    db2b = LogDb(path, ["a", long_label], full_vector_interval=100, header_reserve_bytes=20)
    db2b.log(10.0, {"a": 10.0, long_label: 1.0})
    db2b.close()
    db3 = LogDb(path, ["a", long_label], full_vector_interval=100, header_reserve_bytes=20)
    assert db3.get_range(0, 100) == [{"time": 10.0, "a": 10.0, long_label: 1.0}]
    db3.close()


def test_unrecognized_header_rotates_and_starts_fresh(tmp_path):
    path = _csv_path(tmp_path)
    path.write_text("not,a,valid,logdb,header\ngarbage\n")
    db = LogDb(path, ["a"])
    db.close()
    assert len(db) == 0
    backups = list(tmp_path.glob("log.csv.*.bak"))
    assert len(backups) == 1
