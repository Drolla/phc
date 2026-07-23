"""Tests for core.logging_setup: the "phc" logger tree and in-place status line."""

import io
import logging

from core.logging_setup import InPlaceLineHandler, NewlineSafeStreamHandler, _InPlaceLineState, configure_logging


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


def test_configure_logging_default_level_applies_to_unset_loggers():
    configure_logging({"dest": "stdout"}, {"default": "WARNING"})
    assert logging.getLogger("phc").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("phc.scheduler").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("phc.tasks").getEffectiveLevel() == logging.WARNING


def test_configure_logging_per_module_override():
    configure_logging({"dest": "stdout"}, {"default": "INFO", "scheduler": "DEBUG"})
    assert logging.getLogger("phc.scheduler").getEffectiveLevel() == logging.DEBUG
    assert logging.getLogger("phc.tasks").getEffectiveLevel() == logging.INFO


def test_configure_logging_handler_allows_most_verbose_override_through():
    configure_logging({"dest": "stdout"}, {"default": "WARNING", "scheduler": "DEBUG"})
    root = logging.getLogger("phc")
    assert all(h.level <= logging.DEBUG for h in root.handlers)


def test_configure_logging_defaults_to_info_when_no_log_levels_given():
    configure_logging({"dest": "stdout"})
    assert logging.getLogger("phc").getEffectiveLevel() == logging.INFO
