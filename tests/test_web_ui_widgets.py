"""Tests for extensions.web_ui.widgets: infer_widget_kind()'s pure
inference matrix, describe_endpoint()'s shape, and describe_device()'s
branch-pruning behavior."""

from core.endpoint import Endpoint
from devices.virtual.device import VirtualDevice
from extensions.web_ui.widgets import describe_device, describe_endpoint, infer_widget_kind


# ---------- infer_widget_kind ----------

def test_read_only_endpoint_is_always_a_label():
    ep = Endpoint("temp", writable=False, value_type="float", min=0, max=100, values={0: "off"})
    assert infer_widget_kind(ep) == "label"


def test_writable_bool_is_toggle():
    ep = Endpoint("state", writable=True, value_type="bool")
    assert infer_widget_kind(ep) == "toggle"


def test_writable_with_values_mapping_is_dropdown_regardless_of_type():
    ep = Endpoint("speed", writable=True, value_type="int", values={0: "off", 1: "low", 2: "high"})
    assert infer_widget_kind(ep) == "dropdown"

    untyped = Endpoint("mode", writable=True, values={"a": "Mode A"})
    assert infer_widget_kind(untyped) == "dropdown"


def test_writable_numeric_with_both_min_and_max_is_slider():
    ep = Endpoint("brightness", writable=True, value_type="int", min=0, max=100)
    assert infer_widget_kind(ep) == "slider"

    ep_float = Endpoint("setpoint", writable=True, value_type="float", min=10.0, max=30.0)
    assert infer_widget_kind(ep_float) == "slider"


def test_writable_numeric_with_only_one_or_neither_bound_is_number():
    assert infer_widget_kind(Endpoint("x", writable=True, value_type="int", min=0)) == "number"
    assert infer_widget_kind(Endpoint("x", writable=True, value_type="int", max=100)) == "number"
    assert infer_widget_kind(Endpoint("x", writable=True, value_type="int")) == "number"
    assert infer_widget_kind(Endpoint("x", writable=True, value_type="float")) == "number"


def test_writable_str_or_untyped_is_text():
    assert infer_widget_kind(Endpoint("name", writable=True, value_type="str")) == "text"
    assert infer_widget_kind(Endpoint("name", writable=True)) == "text"


# ---------- describe_endpoint ----------

def test_describe_endpoint_shape():
    ep = Endpoint("brightness", writable=True, value_type="int", min=0, max=100, unit="%",
                  description="Brightness")
    ep.set(42)
    ep.update_state()

    described = describe_endpoint("house.lamp", ep)

    assert described == {
        "device": "house.lamp",
        "endpoint": "brightness",
        "label": "Brightness",
        "widget": "slider",
        "value": 42,
        "text": "42 %",
        "readable": True,
        "writable": True,
        "value_type": "int",
        "unit": "%",
        "min": 0,
        "max": 100,
        "values": None,
        "update_time": ep.get_update_time(),
    }


def test_describe_endpoint_stringifies_values_keys():
    ep = Endpoint("speed", writable=True, value_type="int", values={0: "off", 1: "on"})
    described = describe_endpoint("fan", ep)
    assert described["values"] == {"0": "off", "1": "on"}


def test_describe_endpoint_label_defaults_to_key_when_no_description():
    ep = Endpoint("state")
    described = describe_endpoint("d", ep)
    assert described["label"] == "state"


# ---------- describe_device pruning ----------

def _lamp_with_children():
    child = VirtualDevice("lamp", endpoints=[Endpoint("state", writable=True, value_type="bool")],
                          parent_qualified_id="house")
    house = VirtualDevice("house", children=[child])
    return house, child


def test_describe_device_returns_none_when_nothing_visible():
    house, _lamp = _lamp_with_children()
    assert describe_device(house, visible_pairs=set()) is None


def test_describe_device_prunes_branches_without_visible_endpoints():
    unrelated_child = VirtualDevice("thermostat", endpoints=[Endpoint("temp", value_type="float")],
                                    parent_qualified_id="house")
    lamp = VirtualDevice("lamp", endpoints=[Endpoint("state", writable=True, value_type="bool")],
                         parent_qualified_id="house")
    house = VirtualDevice("house", children=[lamp, unrelated_child])

    described = describe_device(house, visible_pairs={("house.lamp", "state")})

    assert described is not None
    child_ids = {c["qualified_id"] for c in described["children"]}
    assert child_ids == {"house.lamp"}  # thermostat branch pruned entirely


def test_describe_device_keeps_intermediate_group_with_no_endpoints_of_its_own():
    lamp = VirtualDevice("lamp", endpoints=[Endpoint("state", writable=True, value_type="bool")],
                         parent_qualified_id="house")
    house = VirtualDevice("house", children=[lamp])  # "house" itself has no endpoints

    described = describe_device(house, visible_pairs={("house.lamp", "state")})

    assert described is not None
    assert described["endpoints"] == []
    assert len(described["children"]) == 1
    assert described["children"][0]["qualified_id"] == "house.lamp"


def test_describe_device_filters_endpoints_not_in_visible_pairs():
    lamp = VirtualDevice("lamp", endpoints=[
        Endpoint("state", writable=True, value_type="bool"),
        Endpoint("brightness", writable=True, value_type="int", min=0, max=100),
    ])
    described = describe_device(lamp, visible_pairs={("lamp", "state")})
    assert [e["endpoint"] for e in described["endpoints"]] == ["state"]
