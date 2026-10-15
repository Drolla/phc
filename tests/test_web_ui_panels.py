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
    assert panel.series_titles == ["Lamp"]
    assert panel.logdb_instance == "logdb.house"


def test_graph_panel_series_title_is_the_device_for_a_single_endpoint_device():
    """A y-axis label has no device heading above it, so the device -- not
    the endpoint's own name, nor "lamp/power" -- identifies the series."""
    lamp = VirtualDevice("lamp", endpoints=[
        Endpoint("power", writable=True, value_type="int", name="Switch")])
    flat = {"lamp": lamp}

    panel = GraphPanel(flat, id="p", logdb_instance="logdb.house", selectors=["lamp/*"])

    assert panel.series_titles == ["Lamp"]


def test_graph_panel_series_title_is_the_endpoint_on_a_multi_endpoint_device():
    """Unique endpoint labels need no device prefix."""
    hp = VirtualDevice("heat_pump", name="Heat Pump", endpoints=[
        Endpoint("supply", value_type="float"),
        Endpoint("return_", value_type="float"),
    ])
    flat = {"hp": hp}

    panel = GraphPanel(flat, id="p", logdb_instance="logdb.house", selectors=["hp/*"])

    assert panel.series_titles == ["Return", "Supply"]


def test_graph_panel_series_title_prefixes_the_device_on_a_collision():
    flat = {
        "liv": VirtualDevice("liv", name="Living Room", endpoints=[
            Endpoint("temp", name="Temperature"), Endpoint("hum", name="Humidity")]),
        "par": VirtualDevice("par", name="Parents' Room", endpoints=[
            Endpoint("temp", name="Temperature"), Endpoint("hum", name="Humidity")]),
    }

    panel = GraphPanel(flat, id="p", logdb_instance="logdb.house",
                       selectors=["liv/temp", "par/temp", "liv/hum"])

    titles = dict(zip(panel.labels, panel.series_titles, strict=True))
    assert titles == {
        "liv/temp": "Living Room Temperature",
        "par/temp": "Parents' Room Temperature",
        "liv/hum": "Humidity",
    }


def test_graph_panel_series_title_falls_back_to_the_key_within_one_device():
    """Same label twice on one device: the device prefix can't tell them
    apart, so the key replaces the label."""
    flat = {"light": VirtualDevice("light", name="2nd Floor Light", endpoints=[
        Endpoint("base", name="Switch"), Endpoint("sv1", name="Switch")])}

    panel = GraphPanel(flat, id="p", logdb_instance="logdb.house", selectors=["light/*"])

    assert panel.series_titles == ["2nd Floor Light Base", "2nd Floor Light Sv1"]


def test_graph_panel_series_title_uses_the_device_for_a_label_shared_on_it():
    """"Switch" can't say which of the device's switches it is, so with only
    one of them in the panel the device names the series."""
    flat = {"light": VirtualDevice("light", name="2nd Floor Light", endpoints=[
        Endpoint("base", name="Switch"), Endpoint("sv1", name="Switch")])}

    panel = GraphPanel(flat, id="p", logdb_instance="logdb.house", selectors=["light/sv1"])

    assert panel.series_titles == ["2nd Floor Light"]


def test_graph_panel_series_title_keeps_a_hand_written_device_name_verbatim():
    flat = {"cdf": VirtualDevice("cdf", name="Chaux-de-Fonds", endpoints=[Endpoint("t")])}

    panel = GraphPanel(flat, id="p", logdb_instance="logdb.house", selectors=["cdf/*"])

    assert panel.series_titles == ["Chaux-de-Fonds"]


def test_graph_panel_series_title_drops_words_shared_under_one_unit():
    flat = {"hp": VirtualDevice("hp", name="Heat Pump", endpoints=[
        Endpoint("supply", unit="°C", name="Supply Temperature"),
        Endpoint("dhw", unit="°C", name="Hot Water Temperature"),
    ])}

    panel = GraphPanel(flat, id="p", logdb_instance="logdb.house", selectors=["hp/*"])

    assert panel.series_titles == ["Hot Water", "Supply"]


