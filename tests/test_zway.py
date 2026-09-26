"""ZWayDevice over the ZAutomation WebSocket API.

Drives the real protocol path against a fake in-process controller: the
encapsulated request/response framing, the push events that keep state
current between polls, pattern resolution, value translation and the
reconnect ladder.

Everything runs through Scheduler.tick(), which only turns the event
loop while a tick is in flight -- so these tests also pin down that the
module never depends on its reader task having run in between."""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from phc.core.endpoint import Endpoint
from phc.core.scheduler import Scheduler
from phc.devices.zway import zway_ws as conn_mod
from phc.devices.zway.device import ZWayDevice
from phc.devices.zway.zway_ws import ZWayConnection, ZWayError, command_for, to_phc

LEVEL_EVENT = "me.z-wave.devices.level"

# The per-system context zway devices share (phc.core.device.Device.context),
# one System's worth of connections. A fresh one per test is the same
# isolation load_system() gives each System -- and it matters here because a
# connection holds asyncio primitives bound to one loop, and every test builds
# its own Scheduler and therefore its own loop.
_context: dict = {}


@pytest.fixture(autouse=True)
def _fresh_context():
    _context.clear()
    yield
    _context.clear()


def _device(**metrics):
    """One controller device object, as the real API reports it."""
    return {
        "id": metrics.pop("id"),
        "deviceType": metrics.pop("deviceType", "switchBinary"),
        "metrics": {"title": metrics.pop("title", ""), **metrics},
    }


def default_devices():
    """A fresh set of controller devices.

    Built per call: a test that pushes a level mutates these objects, so
    sharing one list would leak state into the next test."""
    return [
        _device(id="ZWayVDev_zway_20-1-37", title="Rez: Light corridor", level="off"),
        _device(id="ZWayVDev_zway_11-0-49-1", title="Rez: Living temp",
                deviceType="sensorMultilevel", level=19.4, scaleTitle="°C"),
        _device(id="DummyDevice_17", title="Dummy Device 2",
                deviceType="switchMultilevel", level=55),
    ]


