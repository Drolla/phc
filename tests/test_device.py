"""Tests for phc.core.device: the Device base class (get/set, fetch, tree recursion)."""

import asyncio

import pytest

from phc.core.device import Device
from phc.core.endpoint import Endpoint
from tests.conftest import fetch_sync


class EchoDevice(Device):
    """Test double: transmit() stages directly into the buffer that
    receive() hands back on the next fetch(), like VirtualDevice."""

    def setup(self):
        self._pending = {}

    def receive(self):
        pending, self._pending = self._pending, {}
        return pending

    def transmit(self, state):
        self._pending.update(state)


class AsyncOnlyEchoDevice(Device):
    """Test double for a native-async device (e.g. phc.devices.zway.ZWayDevice):
    overrides transmit_async() directly and never implements the synchronous
    transmit() at all, per phc.core.device.Device's class docstring guidance for
    a device whose I/O is genuinely async-friendly."""

    def setup(self):
        self._pending = {}

    async def receive_async(self):
        pending, self._pending = self._pending, {}
        return pending

    async def transmit_async(self, state):
        self._pending.update(state)


def make_single_endpoint_device(key="state", writable=True, default=None):
    ep = Endpoint(key, writable=writable)
    device = EchoDevice(key + "_dev", endpoints=[ep])
    if default is not None:
        device.set(default)
        fetch_sync(device)
        device.update_state()
    return device


def test_single_endpoint_scalar_shortcut():
    device = make_single_endpoint_device()
    device.set("on")
    fetch_sync(device)
    device.update_state()
    assert device.get() == "on"
    assert device.event == "on"


def test_set_routes_through_transmit_not_direct_endpoint_write():
    ep = Endpoint("state", writable=True)
    device = EchoDevice("d", endpoints=[ep])
    device.set("on")
    # not observable yet: transmit() only staged it, fetch()/update_state() haven't run
    assert device.get() is None
    assert ep.get() is None
    fetch_sync(device)
    device.update_state()
    assert device.get() == "on"


def test_set_on_readonly_endpoint_raises():
    ep = Endpoint("temp", writable=False)
    device = EchoDevice("d", endpoints=[ep])
    with pytest.raises(AttributeError):
        device.set(21.0)


def test_multi_endpoint_dict_shape():
    power = Endpoint("power", writable=True)
    brightness = Endpoint("brightness", writable=True)
    device = EchoDevice("lamp", endpoints=[power, brightness])

    device.set({"power": "on", "brightness": 80})
    fetch_sync(device)
    device.update_state()

    assert device.get() == {"power": "on", "brightness": 80}


def test_get_set_by_name_targets_one_endpoint_only():
    power = Endpoint("power", writable=True)
    brightness = Endpoint("brightness", writable=True)
    device = EchoDevice("lamp", endpoints=[power, brightness])

    device.set(80, name="brightness")
    fetch_sync(device)
    device.update_state()

    assert device.get("brightness") == 80
    assert device.get("power") is None


def test_set_by_name_on_readonly_endpoint_raises():
    power = Endpoint("power", writable=True)
    temp = Endpoint("temp", writable=False)
    device = EchoDevice("lamp", endpoints=[power, temp])
    with pytest.raises(AttributeError):
        device.set(21.0, name="temp")


def test_children_aggregate_via_dict():
    child_a = make_single_endpoint_device("a")
    child_b = make_single_endpoint_device("b")
    parent = Device("house", children=[child_a, child_b])

    child_a.set("on")
    fetch_sync(child_a)
    child_a.update_state()

    result = parent.get()
    assert result["a_dev"] == "on"
    assert result["b_dev"] is None


