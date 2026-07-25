"""Tests for extensions.web_ui.panels: the local (not core.registry) panel
kind registry, and DevicesPanel (the only shipped panel kind)."""

import pytest

from core.config import ConfigError
from core.endpoint import Endpoint
from devices.virtual.device import VirtualDevice
from extensions.web_ui.panels import DevicesPanel, get_panel_kind_class


def test_get_panel_kind_class_returns_devices_panel_by_default():
    assert get_panel_kind_class("devices") is DevicesPanel


def test_get_panel_kind_class_unknown_kind_raises_config_error():
    with pytest.raises(ConfigError):
        get_panel_kind_class("graph")  # not implemented -- see panels.py docstring


def test_devices_panel_resolves_selectors_and_describes():
    lamp = VirtualDevice("lamp", endpoints=[Endpoint("state", writable=True, value_type="bool")])
    flat = {"lamp": lamp}

    panel = DevicesPanel(["lamp/*"], flat)

    assert panel.kind == "devices"
    assert panel.pairs == [("lamp", "state")]
    assert panel.describe() == {"kind": "devices", "pairs": [("lamp", "state")]}
