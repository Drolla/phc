"""Tests for extensions.web_ui.panels: the local (not core.registry) panel
kind registry, DevicesPanel, and GraphPanel."""

import pytest

from core.config import ConfigError
from core.endpoint import Endpoint
from devices.virtual.device import VirtualDevice
from extensions.web_ui.panels import DevicesPanel, GraphPanel, get_panel_kind_class


def test_get_panel_kind_class_returns_devices_panel_by_default():
    assert get_panel_kind_class("devices") is DevicesPanel


def test_get_panel_kind_class_unknown_kind_raises_config_error():
    with pytest.raises(ConfigError):
        get_panel_kind_class("bogus")


def test_devices_panel_resolves_selectors_and_describes():
    lamp = VirtualDevice("lamp", endpoints=[Endpoint("state", writable=True, value_type="bool")])
    flat = {"lamp": lamp}

    panel = DevicesPanel(["lamp/*"], flat)

    assert panel.kind == "devices"
    assert panel.pairs == [("lamp", "state")]
    assert panel.describe() == {"kind": "devices", "pairs": [("lamp", "state")]}


def test_get_panel_kind_class_returns_graph_panel():
    assert get_panel_kind_class("graph") is GraphPanel


def test_graph_panel_resolves_selectors_and_builds_labels():
    lamp = VirtualDevice("lamp", endpoints=[
        Endpoint("power", writable=True, value_type="int", description="Lamp power"),
    ])
    flat = {"lamp": lamp}

    panel = GraphPanel(flat, id="lamp_history", logdb_instance="logdb.house",
                        selectors=["lamp/*"])

    assert panel.kind == "graph"
    assert panel.pairs == [("lamp", "power")]
    assert panel.labels == ["lamp/power"]
    assert panel.series_titles == ["Lamp power"]
    assert panel.logdb_instance == "logdb.house"


def test_graph_panel_series_title_falls_back_to_qualified_label():
    lamp = VirtualDevice("lamp", endpoints=[Endpoint("power", writable=True, value_type="int")])
    flat = {"lamp": lamp}

    panel = GraphPanel(flat, id="p", logdb_instance="logdb.house", selectors=["lamp/*"])

    assert panel.series_titles == ["lamp/power"]


def test_graph_panel_series_title_prefers_name_over_description():
    lamp = VirtualDevice("lamp", endpoints=[
        Endpoint("power", writable=True, value_type="int",
                 name="Corridor Light", description="Lamp power"),
    ])
    flat = {"lamp": lamp}

    panel = GraphPanel(flat, id="p", logdb_instance="logdb.house", selectors=["lamp/*"])

    assert panel.series_titles == ["Corridor Light"]


def test_graph_panel_requires_non_empty_id():
    flat = {}
    with pytest.raises(ConfigError):
        GraphPanel(flat, id="", logdb_instance="logdb.house", selectors=[])


def test_graph_panel_defaults():
    flat = {}
    panel = GraphPanel(flat, id="p", logdb_instance="logdb.house", selectors=[])

    assert panel.title == "p"  # defaults to id
    assert panel.unit is None
    assert panel.window == 24 * 3600  # "24h" parsed to seconds
    assert panel.decimation == []


def test_graph_panel_parses_decimation_tiers():
    flat = {}
    panel = GraphPanel(flat, id="p", logdb_instance="logdb.house", selectors=[],
                        decimation=[{"older_than": "25h", "factor": 3},
                                    {"older_than": "8D", "factor": 8}])

    assert panel.decimation == [(25 * 3600, 3), (8 * 24 * 3600, 8)]


def test_graph_panel_rejects_malformed_decimation_entry():
    flat = {}
    with pytest.raises(ConfigError):
        GraphPanel(flat, id="p", logdb_instance="logdb.house", selectors=[],
                   decimation=[{"older_than": "25h"}])  # missing 'factor'


def test_graph_panel_rejects_non_positive_decimation_factor():
    flat = {}
    with pytest.raises(ConfigError):
        GraphPanel(flat, id="p", logdb_instance="logdb.house", selectors=[],
                   decimation=[{"older_than": "25h", "factor": 0}])


def test_graph_panel_describe_shape_omits_internal_wiring():
    lamp = VirtualDevice("lamp", endpoints=[
        Endpoint("power", writable=True, value_type="int", description="Lamp power"),
    ])
    flat = {"lamp": lamp}
    panel = GraphPanel(flat, id="lamp_history", logdb_instance="logdb.house",
                        selectors=["lamp/*"], title="Lamp", unit="%", window="1h")

    described = panel.describe()

    assert described == {
        "kind": "graph",
        "id": "lamp_history",
        "title": "Lamp",
        "unit": "%",
        "window": 3600,
        "series_titles": ["Lamp power"],
    }
    # logdb_instance/pairs are internal wiring, not needed client-side --
    # see GraphPanel's own docstring.
    assert "logdb_instance" not in described
    assert "pairs" not in described