def test_update_state_does_not_recurse_into_children():
    # phc.core.scheduler.Scheduler's commit pass visits every device directly
    # via its flat device dict, parent and child alike -- update_state()
    # recursing here too would double-commit a child (see the method's
    # docstring), so a parent's own call must leave its children untouched.
    child = make_single_endpoint_device("a")
    parent = Device("house", children=[child])

    child.set("on")
    fetch_sync(child)
    parent.update_state()  # only the parent's own (zero) endpoints commit

    assert child.get() is None
    assert child.event is None


def test_get_set_by_name_targets_one_child():
    child_a = make_single_endpoint_device("a")
    child_b = make_single_endpoint_device("b")
    parent = Device("house", children=[child_a, child_b])

    parent.set("on", name="a_dev")
    fetch_sync(child_a)
    child_a.update_state()

    assert parent.get("a_dev") == "on"
    assert parent.get("b_dev") is None


def test_qualified_id_nesting():
    grandchild = Device("kitchen_light", parent_qualified_id="house.kitchen")
    assert grandchild.qualified_id == "house.kitchen.kitchen_light"

    root = Device("house")
    assert root.qualified_id == "house"


def test_unknown_name_raises_keyerror():
    device = EchoDevice("d", endpoints=[Endpoint("state", writable=True)])
    with pytest.raises(KeyError):
        device.get("nonexistent")


def test_endpoint_accessor_returns_endpoint_object():
    ep = Endpoint("state", writable=True)
    device = EchoDevice("d", endpoints=[ep])
    assert device.endpoint("state") is ep


def test_child_accessor_returns_device_object():
    child = Device("child")
    parent = Device("parent", children=[child])
    assert parent.child("child") is child


def test_host_like_device_with_no_endpoints_never_due():
    device = Device("house")
    assert device.due(1_000_000.0) is False


def test_next_due_is_infinite_without_update_interval():
    device = Device("house")
    assert device.next_due() == float("inf")


def test_next_due_reflects_update_interval_and_mark_run():
    device = Device("d", update_interval=10.0)
    assert device.next_due() == float("-inf")
    device.mark_run(100.0)
    assert device.next_due() == 110.0


def test_update_time_aggregates_max_across_endpoints_and_children():
    child = make_single_endpoint_device("a")
    parent = Device("house", children=[child])
    assert parent.get_update_time() == 0.0

    child.set("on")
    fetch_sync(child)
    child.update_state()
    assert parent.get_update_time() == child.get_update_time()
    assert parent.get_update_time() > 0.0


# ---------- get_text / set_text ----------

def test_get_text_set_text_single_endpoint_shortcut():
    ep = Endpoint("state", writable=True, value_type="int", values={0: "off", 1: "on"})
    device = EchoDevice("light", endpoints=[ep])

    device.set_text("on")
    fetch_sync(device)
    device.update_state()

    assert device.get() == 1
    assert device.get_text() == "on"


def test_get_text_set_text_by_name():
    power = Endpoint("power", writable=True, value_type="int", values={0: "off", 1: "on"})
    brightness = Endpoint("brightness", writable=True, value_type="int", unit="%")
    device = EchoDevice("lamp", endpoints=[power, brightness])

    device.set_text("on", name="power")
    device.set_text("80", name="brightness")
    fetch_sync(device)
    device.update_state()

    assert device.get_text("power") == "on"
    assert device.get_text("brightness") == "80 %"
    assert device.get("brightness") == 80


def test_set_text_on_readonly_endpoint_raises():
    ep = Endpoint("temp", writable=False, value_type="float")
    device = EchoDevice("d", endpoints=[ep])
    with pytest.raises(AttributeError):
        device.set_text("21.0")


def test_get_text_recurses_into_children():
    child = make_single_endpoint_device("a")
    parent = Device("house", children=[child])

    child.set("on")
    fetch_sync(child)
    child.update_state()

    assert parent.get_text()["a_dev"] == "on"


# ---------- read_transform / write_transform ----------

