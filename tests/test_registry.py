"""Tests for core.registry: the plugin registry and discover_*() functions."""

from core.registry import discover_extensions


def test_discover_extensions_runs_with_no_extension_packages():
    # At minimum, extensions/ itself (with no subpackages yet, or ones with
    # no extension.py) must not make this raise.
    discover_extensions()


def test_discover_extensions_is_idempotent():
    discover_extensions()
    discover_extensions()
