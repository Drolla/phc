"""Tests for core.logging_setup: multi-destination logging and the in-place
status line."""

import io
import logging

import pytest

from core.logging_setup import (InPlaceLineHandler, NewlineSafeStreamHandler, _InPlaceLineState,
                                 _LevelMapFilter, configure_logging)


def test_in_place_record_writes_no_trailing_newline():
    stream = io.StringIO()
    state = _InPlaceLineState()
    handler = InPlaceLineHandler(stream, state)
    handler.setFormatter(logging.Formatter("%(message)s"))

    record = logging.LogRecord("phc.scheduler", logging.DEBUG, __file__, 0,
                                "3:blink 0:report", None, None)
    handler.emit(record)

    assert stream.getvalue() == "\r3:blink 0:report"
    assert state.is_open is True


def test_normal_handler_closes_pending_in_place_line_first():
    stream = io.StringIO()
    state = _InPlaceLineState()
    state.is_open = True
    handler = NewlineSafeStreamHandler(stream, state)
    handler.setFormatter(logging.Formatter("%(message)s"))

    record = logging.LogRecord("phc.scheduler", logging.INFO, __file__, 0,
                                "task blink executed", None, None)
    handler.emit(record)

    assert stream.getvalue() == "\ntask blink executed\n"
    assert state.is_open is False


def test_normal_handler_no_spurious_newline_when_nothing_pending():
    stream = io.StringIO()
    state = _InPlaceLineState()
    handler = NewlineSafeStreamHandler(stream, state)
    handler.setFormatter(logging.Formatter("%(message)s"))

    record = logging.LogRecord("phc.scheduler", logging.INFO, __file__, 0,
                                "task blink executed", None, None)
    handler.emit(record)

    assert stream.getvalue() == "task blink executed\n"


# ---------- _LevelMapFilter ----------

def _record(name, level=logging.INFO):
    return logging.LogRecord(name, level, __file__, 0, "msg", None, None)


def test_level_map_filter_default_applies_to_unlisted_logger():
    f = _LevelMapFilter({"default": "WARNING"}, logging.WARNING)
    assert f.filter(_record("phc.scheduler", logging.INFO)) is False
    assert f.filter(_record("phc.scheduler", logging.WARNING)) is True


def test_level_map_filter_per_name_override():
    f = _LevelMapFilter({"default": "INFO", "scheduler": "DEBUG"}, logging.INFO)
    assert f.filter(_record("phc.scheduler", logging.DEBUG)) is True
    assert f.filter(_record("phc.tasks", logging.DEBUG)) is False
    assert f.filter(_record("phc.tasks", logging.INFO)) is True


def test_level_map_filter_bare_phc_logger_matched_by_literal_key():
    f = _LevelMapFilter({"default": "INFO", "phc": "ERROR"}, logging.INFO)
    assert f.filter(_record("phc", logging.WARNING)) is False
    assert f.filter(_record("phc", logging.ERROR)) is True
    # A bare "phc" override doesn't leak onto "phc.scheduler" -- that's a
    # different (unlisted) name, so it still gets "default".
    assert f.filter(_record("phc.scheduler", logging.INFO)) is True


# ---------- configure_logging ----------

def test_configure_logging_default_level_applies_to_unset_loggers():
    configure_logging([{"dest": "stdout", "levels": {"default": "WARNING"}}])
    root = logging.getLogger("phc")
    assert root.getEffectiveLevel() == logging.WARNING
    filters = [f for h in root.handlers for f in h.filters if isinstance(f, _LevelMapFilter)]
    assert all(not f.filter(_record("phc.scheduler", logging.INFO)) for f in filters)
    assert all(f.filter(_record("phc.scheduler", logging.WARNING)) for f in filters)


def test_configure_logging_per_module_override():
    configure_logging([{"dest": "stdout", "levels": {"default": "INFO", "scheduler": "DEBUG"}}])
    root = logging.getLogger("phc")
    non_in_place = [h for h in root.handlers if isinstance(h, NewlineSafeStreamHandler)]
    assert len(non_in_place) == 1
    level_filter = next(f for f in non_in_place[0].filters if isinstance(f, _LevelMapFilter))
    assert level_filter.filter(_record("phc.scheduler", logging.DEBUG)) is True
    assert level_filter.filter(_record("phc.tasks", logging.DEBUG)) is False
    assert level_filter.filter(_record("phc.tasks", logging.INFO)) is True


