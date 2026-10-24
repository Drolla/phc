"""MatterDevice over the matterjs-server WebSocket API.

Drives the real protocol path against a fake in-process server: the
server-info frame and `start_listening` priming, request/reply framing,
the push events that keep state current between polls, node resolution,
value translation and the reconnect ladder.

Everything runs through direct receive/transmit calls or Scheduler.tick(),
which only turns the event loop while a tick is in flight -- so these
tests also pin down that the module never depends on its reader task
having run in between."""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from phc.core.endpoint import Endpoint
from phc.core.scheduler import Scheduler
from phc.devices.matter import matter_ws as conn_mod
from phc.devices.matter.device import MatterDevice
from phc.devices.matter.matter_ws import (
    MatterConnection,
    MatterError,
    match_nodes,
    to_phc,
    write_for,
)

# The per-system context matter devices share (phc.core.device.Device.context),
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


def _node(node_id, label, serial, *, available=True, **attributes):
    """One node object, as `start_listening` reports it."""
    return {
        "node_id": node_id,
        "available": available,
        "attributes": {"0/40/5": label, "0/40/15": serial, **attributes},
    }


def default_nodes():
    """A fresh set of nodes.

    Built per call: a test that pushes an attribute mutates these objects,
    so sharing one list would leak state into the next test. Node 3 is
    the real Ledvance plug's capture (struct values keyed by tag number)."""
    return [
        _node(3, "My 1st Power Switch (Osram)", "10024458700D6B", **{
            "1/6/0": False,
            "1/144/8": 0,
            "1/144/4": 239000,
            "1/145/1": {"0": 64000, "1": 2, "2": 2},
        }),
        _node(4, "Living lamp", "SER-4", **{"1/6/0": True, "1/8/0": 127}),
        _node(5, "Hall sensor", "SER-5", **{
            "1/1026/0": 2150, "1/1030/0": 1, "1/69/0": None, "1/47/12": 190,
        }),
    ]


