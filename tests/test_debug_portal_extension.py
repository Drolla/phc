"""Tests for phc.extensions.debug_portal.extension: configure()'s selector
resolution, the on_bind/on_tick/on_start/on_stop hook wiring, and the
aiohttp.web app it builds (server.py's routes), driven via aiohttp's test
utilities. No full YAML system needed -- a small hand-built `flat` dict of
VirtualDevice/Endpoint instances, mirroring tests/test_web_ui_extension.py's
style.

No pytest-asyncio dependency: async bodies are wrapped in asyncio.run(),
matching this suite's existing convention (see
tests/test_web_ui_extension.py's own docstring)."""

import asyncio
import json

import aiohttp
import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer

from phc.core.endpoint import Endpoint
from phc.devices.virtual.device import VirtualDevice
from phc.extensions.debug_portal.extension import configure
from phc.extensions.debug_portal.server import LAST_SNAPSHOT


class FakeSystem:
    """Stand-in for phc.core.config.System: DebugPortalInstance only ever
    reads .tasks and .heartbeat off whatever on_bind() is given."""

    def __init__(self, tasks=None, heartbeat=1.0):
        self.tasks = tasks if tasks is not None else []
        self.heartbeat = heartbeat


def _base_params(**overrides):
    params = {
        "host": "127.0.0.1",
        "port": 8081,
        "selectors": ["*"],
        "shutdown_timeout": 5,
    }
    params.update(overrides)
    return params


def _flat():
    lamp = VirtualDevice("lamp", endpoints=[Endpoint("state", writable=True, value_type="bool")])
    sensor = VirtualDevice("sensor", endpoints=[Endpoint("temp", writable=False, value_type="float")])
    return {"lamp": lamp, "sensor": sensor}


# ---------- configure() ----------

def test_configure_resolves_all_endpoints_by_default():
    instance = configure(_base_params(), _flat(), "debug_portal.demo")
    assert instance._pairs == [("lamp", "state"), ("sensor", "temp")]


def test_configure_selectors_narrow_endpoint_set():
    instance = configure(_base_params(selectors=["lamp/*"]), _flat(), "debug_portal.demo")
    assert instance._pairs == [("lamp", "state")]


# ---------- on_bind / on_tick ----------

def test_on_bind_stores_the_system():
    instance = configure(_base_params(), _flat(), "debug_portal.demo")
    system = FakeSystem()
    instance.on_bind(system)
    assert instance._system is system


def test_on_tick_populates_last_snapshot_from_bound_system():
    flat = _flat()
    instance = configure(_base_params(), flat, "debug_portal.demo")
    instance.on_bind(FakeSystem(heartbeat=2.0))

    instance.on_tick(flat)

    snapshot = instance._app[LAST_SNAPSHOT].value
    assert snapshot["tick"] == 1
    assert snapshot["heartbeat"] == 2.0
    assert len(snapshot["endpoints"]) == 2


def test_on_tick_period_defaults_to_heartbeat_then_measures_elapsed(monkeypatch):
    import phc.extensions.debug_portal.extension as ext_module

    flat = _flat()
    instance = configure(_base_params(), flat, "debug_portal.demo")
    instance.on_bind(FakeSystem(heartbeat=3.0))

    times = iter([1000.0, 1001.5])
    monkeypatch.setattr(ext_module.time, "time", lambda: next(times))

    instance.on_tick(flat)
    assert instance._app[LAST_SNAPSHOT].value["period"] == 3.0  # no prior tick -- falls back to heartbeat

    instance.on_tick(flat)
    assert instance._app[LAST_SNAPSHOT].value["period"] == 1.5  # measured delta between the two calls


# ---------- HTTP behavior (TestClient/TestServer) ----------

async def _client_for(params=None, flat=None, system=None):
    instance = configure(params or _base_params(), flat if flat is not None else _flat(),
                          "debug_portal.demo")
    instance.on_bind(system if system is not None else FakeSystem())
    client = TestClient(TestServer(instance._app))
    await client.start_server()
    return client, instance