def test_graph_panel_series_title_keeps_shared_words_across_units():
    flat = {"hp": VirtualDevice("hp", name="Heat Pump", endpoints=[
        Endpoint("supply", unit="°C", name="Supply Temperature"),
        Endpoint("power", unit="%", name="Compressor Temperature"),
    ])}

    panel = GraphPanel(flat, id="p", logdb_instance="logdb.house", selectors=["hp/*"])

    assert panel.series_titles == ["Compressor Temperature", "Supply Temperature"]


def test_graph_panel_series_title_keeps_shared_words_once_prefixed():
    """Two "Radon" series told apart only by device would shrink to the
    device names' one differing word, so a prefixed panel is left as is."""
    flat = {
        "office": VirtualDevice("office", name="Cellar Office", endpoints=[
            Endpoint("radon", unit="Bq/m3", name="Radon"), Endpoint("co2", unit="ppm")]),
        "room": VirtualDevice("room", name="Cellar Room", endpoints=[
            Endpoint("radon", unit="Bq/m3", name="Radon"), Endpoint("co2", unit="ppm")]),
    }

    panel = GraphPanel(flat, id="p", logdb_instance="logdb.house",
                       selectors=["office/radon", "room/radon"])

    assert panel.series_titles == ["Cellar Office Radon", "Cellar Room Radon"]


def test_graph_panel_series_title_humanizes_an_identifier():
    """Underscores become spaces; a digit-led word and an acronym survive."""
    flat = {"z": VirtualDevice("motion_1st_floor", endpoints=[Endpoint("state")])}

    panel = GraphPanel(flat, id="p", logdb_instance="logdb.house", selectors=["z/*"])

    assert panel.series_titles == ["Motion 1st Floor"]


def test_graph_panel_series_title_prefers_name_over_description():
    lamp = VirtualDevice("lamp", endpoints=[
        Endpoint("power", writable=True, value_type="int",
                 name="Corridor Light", description="Lamp power"),
        Endpoint("brightness", value_type="int"),
    ])
    flat = {"lamp": lamp}

    panel = GraphPanel(flat, id="p", logdb_instance="logdb.house", selectors=["lamp/power"])

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
        "series_titles": ["Lamp"],
        "series_kinds": ["numeric"],
        "binary_lanes": None,
    }
    # logdb_instance/pairs are internal wiring, not needed client-side --
    # see GraphPanel's own docstring.
    assert "logdb_instance" not in described
    assert "pairs" not in described


def _mixed_flat():
    """A device whose endpoints cover both series kinds, plus a labelled bool.

    Mirrors the real-world panel that motivated lanes (a power reading, an
    on/off flag and a ratio on one chart)."""
    return {"pump": VirtualDevice("pump", endpoints=[
        Endpoint("power", value_type="float", unit="W", name="Power"),
        Endpoint("active", value_type="bool", name="Active",
                 values={False: "off", True: "on"}),
        Endpoint("cop", value_type="float", name="COP"),
    ])}


def _graph(flat, **kwargs):
    kwargs.setdefault("selectors", ["pump/*"])
    return GraphPanel(flat, id="g", logdb_instance="logdb.house", **kwargs)


def test_graph_panel_infers_binary_kind_for_bool_endpoints_only():
    panel = _graph(_mixed_flat())

    # Parallel to series_titles/labels, in selector-resolution order
    # (resolve_selectors sorts by endpoint key: active, cop, power).
    assert panel.series_titles == ["Active", "COP", "Power"]
    assert panel.series_kinds == ["binary", "numeric", "numeric"]


def test_graph_panel_carries_per_series_units_and_value_labels():
    panel = _graph(_mixed_flat())

    assert panel.series_units == [None, None, "W"]
    # str()-keyed, matching widgets.describe_endpoint -- a Python True
    # serializes as "True", which the client has to match on.
    assert panel.series_values == [{"False": "off", "True": "on"}, None, None]