class FakeServer:
    """A stand-in for the matterjs-server WebSocket endpoint.

    Answers commands and can push events, so a test can exercise both
    directions of the protocol."""

    def __init__(self, nodes=None, *, answer=True):
        self.nodes = {n["node_id"]: n for n in (nodes or default_nodes())}
        # False reproduces a server that accepts the socket and then never
        # answers anything.
        self.answer = answer
        self.fail_commands = False
        self.requests: list[dict] = []
        self.sockets: list[web.WebSocketResponse] = []
        self.server: TestServer | None = None

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/ws", self._handle)
        self.server = TestServer(app)
        await self.server.start_server()
        return f"ws://127.0.0.1:{self.server.port}/ws"

    async def stop(self):
        if self.server is not None:
            await self.server.close()
            self.server = None

    async def drop_sockets(self):
        """Close every open socket, as a server going away would."""
        for ws in list(self.sockets):
            await ws.close()
        self.sockets.clear()

    async def push(self, event, data):
        """Send an unsolicited event, as the real server does."""
        for ws in list(self.sockets):
            await ws.send_str(json.dumps({"event": event, "data": data}))

    async def push_attribute(self, node_id, path, value):
        """Change an attribute and tell every listener."""
        self.nodes[node_id]["attributes"][path] = value
        await self.push("attribute_updated", [node_id, path, value])

    async def _handle(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        await ws.send_str(json.dumps({"schema_version": 14, "bluetooth_enabled": True}))
        async for message in ws:
            frame = json.loads(message.data)
            self.requests.append(frame)
            if not self.answer:
                continue
            await ws.send_str(json.dumps(self._respond(frame)))
        return ws

    def _respond(self, frame: dict) -> dict:
        reply = {"message_id": frame["message_id"]}
        if frame["command"] in ("start_listening", "get_nodes"):
            reply["result"] = list(self.nodes.values())
        elif frame["command"] == "device_command" and self.fail_commands:
            reply.update(error_code=7, details="InvalidCommand")
        else:
            reply["result"] = None
        return reply

    def commands(self) -> list[dict]:
        """The args of every device_command received, in order."""
        return [r["args"] for r in self.requests if r["command"] == "device_command"]


def build_endpoint(key, *, attribute=None, value_type=None, writable=False, **params):
    """One endpoint, with the matter fields where module params live."""
    return Endpoint(key, value_type=value_type, writable=writable,
                    params={"attribute": attribute, **params})


def build_device(url, endpoints, *, update_interval=None, **params):
    """A MatterDevice wired to `url`, with endpoints keyed by name."""
    return MatterDevice(
        "matter1",
        endpoints=[build_endpoint(key, **spec) for key, spec in endpoints.items()],
        params={"url": url, **params},
        context=_context,
        update_interval=update_interval,
    )


def connection_of(device, url):
    """The shared connection a device built on the running loop."""
    return device._state.connections[(url, id(asyncio.get_running_loop()))][1]


def run(coro):
    """Run one coroutine on a throwaway loop, closing it afterwards."""
    return asyncio.run(coro)


async def with_server(body, *, nodes=None, answer=True):
    """Start a fake server, run `body(url, server)`, stop it.

    Every connection built during the body is closed first: TestServer
    shutdown waits for its open WebSocket handlers to return, so a
    socket left open stalls teardown until the server's own timeout."""
    server = FakeServer(nodes, answer=answer)
    url = await server.start()
    try:
        return await body(url, server)
    finally:
        await close_connections()
        await server.drop_sockets()
        await server.stop()


async def close_connections():
    """Close every connection the shared context is holding."""
    state = _context.get("matter")
    if state is None:
        return
    for _loop, connection in list(state.connections.values()):
        await connection.close()
    state.connections.clear()


SWITCH = {"attribute": "6/0", "value_type": "bool", "writable": True,
          "commands": {True: "On", False: "Off"}}
DIMMER = {"attribute": "8/0", "value_type": "int", "writable": True,
          "level_command": "MoveToLevelWithOnOff", "scale": 0.393701}


# ---------- value translation ----------


def test_scale_field_and_rounding():
    assert to_phc(239000, {"scale": 0.001}, "float") == pytest.approx(239.0)
    assert to_phc({"0": 64000, "1": 2}, {"field": 0, "scale": 1e-6}, "float") == \
        pytest.approx(0.064)
    assert to_phc(127, {"scale": 0.393701}, "int") == 50
    assert to_phc(190, {"scale": 0.5}, "int") == 95


def test_mask_and_bool_reads():
    assert to_phc(1, {"mask": 1}, "bool") is True
    assert to_phc(0, {"mask": 1}, "bool") is False
    assert to_phc(True, {}, "bool") is True
    assert to_phc(0, {}, "bool") is False


@pytest.mark.parametrize("raw, params", [
    (None, {}),
    ("garbage", {"scale": 2}),
    (5, {"field": 0}),                  # field on a non-struct
    ({"1": 5}, {"field": 0}),           # field the struct lacks
    (True, {"scale": 2}),               # a bool is not a number
])
def test_unreadable_values_become_none(raw, params):
    assert to_phc(raw, params, "float") is None


@pytest.mark.parametrize("value, expected", [
    (True, "On"), (False, "Off"), ("on", "On"), ("OFF", "Off"), (1, "On"), (0, "Off"),
])
def test_bool_writes_pick_the_declared_command(value, expected):
    assert write_for(value, SWITCH) == (expected, {})


def test_level_write_divides_by_scale():
    name, payload = write_for(50, DIMMER)
    assert name == "MoveToLevelWithOnOff"
    assert payload["level"] == 127
    assert write_for(100, DIMMER)[1]["level"] == 254
    assert write_for(0, DIMMER)[1]["level"] == 0


@pytest.mark.parametrize("value, params", [
    (True, {}),                          # nothing declared: read-only
    (True, DIMMER),                      # a level endpoint takes no bool
    ("banana", SWITCH),
    (None, SWITCH),
])
def test_writes_of_the_wrong_shape_are_rejected(value, params):
    with pytest.raises(MatterError):
        write_for(value, params)


# ---------- node matching ----------


def test_match_is_case_insensitive_unless_asked():
    names = {3: ["My 1st Power Switch (Osram)", "10024458700D6B"], 4: ["Living lamp"]}
    assert match_nodes("*power switch*", names) == [3]
    assert match_nodes("10024458700d6b", names) == [3]
    assert match_nodes("*power switch*", names, match_case=True) == []
    assert match_nodes("*Power Switch*", names, match_case=True) == [3]


def test_ambiguous_pattern_names_every_candidate():
    async def body(url, controller):
        connection = MatterConnection(url)
        try:
            assert await connection.ensure_started()
            with pytest.raises(conn_mod.MatterLookupError) as excinfo:
                connection.resolve("*")
            return str(excinfo.value)
        finally:
            await connection.close()

    message = run(with_server(body))
    assert "matches 3 matter nodes" in message
    assert "3 (" in message and "5 (Hall sensor)" in message


def test_node_id_and_label_and_serial_all_resolve():
    async def body(url, controller):
        connection = MatterConnection(url)
        try:
            assert await connection.ensure_started()
            ids = (connection.resolve(4), connection.resolve("living LAMP"),
                   connection.resolve("SER-4"))
            with pytest.raises(conn_mod.MatterLookupError):
                connection.resolve(99)
            return ids
        finally:
            await connection.close()

    assert run(with_server(body)) == (4, 4, 4)


# ---------- reads through the device ----------


def test_reads_translate_every_endpoint():
    async def body(url, controller):
        device = build_device(url, {
            "switch": {**SWITCH, "node": "My 1st*"},
            "power": {"attribute": "144/8", "value_type": "float", "scale": 0.001,
                      "node": "My 1st*"},
            "voltage": {"attribute": "144/4", "value_type": "float", "scale": 0.001,
                        "node": "My 1st*"},
            "energy": {"attribute": "145/1", "value_type": "float", "field": 0,
                       "scale": 1e-6, "node": "My 1st*"},
            "temp": {"attribute": "1026/0", "value_type": "float", "scale": 0.01,
                     "node": "Hall*"},
            "motion": {"attribute": "1030/0", "value_type": "bool", "mask": 1,
                       "node": "Hall*"},
            "contact": {"attribute": "69/0", "value_type": "bool", "node": "Hall*"},
            "battery": {"attribute": "47/12", "value_type": "int", "scale": 0.5,
                        "node": "Hall*"},
        })
        return await device.receive_async()

    values = run(with_server(body))
    assert values["switch"] is False
    assert values["power"] == pytest.approx(0.0)
    assert values["voltage"] == pytest.approx(239.0)
    assert values["energy"] == pytest.approx(0.064)
    assert values["temp"] == pytest.approx(21.5)
    assert values["motion"] is True
    assert values["contact"] is None          # node reports null
    assert values["battery"] == 95


def test_device_node_is_the_default_and_an_endpoint_overrides_it():
    async def body(url, controller):
        device = build_device(url, {
            "a": {**SWITCH},                       # device-level node (4)
            "b": {**SWITCH, "node": 3},            # overridden, by node id
        }, node="Living lamp")
        return await device.receive_async()

    assert run(with_server(body)) == {"a": True, "b": False}


def test_endpoint_number_defaults_to_one_and_can_be_set():
    async def body(url, controller):
        controller.nodes[4]["attributes"]["2/6/0"] = False
        device = build_device(url, {
            "ep1": {**SWITCH},
            "ep2": {**SWITCH, "endpoint": 2},
        }, node="Living lamp")
        return await device.receive_async()

    assert run(with_server(body)) == {"ep1": True, "ep2": False}


def test_patterns_resolve_once_per_generation():
    """A glob is config-time convenience, never a per-access cost."""
    async def body(url, controller):
        device = build_device(url, {"light": {**SWITCH, "node": "Living*"}})
        calls = []
        original = conn_mod.match_nodes

        def counting(pattern, names, match_case=False):
            calls.append(pattern)
            return original(pattern, names, match_case)

        conn_mod.match_nodes = counting
        try:
            for _ in range(3):
                await device.receive_async()
            await device.transmit_async({"light": True})
            await device.transmit_async({"light": False})
        finally:
            conn_mod.match_nodes = original
        return len(calls)

    assert run(with_server(body)) == 1


# ---------- push events ----------


def test_attribute_event_updates_the_cache():
    async def body(url, controller):
        device = build_device(url, {"light": {**SWITCH, "node": "Living*"}})
        assert (await device.receive_async())["light"] is True
        await controller.push_attribute(4, "1/6/0", False)
        await asyncio.sleep(0.05)       # let the reader drain the socket
        return (await device.receive_async())["light"]

    assert run(with_server(body)) is False


def test_event_hook_sees_events_after_the_cache_applied_them():
    async def body(url, controller):
        connection = MatterConnection(url)
        seen = []
        connection.on_event = lambda event, data: seen.append(
            (event, data, connection.read(4, "1/6/0")))
        try:
            assert await connection.ensure_started()
            await controller.push_attribute(4, "1/6/0", False)
            await asyncio.sleep(0.05)
            return seen
        finally:
            await connection.close()

    assert run(with_server(body)) == [("attribute_updated", [4, "1/6/0", False], False)]


def test_unavailable_node_reads_none_and_recovers():
    async def body(url, controller):
        device = build_device(url, {"light": {**SWITCH, "node": "Living*"}})
        await device.receive_async()
        node = controller.nodes[4]
        await controller.push("node_updated", {**node, "available": False})
        await asyncio.sleep(0.05)
        down = (await device.receive_async())["light"]
        await controller.push("node_updated", {**node, "available": True})
        await asyncio.sleep(0.05)
        return down, (await device.receive_async())["light"]

    assert run(with_server(body)) == (None, True)


def test_removed_node_reads_none_and_a_readded_one_rebinds():
    async def body(url, controller):
        device = build_device(url, {"light": {**SWITCH, "node": "Living*"}})
        await device.receive_async()
        node = controller.nodes[4]
        await controller.push("node_removed", 4)
        await asyncio.sleep(0.05)
        gone = await device.receive_async()
        failure = device.consume_reported_failure()
        await controller.push("node_added", node)
        await asyncio.sleep(0.05)
        return gone, failure, await device.receive_async()

    gone, failure, back = run(with_server(body))
    assert gone == {"light": None}
    assert failure is not None and "no matter node matches" in failure
    assert back == {"light": True}


def test_relabelling_a_node_rebinds_the_endpoint():
    async def body(url, controller):
        device = build_device(url, {"light": {**SWITCH, "node": "Living*"}})
        assert (await device.receive_async())["light"] is True
        await controller.push_attribute(4, "0/40/5", "Kitchen lamp")
        await asyncio.sleep(0.05)
        gone = await device.receive_async()
        return gone, device.consume_reported_failure()

    gone, failure = run(with_server(body))
    assert gone == {"light": None}
    assert failure is not None and "Living*" in failure


# ---------- writes through the device ----------


def test_write_sends_the_declared_command():
    async def body(url, controller):
        device = build_device(url, {
            "light": {**SWITCH, "node": "Living*"},
            "dimmer": {**DIMMER, "node": "Living*"},
        })
        await device.receive_async()
        await device.transmit_async({"light": False})
        await device.transmit_async({"dimmer": 50})
        return controller.commands()

    off, level = run(with_server(body))
    assert off == {"node_id": 4, "endpoint_id": 1, "cluster_id": 6,
                   "command_name": "Off", "payload": {}}
    assert level["cluster_id"] == 8
    assert level["command_name"] == "MoveToLevelWithOnOff"
    assert level["payload"]["level"] == 127


def test_write_to_a_read_only_endpoint_never_reaches_the_server():
    async def body(url, controller):
        device = build_device(url, {
            "temp": {"attribute": "1026/0", "value_type": "float", "writable": True,
                     "node": "Hall*"},
        })
        await device.receive_async()
        await device.transmit_async({"temp": 20.0})
        return controller.commands(), device.consume_reported_failure()

    commands, failure = run(with_server(body))
    assert commands == []
    assert failure is not None and "cannot write" in failure


def test_one_unresolvable_endpoint_leaves_its_siblings_working():
    async def body(url, controller):
        device = build_device(url, {
            "light": {**SWITCH, "node": "Living*"},
            "missing": {**SWITCH, "node": "*no such node*"},
        })
        values = await device.receive_async()
        return values, device.consume_reported_failure()

    values, failure = run(with_server(body))
    assert values == {"light": True, "missing": None}
    assert failure is not None and "no such node" in failure


def test_an_endpoint_without_node_or_attribute_is_reported():
    async def body(url, controller):
        device = build_device(url, {"orphan": {"value_type": "bool"}})
        values = await device.receive_async()
        return values, device.consume_reported_failure()

    values, failure = run(with_server(body))
    assert values == {"orphan": None}
    assert failure is not None and "needs a node" in failure


def test_error_reply_to_a_write_keeps_the_connection_up():
    async def body(url, controller):
        device = build_device(url, {"light": {**SWITCH, "node": "Living*"}})
        await device.receive_async()
        controller.fail_commands = True
        await device.transmit_async({"light": True})
        return connection_of(device, url).connected, device.consume_reported_failure()

    connected, failure = run(with_server(body))
    assert connected
    assert failure is not None and "InvalidCommand" in failure


# ---------- connection failures ----------


def test_a_server_that_never_answers_fails_rather_than_hanging():
    async def body(url, controller):
        connection = MatterConnection(url, request_timeout=0.2)
        try:
            assert await connection.ensure_started() is False
            return connection.last_error
        finally:
            await connection.close()

    assert "no response" in run(with_server(body, answer=False))


def test_unreachable_server_reads_none_and_reports_failure():
    async def body(url, controller):
        await controller.stop()         # nothing listening any more
        device = build_device(url, {"light": {**SWITCH, "node": "Living*"}})
        values = await device.receive_async()
        return values, device.consume_reported_failure()

    values, failure = run(with_server(body))
    assert values == {"light": None}
    assert failure is not None and "unavailable" in failure


def test_backoff_ladder_gates_reconnect_attempts():
    async def body(url, controller):
        await controller.stop()
        connection = MatterConnection(url, request_timeout=0.2)
        assert await connection.ensure_started() is False
        first_retry = connection._retry_at
        # While the delay stands, no further attempt is even made.
        assert await connection.ensure_started() is False
        assert connection._retry_at == first_retry
        for _ in range(5):
            connection._retry_at = 0.0
            await connection.ensure_started()
        return connection._attempt

    # One failure before the loop, five inside it.
    assert run(with_server(body)) == 6


def test_server_info_is_kept():
    async def body(url, controller):
        connection = MatterConnection(url)
        try:
            assert await connection.ensure_started()
            return connection.server_info
        finally:
            await connection.close()

    assert run(with_server(body))["bluetooth_enabled"] is True


def test_reconnect_reprimes_and_rebinds():
    async def body(url, controller):
        device = build_device(url, {"light": {**SWITCH, "node": "Living*"}})
        assert (await device.receive_async())["light"] is True
        connection = connection_of(device, url)
        generation = connection.generation

        # The server drops the socket; the state changes while it is gone.
        controller.nodes[4]["attributes"]["1/6/0"] = False
        await controller.drop_sockets()
        await asyncio.sleep(0.05)
        connection._retry_at = 0.0      # skip the backoff wait

        value = (await device.receive_async())["light"]
        return value, connection.generation > generation

    value, reprimed = run(with_server(body))
    assert value is False
    assert reprimed


def test_resync_rereads_when_due():
    async def body(url, controller):
        device = build_device(url, {"light": {**SWITCH, "node": "Living*"}},
                              resync_interval="1s")
        assert (await device.receive_async())["light"] is True
        # An event the client never saw: only a resync can pick it up.
        controller.nodes[4]["attributes"]["1/6/0"] = False
        connection_of(device, url)._last_resync -= 10
        return (await device.receive_async())["light"]

    assert run(with_server(body)) is False


# ---------- through the scheduler ----------


def test_runs_through_scheduler_ticks():
    """The loop only turns during a tick, so nothing may rely on the
    reader task having run in between."""
    server = FakeServer()
    device = build_device("ws://placeholder", {
        "light": {**SWITCH, "node": "Living*"},
    }, update_interval=1.0)
    scheduler = Scheduler({"matter1": device}, heartbeat=1.0)
    # The server must live on the Scheduler's own loop: a TestServer
    # started on one loop cannot be stopped from another.
    scheduler._ensure_runtime()
    loop = scheduler._loop

    async def finish():
        await close_connections()
        await server.drop_sockets()
        await server.stop()

    try:
        device._url = loop.run_until_complete(server.start())
        scheduler.tick(now=0.0)
        scheduler.tick(now=1.0)
        assert device.endpoints["light"].get() is True
    finally:
        loop.run_until_complete(finish())
        scheduler.close()
