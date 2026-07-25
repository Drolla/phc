"""Tests for extensions.web_ui.extension: configure()'s page/section/panel
YAML parsing, and the aiohttp.web app it builds (server.py's routes),
driven via aiohttp's test utilities. No full YAML system needed -- a
small hand-built `flat` dict of VirtualDevice/Endpoint instances, mirrored
after tests/test_logdb_extension.py's style.

No pytest-asyncio dependency: async bodies are wrapped in asyncio.run(),
matching this suite's existing convention (see tests/conftest.py's
fetch_sync)."""

import asyncio

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer

from core.config import ConfigError
from core.endpoint import Endpoint
from devices.virtual.device import VirtualDevice
from extensions.web_ui.extension import configure
from extensions.web_ui.server import PAGES


def _base_params(**overrides):
    params = {
        "host": "127.0.0.1",
        "port": 8080,
        "selectors": ["*"],
        "pages": None,
        "refresh_interval": "2s",
        "shutdown_timeout": 5,
    }
    params.update(overrides)
    return params


def _flat():
    lamp = VirtualDevice("lamp", endpoints=[Endpoint("state", writable=True, value_type="bool")])
    dimmer = VirtualDevice("dimmer", endpoints=[
        Endpoint("level", writable=True, value_type="int", min=0, max=100, unit="%"),
    ])
    fan = VirtualDevice("fan", endpoints=[
        Endpoint("speed", writable=True, value_type="int", values={0: "off", 1: "low", 2: "high"}),
    ])
    sensor = VirtualDevice("sensor", endpoints=[Endpoint("temp", writable=False, value_type="float", unit="C")])
    return {"lamp": lamp, "dimmer": dimmer, "fan": fan, "sensor": sensor}


async def _commit(device, endpoint_key, value):
    """Stage + fetch + commit a write, mirroring the two-phase model a real
    scheduler tick drives (tests/test_logdb_extension.py's sync _commit,
    adapted to run inside these tests' own event loop)."""
    device.set(value, name=endpoint_key)
    await device.fetch()
    device.update_state()


# ---------- configure(): page/section/panel parsing ----------

def test_configure_without_pages_synthesizes_single_page_and_section():
    instance = configure(_base_params(), _flat(), "web_ui.demo")
    pages = instance._app[PAGES]
    assert [p.id for p in pages] == ["home"]
    assert [s.id for s in pages[0].sections] == ["all"]
    assert pages[0].sections[0].collapsed is False


def test_configure_with_pages_builds_hierarchy():
    params = _base_params(pages=[
        {"id": "overview", "title": "Overview", "sections": [
            {"id": "lights", "title": "Lights", "collapsed": False, "selectors": ["lamp/*"]},
        ]},
        {"id": "sensors", "sections": [
            {"id": "readings", "selectors": ["sensor/*"]},
        ]},
    ])
    instance = configure(params, _flat(), "web_ui.demo")
    pages = instance._app[PAGES]

    assert [p.id for p in pages] == ["overview", "sensors"]
    assert pages[0].title == "Overview"
    assert pages[1].title == "sensors"  # defaults to id
    assert pages[0].sections[0].id == "lights"
    assert pages[0].sections[0].collapsed is False
    assert pages[1].sections[0].collapsed is True  # default


def test_configure_page_missing_id_raises():
    params = _base_params(pages=[{"sections": [{"id": "s", "selectors": ["*"]}]}])
    with pytest.raises(ConfigError):
        configure(params, _flat(), "web_ui.demo")


def test_configure_duplicate_page_id_raises():
    section = {"id": "s", "selectors": ["*"]}
    params = _base_params(pages=[
        {"id": "a", "sections": [section]},
        {"id": "a", "sections": [section]},
    ])
    with pytest.raises(ConfigError):
        configure(params, _flat(), "web_ui.demo")