def test_graph_panel_carries_distinct_units_for_an_all_numeric_panel():
    """The environment_history shape: no bool endpoints at all, but
    temperature (C) and humidity (%) share one panel. graph.js uses this
    per-series unit (not just the single panel-wide `unit:`) to put them
    on separate axes -- see static/graph.js's assignUnitAxes."""
    flat = {"room": VirtualDevice("room", endpoints=[
        Endpoint("temp", value_type="float", unit="C", name="Temp"),
        Endpoint("hum", value_type="float", unit="%", name="Hum"),
    ])}
    panel = _graph(flat, selectors=["room/*"], unit="C")

    assert panel.series_kinds == ["numeric", "numeric"]
    assert panel.series_units == ["%", "C"]  # sorted by key: hum, temp


def test_graph_panel_binary_lanes_true_forces_every_series_into_a_lane():
    panel = _graph(_mixed_flat(), binary_lanes=True)

    assert panel.series_kinds == ["binary", "binary", "binary"]


def test_graph_panel_binary_lanes_false_restores_one_shared_axis():
    panel = _graph(_mixed_flat(), binary_lanes=False)

    assert panel.series_kinds == ["numeric", "numeric", "numeric"]
    assert panel.describe()["binary_lanes"] is False


def test_graph_panel_rejects_non_boolean_binary_lanes():
    with pytest.raises(ConfigError):
        _graph(_mixed_flat(), binary_lanes="yes")


def test_graph_panel_defaults_binary_to_endpoint_aggregation_and_numeric_to_avg():
    panel = _graph(_mixed_flat())

    # Keyed by label (what LogDb.get_decimated wants), not a parallel list.
    assert panel.series_aggregations == {
        "pump/active": "max",  # the bool endpoint's own `aggregation` default
        "pump/cop": "avg",
        "pump/power": "avg",
    }


def test_graph_panel_binary_series_follows_endpoint_aggregation_min():
    """The endpoint is the per-series control: declaring `aggregation: min`
    makes its lane decimate by min, with no panel-level setting."""
    flat = {"pump": VirtualDevice("pump", endpoints=[
        Endpoint("active", value_type="bool", aggregation="min"),
    ])}
    panel = _graph(flat)

    assert panel.series_aggregations == {"pump/active": "min"}


def test_graph_panel_binary_aggregation_avg_restores_averaging():
    panel = _graph(_mixed_flat(), binary_aggregation="avg")

    assert panel.series_aggregations["pump/active"] == "avg"


def test_graph_panel_binary_aggregation_leaves_numeric_series_averaged():
    """A panel-wide setting must not turn a power chart into a peak chart."""
    panel = _graph(_mixed_flat(), binary_aggregation="max")

    assert panel.series_aggregations["pump/cop"] == "avg"
    assert panel.series_aggregations["pump/power"] == "avg"


def test_graph_panel_binary_lanes_true_also_switches_int_series_to_max():
    """One override, one coherent story: forcing a level endpoint into a
    lane also gives it the lane-appropriate decimation rule."""
    flat = {"dim": VirtualDevice("dim", endpoints=[
        Endpoint("level", value_type="int", min=0, max=99),
    ])}
    panel = _graph(flat, selectors=["dim/*"], binary_lanes=True)

    assert panel.series_kinds == ["binary"]
    assert panel.series_aggregations == {"dim/level": "max"}


def test_graph_panel_rejects_invalid_binary_aggregation():
    with pytest.raises(ConfigError):
        _graph(_mixed_flat(), binary_aggregation="mean")


def test_graph_panel_does_not_retain_the_device_tree():
    """`flat` is the whole device tree; a panel holding it would pin it for
    the process's lifetime. Everything needed is extracted in __init__."""
    panel = _graph(_mixed_flat())

    assert not any(isinstance(value, dict) and "pump" in value
                   for value in vars(panel).values())


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