def test_fetch_applies_read_transform_to_raw_value():
    ep = Endpoint("temp", value_type="float", read_transform="value - 1.5")
    device = EchoDevice("multi_2nd", endpoints=[ep])
    device._pending = {"temp": 20.0}   # simulates a raw hardware reading

    fetch_sync(device)
    device.update_state()

    assert device.get() == 18.5


def test_set_applies_write_transform_before_transmit():
    ep = Endpoint("temp", writable=True, value_type="float", write_transform="value + 1.5")
    device = EchoDevice("d", endpoints=[ep])

    device.set(18.5)
    fetch_sync(device)   # EchoDevice.receive() hands back what transmit() staged
    device.update_state()

    assert device.get() == 20.0   # transmit() saw the write-transformed (raw) value


def test_set_text_applies_write_transform_after_from_text():
    ep = Endpoint("temp", writable=True, value_type="float", write_transform="value + 1.5")
    device = EchoDevice("d", endpoints=[ep])

    device.set_text("18.5")
    fetch_sync(device)
    device.update_state()

    assert device.get() == 20.0


def test_read_and_write_transform_round_trip_through_fetch_and_set():
    ep = Endpoint("temp", writable=True, value_type="float",
                  read_transform="value - 1.5", write_transform="value + 1.5")
    device = EchoDevice("multi_2nd", endpoints=[ep])

    device.set(18.5)      # logical value written by a task/user
    fetch_sync(device)    # echoed back through transmit()/receive() as raw hardware state
    device.update_state()

    assert device.get() == 18.5   # write_transform then read_transform cancel out


# ---------- native-async devices (set_text/set vs. set_text_async/set_async) ----------

def test_set_text_is_silently_dropped_on_a_native_async_device():
    """Outside a tick, set_text()'s immediate-write path only ever calls the
    synchronous transmit() (see Device._emit()). A device that overrides
    transmit_async() directly instead -- like phc.devices.zway.ZWayDevice --
    never implements transmit(), so the write is silently swallowed. This
    pins down that known limitation (the reason phc.extensions.web_ui's write
    endpoint uses set_text_async() instead, see the next test)."""
    ep = Endpoint("state", writable=True, value_type="int", values={0: "off", 1: "on"})
    device = AsyncOnlyEchoDevice("light", endpoints=[ep])

    device.set_text("on")
    fetch_sync(device)
    device.update_state()

    assert device.get() is None


def test_set_text_async_reaches_a_native_async_device():
    ep = Endpoint("state", writable=True, value_type="int", values={0: "off", 1: "on"})
    device = AsyncOnlyEchoDevice("light", endpoints=[ep])

    asyncio.run(device.set_text_async("on"))
    fetch_sync(device)
    device.update_state()

    assert device.get() == 1
    assert device.get_text() == "on"


def test_set_text_async_by_name_and_multi_endpoint_dict():
    power = Endpoint("power", writable=True, value_type="int", values={0: "off", 1: "on"})
    brightness = Endpoint("brightness", writable=True, value_type="int", unit="%")
    device = AsyncOnlyEchoDevice("lamp", endpoints=[power, brightness])

    async def write_both():
        await device.set_text_async("on", name="power")
        await device.set_text_async("80", name="brightness")

    asyncio.run(write_both())
    fetch_sync(device)
    device.update_state()

    assert device.get_text("power") == "on"
    assert device.get("brightness") == 80


def test_set_text_async_applies_write_transform_after_from_text():
    ep = Endpoint("temp", writable=True, value_type="float", write_transform="value + 1.5")
    device = AsyncOnlyEchoDevice("d", endpoints=[ep])

    asyncio.run(device.set_text_async("18.5"))
    fetch_sync(device)
    device.update_state()

    assert device.get() == 20.0


def test_set_text_async_on_readonly_endpoint_raises():
    ep = Endpoint("temp", writable=False, value_type="float")
    device = AsyncOnlyEchoDevice("d", endpoints=[ep])
    with pytest.raises(AttributeError):
        asyncio.run(device.set_text_async("21.0"))