def test_configure_section_missing_id_raises():
    params = _base_params(pages=[{"id": "a", "sections": [{"selectors": ["*"]}]}])
    with pytest.raises(ConfigError):
        configure(params, _flat(), "web_ui.demo")


def test_configure_duplicate_section_id_within_page_raises():
    params = _base_params(pages=[{"id": "a", "sections": [
        {"id": "s", "selectors": ["*"]},
        {"id": "s", "selectors": ["lamp/*"]},
    ]}])
    with pytest.raises(ConfigError):
        configure(params, _flat(), "web_ui.demo")


def test_configure_section_requires_exactly_one_of_selectors_or_panels():
    neither = _base_params(pages=[{"id": "a", "sections": [{"id": "s"}]}])
    with pytest.raises(ConfigError):
        configure(neither, _flat(), "web_ui.demo")

    both = _base_params(pages=[{"id": "a", "sections": [
        {"id": "s", "selectors": ["*"], "panels": [{"kind": "devices", "selectors": ["*"]}]},
    ]}])
    with pytest.raises(ConfigError):
        configure(both, _flat(), "web_ui.demo")


def test_configure_panel_unknown_kind_raises():
    params = _base_params(pages=[{"id": "a", "sections": [
        {"id": "s", "panels": [{"kind": "graph", "selectors": ["*"]}]},
    ]}])
    with pytest.raises(ConfigError):
        configure(params, _flat(), "web_ui.demo")


def test_configure_explicit_devices_panel_kind_works_like_shorthand():
    params = _base_params(pages=[{"id": "a", "sections": [
        {"id": "s", "panels": [{"kind": "devices", "selectors": ["lamp/*"]}]},
    ]}])
    instance = configure(params, _flat(), "web_ui.demo")
    panel = instance._app[PAGES][0].sections[0].panels[0]
    assert panel.pairs == [("lamp", "state")]


# ---------- HTTP behavior ----------

async def _client_for(params=None, flat=None):
    instance = configure(params or _base_params(), flat if flat is not None else _flat(), "web_ui.demo")
    client = TestClient(TestServer(instance._app))
    await client.start_server()
    return client, instance


def test_get_root_redirects_to_first_page():
    async def run():
        client, _ = await _client_for()
        try:
            resp = await client.get("/", allow_redirects=False)
            assert resp.status == 302
            assert resp.headers["Location"] == "/page/home"
        finally:
            await client.close()
    asyncio.run(run())


def test_get_page_renders_expected_widgets():
    async def run():
        client, _ = await _client_for()
        try:
            resp = await client.get("/page/home")
            assert resp.status == 200
            text = await resp.text()
            assert 'data-device="lamp" data-endpoint="state"' in text
            assert "widget-toggle" in text
            assert "widget-slider" in text
            assert "widget-dropdown" in text
            assert "widget-label" in text
        finally:
            await client.close()
    asyncio.run(run())


def test_get_page_unknown_id_404():
    async def run():
        client, _ = await _client_for()
        try:
            resp = await client.get("/page/nonexistent")
            assert resp.status == 404
        finally:
            await client.close()
    asyncio.run(run())


def test_get_page_section_details_open_state_matches_collapsed():
    async def run():
        params = _base_params(pages=[{"id": "a", "sections": [
            {"id": "open_section", "title": "Open", "collapsed": False, "selectors": ["lamp/*"]},
            {"id": "closed_section", "title": "Closed", "collapsed": True, "selectors": ["dimmer/*"]},
        ]}])
        client, _ = await _client_for(params)
        try:
            resp = await client.get("/page/a")
            text = await resp.text()
            open_pos = text.index("Open")
            closed_pos = text.index("Closed")
            open_details_start = text.rindex("<details", 0, open_pos)
            closed_details_start = text.rindex("<details", 0, closed_pos)
            assert "open" in text[open_details_start:open_pos]
            assert "open" not in text[closed_details_start:closed_pos]
        finally:
            await client.close()
    asyncio.run(run())