class FakeController:
    """A stand-in for the controller's ZAutomation WebSocket endpoint.

    Answers encapsulated requests and can push level events, so a test
    can exercise both directions of the protocol."""

    def __init__(self, devices=None, *, answer=True):
        self.devices = {d["id"]: d for d in (devices or default_devices())}
        # False reproduces a bad token: the handshake succeeds and then
        # nothing is ever answered.
        self.answer = answer
        self.requests: list[str] = []
        self.sockets: list[web.WebSocketResponse] = []
        self.server: TestServer | None = None

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/", self._handle)
        self.server = TestServer(app)
        await self.server.start_server()
        return f"ws://127.0.0.1:{self.server.port}/"

    async def stop(self):
        if self.server is not None:
            await self.server.close()
            self.server = None

    async def drop_sockets(self):
        """Close every open socket, as a controller going away would."""
        for ws in list(self.sockets):
            await ws.close()
        self.sockets.clear()

    async def push_level(self, device_id: str, level):
        """Send an unsolicited level event, as the real controller does."""
        self.devices[device_id]["metrics"]["level"] = level
        for ws in list(self.sockets):
            await ws.send_str(json.dumps({"type": LEVEL_EVENT,
                                          "data": self.devices[device_id]}))

    async def _handle(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        async for message in ws:
            frame = json.loads(message.data)
            path = frame["data"]["url"]
            self.requests.append(path)
            if not self.answer:
                continue
            await ws.send_str(json.dumps({
                "type": frame["responseEvent"],
                "data": self._respond(path),
            }))
        return ws

    def _respond(self, path: str) -> dict:
        body = self._body(path)
        return {"status": body["code"], "body": json.dumps(body)}

    def _body(self, path: str) -> dict:
        rest = path.removeprefix("/ZAutomation/api/v1/devices")
        if not rest:
            return {"data": {"devices": list(self.devices.values())},
                    "code": 200, "message": "200 OK", "error": None}
        device_id, _, command = rest.lstrip("/").partition("/")
        if device_id not in self.devices:
            return {"data": None, "code": 404, "message": "404 Not Found",
                    "error": f"Device {device_id} doesn't exist"}
        if command:
            return {"data": None, "code": 200, "message": "200 OK", "error": None}
        return {"data": self.devices[device_id], "code": 200,
                "message": "200 OK", "error": None}


def build_endpoint(key, *, device, value_type=None, writable=False, match_case=False):
    """One endpoint, with the zway fields where module params live."""
    return Endpoint(key, value_type=value_type, writable=writable,
                    params={"device": device, "match_case": match_case})


def build_device(url, endpoints, *, update_interval=None, **params):
    """A ZWayDevice wired to `url`, with endpoints keyed by name."""
    return ZWayDevice(
        "zway1",
        endpoints=[build_endpoint(key, **spec) for key, spec in endpoints.items()],
        params={"url": url, "token": "tok", **params},
        context=_context,
        update_interval=update_interval,
    )


def run(coro):
    """Run one coroutine on a throwaway loop, closing it afterwards."""
    return asyncio.run(coro)


async def with_controller(body, *, devices=None, answer=True):
    """Start a fake controller, run `body(url, controller)`, stop it.

    Every connection built during the body is closed first: TestServer
    shutdown waits for its open WebSocket handlers to return, so a
    socket left open stalls teardown until the server's own timeout."""
    controller = FakeController(devices, answer=answer)
    url = await controller.start()
    try:
        return await body(url, controller)
    finally:
        await close_connections()
        await controller.drop_sockets()
        await controller.stop()


async def close_connections():
    """Close every connection the shared context is holding."""
    state = _context.get("zway")
    if state is None:
        return
    for _loop, connection in list(state.connections.values()):
        await connection.close()
    state.connections.clear()


# ---------- value translation ----------


def test_binary_levels_translate_to_real_booleans():
    # bool("off") is True, so a level that merely looks truthy must not be
    # allowed to stand in for the real state.
    assert to_phc("off", "switchBinary") is False
    assert to_phc("on", "switchBinary") is True
    assert to_phc("off", "sensorBinary") is False


@pytest.mark.parametrize("level", [None, "", "unexpected"])
def test_unreadable_levels_become_none(level):
    assert to_phc(level, "switchBinary") is None


def test_failed_node_reads_as_none():
    controller_device = _device(id="d", title="t", level="on", isFailed=True)
    connection = ZWayConnection("ws://x", "t")
    connection._store(controller_device)
    assert connection.levels["d"] is None


def test_numeric_levels_follow_the_profile_type():
    assert to_phc(42, "sensorMultilevel") == 42.0
    assert isinstance(to_phc(42, "sensorMultilevel"), float)
    assert to_phc("nope", "sensorMultilevel") is None
    assert to_phc(55, "switchMultilevel") == 55


def test_unknown_device_type_passes_the_level_through():
    assert to_phc("whatever", "somethingNew") == "whatever"


def test_doorlock_reads_the_raw_command_string():
    assert to_phc("close", "doorlock") == "close"
    assert to_phc("open", "doorlock") == "open"
    assert to_phc("clear", "doorlock") == "clear"
    assert to_phc("", "doorlock") is None
    assert to_phc(None, "doorlock") is None


# ---------- commands ----------


@pytest.mark.parametrize("value,expected", [
    (True, "command/on"), (False, "command/off"),
    ("on", "command/on"), ("off", "command/off"),
    (1, "command/on"), (0, "command/off"),
])
def test_binary_writes_map_to_on_off(value, expected):
    assert command_for(value, "switchBinary") == expected


def test_numeric_write_uses_exact_level():
    assert command_for(42, "switchMultilevel") == "command/exact?level=42"


@pytest.mark.parametrize("device_type", ["sensorMultilevel", "sensorBinary", "battery"])
def test_writes_to_sensors_are_rejected(device_type):
    # The real controller answers 200 OK to these, so nothing but this
    # check stands between a config typo and a silent no-op.
    with pytest.raises(ZWayError, match="read-only"):
        command_for(True, device_type)


def test_write_of_the_wrong_shape_is_rejected():
    with pytest.raises(ZWayError, match="cannot write"):
        command_for("warm", "thermostat")


def test_unknown_device_type_refuses_writes():
    with pytest.raises(ZWayError, match="unknown"):
        command_for(True, "somethingNew")


def test_doorlock_write_uses_the_value_as_a_named_command():
    assert command_for("open", "doorlock") == "command/open"
    assert command_for("close", "doorlock") == "command/close"
    assert command_for("clear", "doorlock") == "command/clear"
    assert command_for("CLEAR", "doorlock") == "command/clear"


def test_doorlock_rejects_a_bool_or_an_unrecognized_command_name():
    with pytest.raises(ZWayError, match="cannot write"):
        command_for(True, "doorlock")
    with pytest.raises(ZWayError, match="cannot write"):
        command_for("bogus", "doorlock")


# ---------- pattern resolution ----------


@pytest.mark.parametrize("pattern", [
    "ZWayVDev_zway_20-1-37",            # exact id
    "Rez: Light corridor",              # exact title
    "*light corridor*",                 # glob, and case-insensitive
    "REZ: LIGHT CORRIDOR",
])
def test_patterns_resolve_to_one_device(pattern):
    async def body(url, controller):
        connection = ZWayConnection(url, "tok")
        assert await connection.ensure_started()
        try:
            assert connection.resolve(pattern) == "ZWayVDev_zway_20-1-37"
        finally:
            await connection.close()

    run(with_controller(body))


def test_match_case_restores_case_sensitivity():
    async def body(url, controller):
        connection = ZWayConnection(url, "tok")
        assert await connection.ensure_started()
        try:
            assert connection.resolve("Rez: Light corridor", match_case=True)
            with pytest.raises(ZWayError, match="no z-way device"):
                connection.resolve("REZ: LIGHT CORRIDOR", match_case=True)
        finally:
            await connection.close()

    run(with_controller(body))


def test_ambiguous_pattern_names_every_candidate():
    async def body(url, controller):
        connection = ZWayConnection(url, "tok")
        assert await connection.ensure_started()
        try:
            with pytest.raises(ZWayError) as excinfo:
                connection.resolve("*Rez*")
            message = str(excinfo.value)
            assert "ZWayVDev_zway_20-1-37" in message
            assert "ZWayVDev_zway_11-0-49-1" in message
        finally:
            await connection.close()

    run(with_controller(body))


def test_patterns_resolve_once_per_connection():
    """A glob is config-time convenience, never a per-access cost."""
    async def body(url, controller):
        device = build_device(url, {
            "light": {"value_type": "bool", "writable": True,
                      "device": "*light corridor*"},
        })
        calls = []
        original = conn_mod.match_devices

        def counting(pattern, titles, match_case=False):
            calls.append(pattern)
            return original(pattern, titles, match_case)

        conn_mod.match_devices = counting
        try:
            for _ in range(3):
                await device.receive_async()
            await device.transmit_async({"light": True})
            await device.transmit_async({"light": False})
        finally:
            conn_mod.match_devices = original
        return len(calls)

    assert run(with_controller(body)) == 1


# ---------- reads and writes through the device ----------


def test_reads_translate_every_endpoint():
    async def body(url, controller):
        device = build_device(url, {
            "light": {"value_type": "bool", "device": "Rez: Light corridor"},
            "temp": {"value_type": "float", "device": "*living temp*"},
        })
        return await device.receive_async()

    values = run(with_controller(body))
    assert values == {"light": False, "temp": 19.4}


def test_push_event_updates_the_cached_level():
    async def body(url, controller):
        device = build_device(url, {
            "light": {"value_type": "bool", "device": "Rez: Light corridor"},
        })
        assert (await device.receive_async())["light"] is False
        await controller.push_level("ZWayVDev_zway_20-1-37", "on")
        await asyncio.sleep(0.05)       # let the reader drain the socket
        return (await device.receive_async())["light"]

    assert run(with_controller(body)) is True


def test_write_sends_the_command_for_the_device_type():
    async def body(url, controller):
        device = build_device(url, {
            "light": {"value_type": "bool", "writable": True, "device": "Rez: Light corridor"},
            "dimmer": {"value_type": "int", "writable": True, "device": "Dummy Device 2"},
        })
        await device.receive_async()
        await device.transmit_async({"light": True})
        await device.transmit_async({"dimmer": 42})
        return controller.requests

    requests = run(with_controller(body))
    assert "/ZAutomation/api/v1/devices/ZWayVDev_zway_20-1-37/command/on" in requests
    assert "/ZAutomation/api/v1/devices/DummyDevice_17/command/exact?level=42" in requests


def test_write_to_a_sensor_never_reaches_the_controller():
    async def body(url, controller):
        device = build_device(url, {
            "temp": {"value_type": "float", "writable": True, "device": "*living temp*"},
        })
        await device.receive_async()
        controller.requests.clear()
        await device.transmit_async({"temp": 20.0})
        return controller.requests, device.consume_reported_failure()

    requests, failure = run(with_controller(body))
    assert requests == []
    assert failure is not None and "read-only" in failure


def test_one_unresolvable_endpoint_leaves_its_siblings_working():
    async def body(url, controller):
        device = build_device(url, {
            "light": {"value_type": "bool", "device": "Rez: Light corridor"},
            "missing": {"value_type": "bool", "device": "*no such device*"},
        })
        values = await device.receive_async()
        return values, device.consume_reported_failure()

    values, failure = run(with_controller(body))
    assert values == {"light": False, "missing": None}
    assert failure is not None and "no such device" in failure


# ---------- connection failures ----------


def test_a_controller_that_never_answers_fails_rather_than_hanging():
    """The bad-token symptom: connected, then silence."""
    async def body(url, controller):
        connection = ZWayConnection(url, "bad", request_timeout=0.2)
        try:
            assert await connection.ensure_started() is False
            return connection.last_error
        finally:
            await connection.close()

    assert "no response" in run(with_controller(body, answer=False))


def test_unreachable_controller_reads_none_and_reports_failure():
    async def body(url, controller):
        await controller.stop()         # nothing listening any more
        device = build_device(url, {
            "light": {"value_type": "bool", "device": "Rez: Light corridor"},
        })
        values = await device.receive_async()
        return values, device.consume_reported_failure()

    values, failure = run(with_controller(body))
    assert values == {"light": None}
    assert failure is not None and "unavailable" in failure


def test_backoff_ladder_gates_reconnect_attempts():
    async def body(url, controller):
        await controller.stop()
        connection = ZWayConnection(url, "tok", request_timeout=0.2)
        assert await connection.ensure_started() is False
        first_retry = connection._retry_at
        # While the delay stands, no further attempt is even made.
        assert await connection.ensure_started() is False
        assert connection._retry_at == first_retry
        # The ladder lengthens: 1s, 10s, 60s, then 5m forever.
        delays = []
        for _ in range(5):
            connection._retry_at = 0.0
            await connection.ensure_started()
            delays.append(round(connection._retry_at - asyncio.get_running_loop().time()))
        return connection._attempt

    # One failure before the loop, five inside it.
    assert run(with_controller(body)) == 6


def test_reconnect_reprimes_and_rebinds():
    async def body(url, controller):
        device = build_device(url, {
            "light": {"value_type": "bool", "device": "Rez: Light corridor"},
        })
        assert (await device.receive_async())["light"] is False

        connection = device._state.connections[(url, "tok",
                                                id(asyncio.get_running_loop()))][1]
        generation = connection.generation

        # The controller drops the socket; the level changes while it is gone.
        controller.devices["ZWayVDev_zway_20-1-37"]["metrics"]["level"] = "on"
        await controller.drop_sockets()
        await asyncio.sleep(0.05)
        connection._retry_at = 0.0      # skip the backoff wait

        value = (await device.receive_async())["light"]
        return value, connection.generation > generation

    value, reprimed = run(with_controller(body))
    assert value is True
    assert reprimed


def test_404_on_one_write_keeps_the_connection_up():
    async def body(url, controller):
        device = build_device(url, {
            "light": {"value_type": "bool", "writable": True, "device": "Rez: Light corridor"},
        })
        await device.receive_async()
        connection = device._state.connections[(url, "tok",
                                                id(asyncio.get_running_loop()))][1]
        # Resolve stays valid while the controller forgets the device.
        del controller.devices["ZWayVDev_zway_20-1-37"]
        await device.transmit_async({"light": True})
        return connection.connected, device.consume_reported_failure()

    connected, failure = run(with_controller(body))
    assert connected
    assert failure is not None and "404" in failure


# ---------- through the scheduler ----------


def test_runs_through_scheduler_ticks():
    """The loop only turns during a tick, so nothing may rely on the
    reader task having run in between."""
    controller = FakeController()
    device = build_device("ws://placeholder", {
        "light": {"value_type": "bool", "writable": True,
                  "device": "Rez: Light corridor"},
    }, update_interval=1.0)
    scheduler = Scheduler({"zway1": device}, heartbeat=1.0)
    # The controller must live on the Scheduler's own loop: a TestServer
    # started on one loop cannot be stopped from another.
    scheduler._ensure_runtime()
    loop = scheduler._loop

    async def finish():
        await close_connections()
        await controller.drop_sockets()
        await controller.stop()

    try:
        device._url = loop.run_until_complete(controller.start())
        scheduler.tick(now=0.0)
        scheduler.tick(now=1.0)
        assert device.endpoints["light"].get() is False
    finally:
        loop.run_until_complete(finish())
        scheduler.close()
