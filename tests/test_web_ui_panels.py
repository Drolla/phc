"""Tests for phc.extensions.web_ui.panels: the local (not phc.core.registry) panel
kind registry, DevicesPanel, GraphPanel, and TimersPanel."""

import pytest

from phc.core.config import ConfigError
from phc.core.endpoint import Endpoint
from phc.devices.virtual.device import VirtualDevice
from phc.extensions.web_ui.panels import DevicesPanel, GraphPanel, TimersPanel, get_panel_kind_class


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


def test_get_panel_kind_class_returns_timers_panel():
    assert get_panel_kind_class("timers") is TimersPanel


def test_timers_panel_requires_non_empty_id():
    with pytest.raises(ConfigError):
        TimersPanel({}, id="", timer_instance="timer.house")


def test_timers_panel_defaults_title_to_id():
    panel = TimersPanel({}, id="house_timers", timer_instance="timer.house")
    assert panel.title == "house_timers"


def test_timers_panel_describe_shape_omits_internal_wiring():
    panel = TimersPanel({}, id="house_timers", timer_instance="timer.house", title="House Timers")

    described = panel.describe()

    assert described == {"kind": "timers", "id": "house_timers", "title": "House Timers"}
    # timer_instance is internal wiring, resolved lazily at request time --
    # see TimersPanel's own docstring.
    assert "timer_instance" not in described


# ---------- duplicate panel ids ----------

def test_duplicate_ids_are_rejected_for_any_panel_kind():
    """The check used to be written out once per addressable kind (graph,
    timers), so a new kind with an id had to remember to add a third. It
    now covers every kind that has an id -- verified with a panel kind
    that did not exist when the check was written."""
    from phc.extensions.web_ui import extension as web_ui_extension
    from phc.extensions.web_ui.panels import Panel, _panel_kinds, register_panel_kind

    @register_panel_kind("gauge")
    class GaugePanel(Panel):
        kind = "gauge"

        def __init__(self, flat, id):
            self.id = id

        def describe(self):
            return {"kind": self.kind, "id": self.id}

    try:
        section = web_ui_extension.Section(
            id="s", title="", collapsed=False,
            panels=[GaugePanel(flat={}, id="dupe"), GaugePanel(flat={}, id="dupe")])
        page = web_ui_extension.Page(id="p", title="", sections=[section])

        with pytest.raises(ConfigError) as excinfo:
            web_ui_extension._reject_duplicate_panel_ids([page], "web_ui.home")
        assert "gauge panel" in str(excinfo.value)
        assert "dupe" in str(excinfo.value)
    finally:
        _panel_kinds.pop("gauge", None)


def test_the_same_id_on_two_different_panel_kinds_is_allowed():
    """Each kind is indexed separately (GRAPH_PANELS_BY_ID vs
    TIMER_PANELS_BY_ID), so ids only have to be unique within a kind --
    requiring global uniqueness would reject a valid config."""
    from phc.extensions.web_ui import extension as web_ui_extension

    graph = GraphPanel(flat={}, id="house", logdb_instance="logdb.x", selectors=[])
    timers = TimersPanel(flat={}, id="house", timer_instance="timer.x")
    section = web_ui_extension.Section(id="s", title="", collapsed=False,
                                        panels=[graph, timers])
    page = web_ui_extension.Page(id="p", title="", sections=[section])

    web_ui_extension._reject_duplicate_panel_ids([page], "web_ui.home")   # must not raise
