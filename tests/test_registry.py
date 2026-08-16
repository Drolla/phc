"""Tests for phc.core.registry: the plugin registry and discover_*() functions."""

from phc.core.registry import discover_extensions, get_task_kind_class


def test_discover_extensions_runs_with_no_extension_packages():
    # At minimum, phc/extensions/ itself (with no subpackages yet, or ones with
    # no extension.py) must not make this raise.
    discover_extensions()


def test_discover_extensions_is_idempotent():
    discover_extensions()
    discover_extensions()


def test_discover_extensions_populates_log_db_task_kind():
    discover_extensions()
    assert get_task_kind_class("log_db").__name__ == "LogDbAction"