def test_get_index_renders_endpoint_skeleton():
    async def run():
        client, _ = await _client_for()
        try:
            resp = await client.get("/")
            assert resp.status == 200
            text = await resp.text()
            assert 'data-key="lamp/state"' in text
            assert 'data-device="lamp" data-endpoint="state"' in text
            assert 'data-key="sensor/temp"' in text
        finally:
            await client.close()
    asyncio.run(run())


def test_get_api_snapshot_503_before_first_tick():
    async def run():
        client, _ = await _client_for()
        try:
            resp = await client.get("/api/snapshot")
            assert resp.status == 503
        finally:
            await client.close()
    asyncio.run(run())


def test_get_api_snapshot_returns_last_broadcast_snapshot():
    async def run():
        flat = _flat()
        client, instance = await _client_for(flat=flat)
        try:
            instance.on_tick(flat)
            resp = await client.get("/api/snapshot")
            assert resp.status == 200
            body = await resp.json()
            assert body["tick"] == 1
            assert len(body["endpoints"]) == 2
        finally:
            await client.close()
    asyncio.run(run())


def test_sse_stream_delivers_one_message_per_tick():
    async def run():
        flat = _flat()
        client, instance = await _client_for(flat=flat)
        try:
            resp = await client.get("/events")
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/event-stream")

            instance.on_tick(flat)
            line = await asyncio.wait_for(resp.content.readline(), timeout=2)
            assert line.startswith(b"data: ")
            payload = json.loads(line[len(b"data: "):])
            assert payload["tick"] == 1

            instance.on_tick(flat)
            # Skip the blank line separating SSE frames, then read the next one.
            await asyncio.wait_for(resp.content.readline(), timeout=2)
            line = await asyncio.wait_for(resp.content.readline(), timeout=2)
            assert line.startswith(b"data: ")
            payload = json.loads(line[len(b"data: "):])
            assert payload["tick"] == 2
        finally:
            resp.close()
            await client.close()
    asyncio.run(run())


def test_sse_client_mailbox_coalesces_when_not_drained():
    """A client that hasn't read yet must see only the LATEST snapshot once
    it does read -- broadcast() overwrites the single-slot mailbox rather
    than queueing (see SseHub)."""
    async def run():
        flat = _flat()
        client, instance = await _client_for(flat=flat)
        try:
            resp = await client.get("/events")
            assert resp.status == 200

            instance.on_tick(flat)  # tick 1 -- never read
            instance.on_tick(flat)  # tick 2 -- overwrites the mailbox

            line = await asyncio.wait_for(resp.content.readline(), timeout=2)
            payload = json.loads(line[len(b"data: "):])
            assert payload["tick"] == 2
        finally:
            resp.close()
            await client.close()
    asyncio.run(run())


def test_on_stop_wakes_open_sse_connections():
    async def run():
        flat = _flat()
        client, instance = await _client_for(flat=flat)
        try:
            resp = await client.get("/events")
            assert resp.status == 200

            await instance.on_stop(flat)  # _runner is None here -- only sets SHUTDOWN_EVENT

            remaining = await asyncio.wait_for(resp.content.read(), timeout=2)
            assert remaining == b""  # connection closed promptly, not left hanging
        finally:
            resp.close()
            await client.close()
    asyncio.run(run())


# ---------- real on_start()/on_stop() lifecycle (not just TestClient) ----------

def test_on_start_binds_a_real_port_and_on_stop_releases_it():
    async def run():
        flat = _flat()
        instance = configure(_base_params(port=0), flat, "debug_portal.demo")
        instance.on_bind(FakeSystem())
        await instance.on_start(flat)
        try:
            port = instance._runner.addresses[0][1]
            async with ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/") as resp:
                    assert resp.status == 200
        finally:
            await instance.on_stop(flat)

        # The port is closed, so connecting fails -- the same pair the
        # device modules catch for a network failure.
        with pytest.raises((aiohttp.ClientError, asyncio.TimeoutError)):
            async with ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/", timeout=1) as resp:
                    pass
    asyncio.run(run())