def test_configure_logging_root_level_is_minimum_across_destinations():
    # The root logger itself must allow through the most verbose level any
    # destination wants, or a per-destination filter never even sees the
    # record -- filtering happens handler-side now, not via per-logger
    # levels (see _LevelMapFilter's docstring).
    configure_logging([{"dest": "stdout", "levels": {"default": "WARNING", "scheduler": "DEBUG"}}])
    root = logging.getLogger("phc")
    assert root.getEffectiveLevel() == logging.DEBUG
    assert all(h.level == logging.NOTSET for h in root.handlers)


def test_configure_logging_defaults_to_info_when_no_log_config_given():
    configure_logging(None)
    assert logging.getLogger("phc").getEffectiveLevel() == logging.INFO


def test_configure_logging_rejects_old_mapping_form():
    with pytest.raises(ValueError):
        configure_logging({"dest": "stdout"})


def test_configure_logging_rejects_duplicate_dest():
    with pytest.raises(ValueError):
        configure_logging([{"dest": "stdout"}, {"dest": "stdout", "levels": {"default": "DEBUG"}}])


def test_configure_logging_file_destination_writes_and_appends(tmp_path):
    configure_logging([{"dest": "app.log", "levels": {"default": "INFO"}}], config_dir=tmp_path)
    logging.getLogger("phc.tasks").info("first run")
    log_path = tmp_path / "app.log"
    assert log_path.exists()
    assert "first run" in log_path.read_text()

    # Re-configuring (as load_system() does on every call) must close the
    # previous FileHandler rather than leaking its fd, and a second run
    # must append rather than truncate.
    configure_logging([{"dest": "app.log", "levels": {"default": "INFO"}}], config_dir=tmp_path)
    logging.getLogger("phc.tasks").info("second run")
    text = log_path.read_text()
    assert "first run" in text and "second run" in text


def test_configure_logging_file_destination_never_gets_in_place_records(tmp_path):
    configure_logging([{"dest": "app.log", "levels": {"default": "DEBUG"}}], config_dir=tmp_path)
    logger = logging.getLogger("phc.scheduler")
    logger.info("3:blink 0:report", extra={"in_place": True})
    logger.info("task blink executed")
    text = (tmp_path / "app.log").read_text()
    assert "\r" not in text
    assert "task blink executed" in text


def test_configure_logging_cli_override_applies_to_stream_but_not_file_dest(tmp_path):
    configure_logging(
        [{"dest": "stdout", "levels": {"default": "INFO"}},
         {"dest": "warn.log", "levels": {"default": "WARNING"}}],
        log_levels_override={"default": "DEBUG"},
        config_dir=tmp_path,
    )
    root = logging.getLogger("phc")
    stream_handler = next(h for h in root.handlers if isinstance(h, NewlineSafeStreamHandler))
    file_handler = next(h for h in root.handlers if isinstance(h, logging.FileHandler))
    stream_filter = next(f for f in stream_handler.filters if isinstance(f, _LevelMapFilter))
    file_filter = next(f for f in file_handler.filters if isinstance(f, _LevelMapFilter))
    assert stream_filter.filter(_record("phc.tasks", logging.DEBUG)) is True
    assert file_filter.filter(_record("phc.tasks", logging.DEBUG)) is False
    assert file_filter.filter(_record("phc.tasks", logging.WARNING)) is True


def test_configure_logging_closes_previous_file_handler(tmp_path):
    # load_system() calls configure_logging() on every load; an unclosed
    # FileHandler would leak an open fd per call -- on Windows that also
    # keeps the file locked, breaking tmp_path cleanup in other tests.
    configure_logging([{"dest": "app.log"}], config_dir=tmp_path)
    logging.getLogger("phc.tasks").info("first")
    first_file_handler = next(h for h in logging.getLogger("phc").handlers
                              if isinstance(h, logging.FileHandler))

    configure_logging([{"dest": "app.log"}], config_dir=tmp_path)

    # FileHandler.close() sets .stream back to None once the underlying
    # file is actually closed.
    assert first_file_handler.stream is None