def test_get_api_tree_shape():
    async def run():
        client, _ = await _client_for()
        try:
            resp = await client.get("/api/tree")
            assert resp.status == 200
            body = await resp.json()
            ids = {d["qualified_id"] for d in body["roots"]}
            assert ids == {"lamp", "dimmer", "fan", "sensor"}
        finally:
            await client.close()
    asyncio.run(run())


def test_get_widget_reflects_current_committed_state():
    async def run():
        flat = _flat()
        client, _ = await _client_for(flat=flat)
        try:
            resp = await client.get("/widget/dimmer/level")
            text = await resp.text()
            assert 'value="0"' in text  # unset -> slider falls back to min (0)
            assert '<span class="widget-value"></span>' in text  # no committed value yet

            await _commit(flat["dimmer"], "level", 42)
            resp = await client.get("/widget/dimmer/level")
            text = await resp.text()
            assert "42 %" in text
            assert 'value="42"' in text
        finally:
            await client.close()
    asyncio.run(run())


def test_post_api_set_writes_value_visible_after_next_commit():
    async def run():
        flat = _flat()
        client, _ = await _client_for(flat=flat)
        try:
            resp = await client.post("/api/set", data={"device": "lamp", "endpoint": "state", "text": "true"})
            assert resp.status == 204
            assert flat["lamp"].get("state") is None  # not yet observable (two-phase model)

            await flat["lamp"].fetch()
            flat["lamp"].update_state()
            assert flat["lamp"].get("state") is True
        finally:
            await client.close()
    asyncio.run(run())


def test_post_api_set_dropdown_posts_label_text_and_resolves_via_from_text():
    async def run():
        flat = _flat()
        client, _ = await _client_for(flat=flat)
        try:
            resp = await client.post("/api/set", data={"device": "fan", "endpoint": "speed", "text": "high"})
            assert resp.status == 204
            await flat["fan"].fetch()
            flat["fan"].update_state()
            assert flat["fan"].get("speed") == 2
        finally:
            await client.close()
    asyncio.run(run())


def test_post_api_set_read_only_endpoint_forbidden():
    async def run():
        client, _ = await _client_for()
        try:
            resp = await client.post("/api/set", data={"device": "sensor", "endpoint": "temp", "text": "20"})
            assert resp.status == 403
        finally:
            await client.close()
    asyncio.run(run())


def test_post_api_set_malformed_body_bad_request():
    async def run():
        client, _ = await _client_for()
        try:
            resp = await client.post("/api/set", data={"device": "lamp"})  # missing endpoint/text
            assert resp.status == 400
        finally:
            await client.close()
    asyncio.run(run())


def test_post_api_set_unknown_device_not_found():
    async def run():
        client, _ = await _client_for()
        try:
            resp = await client.post("/api/set", data={"device": "nope", "endpoint": "x", "text": "1"})
            assert resp.status == 404
        finally:
            await client.close()
    asyncio.run(run())


def test_post_api_set_unknown_endpoint_not_found():
    async def run():
        client, _ = await _client_for()
        try:
            resp = await client.post("/api/set", data={"device": "lamp", "endpoint": "nope", "text": "1"})
            assert resp.status == 404
        finally:
            await client.close()
    asyncio.run(run())


# ---------- real on_start()/on_stop() lifecycle (not just TestClient) ----------

def test_on_start_binds_a_real_port_and_on_stop_releases_it():
    async def run():
        flat = _flat()
        instance = configure(_base_params(port=0), flat, "web_ui.demo")
        await instance.on_start(flat)
        try:
            port = instance._runner.addresses[0][1]
            async with ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/page/home") as resp:
                    assert resp.status == 200
        finally:
            await instance.on_stop(flat)

        with pytest.raises(Exception):
            async with ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/page/home", timeout=1) as resp:
                    pass
    asyncio.run(run())
