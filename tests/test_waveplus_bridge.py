"""Async WavePlusBridge device: receive_async() reads a WavePlus_Bridge JSON
payload over aiohttp, awaited on the event loop (no worker thread). These
tests drive it through the real async HTTP path against a throwaway local
HTTP server, so they cover the aiohttp fetch, the per-sensor JSON selection,
the failure-to-None behavior, the per-sensor staleness gate, and the shared
cache_time behavior, end to end via the Scheduler.
"""

import asyncio
import http.server
import json
import threading
import time

import pytest

from core.endpoint import Endpoint
from core.scheduler import Scheduler
from devices.waveplus_bridge import device as waveplus_bridge_device
from devices.waveplus_bridge.device import WavePlusBridgeDevice

# cellar_office is fresh (50s old); cellar_room is stale (500s old) relative
# to current_time, given the default data_validity_time of 5m (300s).
PAYLOAD = {
    "current_time": 1000,
    "devices": {
        "cellar_office": {
            "update_time": 950,
            "temperature": 19.85, "humidity": 43.5, "pressure": 900.68,
            "radon_st": 69, "radon_lt": 36, "co2": 1390.0, "voc": 465.0,
        },
        "cellar_room": {
            "update_time": 500,
            "temperature": 18.2, "humidity": 40.0, "pressure": 900.1,
            "radon_st": 20, "radon_lt": 18, "co2": 800.0, "voc": 200.0,
        },
    },
}

def _endpoints():
    """Fresh Endpoint objects for one device -- must not be shared across
    device instances (Endpoint holds per-device mutable state), especially
    in tests that co-schedule two devices in the same tick."""
    return [
        Endpoint("temperature", parameters={"field": "temperature"}),
        Endpoint("radon_st", parameters={"field": "radon_st"}),
        Endpoint("co2", parameters={"field": "co2"}),
    ]


@pytest.fixture(autouse=True)
def _clear_response_cache():
    """The response cache is shared module-level state (by design -- see
    WavePlusBridgeDevice). Each test already uses a fresh random-port URL so
    cache keys never actually collide, but clearing explicitly keeps these
    tests airtight against that assumption.

    The cache's guarding asyncio.Lock is also module-level, and each test
    builds its own Scheduler (-> its own event loop). asyncio.Lock only
    binds to a specific event loop lazily, the first time acquire() is
    genuinely contended (not on every acquire's fast path) -- so two
    *different* tests that both co-schedule two devices sharing one
    base_url (real lock contention) would otherwise collide: the second
    such test inherits a lock still bound to the first test's already-closed
    loop and fails with "bound to a different event loop". Replacing the
    lock with a fresh one alongside the cache keeps every test isolated."""
    waveplus_bridge_device._response_cache.clear()
    waveplus_bridge_device._response_cache_lock = asyncio.Lock()
    yield
    waveplus_bridge_device._response_cache.clear()


def _serve(body: bytes, status: int = 200):
    """Start a throwaway local HTTP server returning `body`/`status` for any
    GET. Returns (server, base_url); call server.shutdown() when done. The
    server's `hit_count` attribute counts GET requests received, for tests
    that verify caching did (or did not) avoid a re-fetch."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.hit_count += 1
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.hit_count = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/data"
    return server, base_url


def _device(base_url, sensor_id="cellar_office", cache_time=None,
            data_validity_time=None, endpoints=None):
    params = {"base_url": base_url, "sensor_id": sensor_id}
    if cache_time is not None:
        params["cache_time"] = cache_time
    if data_validity_time is not None:
        params["data_validity_time"] = data_validity_time
    return WavePlusBridgeDevice(
        sensor_id,
        params=params,
        endpoints=endpoints if endpoints is not None else _endpoints(),
        update_interval=0.0,
    )


def test_waveplus_fetches_and_parses_over_http():
    server, base_url = _serve(json.dumps(PAYLOAD).encode("utf-8"))
    try:
        dev = _device(base_url, sensor_id="cellar_office")
        scheduler = Scheduler({"office": dev})
        scheduler.tick(now=0.0)   # drives receive_async() -> aiohttp GET -> JSON parse
        scheduler.close()
        assert dev.get("temperature") == 19.85
        assert dev.get("radon_st") == 69
        assert dev.get("co2") == 1390.0
    finally:
        server.shutdown()


def test_waveplus_reports_none_on_http_error():
    server, base_url = _serve(b"upstream error", status=500)
    try:
        dev = _device(base_url)
        scheduler = Scheduler({"office": dev})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert dev.get("temperature") is None
        assert dev.get("radon_st") is None
    finally:
        server.shutdown()


def test_waveplus_reports_none_when_sensor_id_missing_from_payload():
    server, base_url = _serve(json.dumps(PAYLOAD).encode("utf-8"))
    try:
        dev = _device(base_url, sensor_id="attic")   # not in PAYLOAD["devices"]
        scheduler = Scheduler({"attic": dev})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert dev.get("temperature") is None
        assert dev.get("radon_st") is None
    finally:
        server.shutdown()


def test_waveplus_reports_none_when_sensor_stale():
    server, base_url = _serve(json.dumps(PAYLOAD).encode("utf-8"))
    try:
        # Default data_validity_time (5m/300s): cellar_room is 500s old (stale),
        # cellar_office is 50s old (fresh) -- same fetch, same tick.
        office = _device(base_url, sensor_id="cellar_office")
        room = _device(base_url, sensor_id="cellar_room")
        scheduler = Scheduler({"office": office, "room": room})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert office.get("temperature") == 19.85
        assert room.get("temperature") is None
        assert room.get("radon_st") is None
    finally:
        server.shutdown()


def test_waveplus_reports_values_when_sensor_within_validity():
    server, base_url = _serve(json.dumps(PAYLOAD).encode("utf-8"))
    try:
        # cellar_room is 500s old -- within a generously widened validity window.
        dev = _device(base_url, sensor_id="cellar_room", data_validity_time="10m")
        scheduler = Scheduler({"room": dev})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert dev.get("temperature") == 18.2
        assert dev.get("radon_st") == 20
    finally:
        server.shutdown()


def test_waveplus_tolerates_missing_timestamps():
    payload = {
        "devices": {
            "cellar_office": {
                "temperature": 19.85, "radon_st": 69, "co2": 1390.0,
            },
        },
    }
    server, base_url = _serve(json.dumps(payload).encode("utf-8"))
    try:
        dev = _device(base_url, sensor_id="cellar_office")
        scheduler = Scheduler({"office": dev})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert dev.get("temperature") == 19.85
        assert dev.get("radon_st") == 69
    finally:
        server.shutdown()


def test_waveplus_reports_none_for_missing_field_on_fresh_sensor():
    payload = {
        "current_time": 1000,
        "devices": {
            "cellar_office": {
                "update_time": 950,
                "temperature": 19.85, "radon_st": 69,
                # no "co2" field -- e.g. an older Wave Plus variant
            },
        },
    }
    server, base_url = _serve(json.dumps(payload).encode("utf-8"))
    try:
        dev = _device(base_url, sensor_id="cellar_office")
        scheduler = Scheduler({"office": dev})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert dev.get("temperature") == 19.85
        assert dev.get("radon_st") == 69
        assert dev.get("co2") is None
    finally:
        server.shutdown()


def test_waveplus_reuses_cached_response_within_cache_time():
    server, base_url = _serve(json.dumps(PAYLOAD).encode("utf-8"))
    try:
        dev = _device(base_url, cache_time="60s")
        scheduler = Scheduler({"office": dev})
        scheduler.tick(now=0.0)
        scheduler.tick(now=0.1)  # well within cache_time -- must hit the cache
        scheduler.close()
        assert server.hit_count == 1
        assert dev.get("temperature") == 19.85
    finally:
        server.shutdown()


def test_waveplus_cache_expires_after_cache_time():
    server, base_url = _serve(json.dumps(PAYLOAD).encode("utf-8"))
    try:
        dev = _device(base_url, cache_time="50ms")
        scheduler = Scheduler({"office": dev})
        scheduler.tick(now=0.0)
        assert server.hit_count == 1
        time.sleep(0.1)  # past cache_time
        scheduler.tick(now=1.0)
        scheduler.close()
        assert server.hit_count == 2
        assert dev.get("temperature") == 19.85
    finally:
        server.shutdown()


def test_waveplus_shares_cache_across_two_sensors_on_same_bridge():
    server, base_url = _serve(json.dumps(PAYLOAD).encode("utf-8"))
    try:
        office = _device(base_url, sensor_id="cellar_office", cache_time="60s")
        room = _device(base_url, sensor_id="cellar_room", cache_time="60s",
                        data_validity_time="10m")  # keep room fresh for this test
        # Both devices are due in the same tick, so the Scheduler gathers
        # their fetches concurrently -- exercising the cache lock's
        # double-checked locking, not just sequential reuse.
        scheduler = Scheduler({"office": office, "room": room})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert server.hit_count == 1
        assert office.get("temperature") == 19.85
        assert room.get("temperature") == 18.2
    finally:
        server.shutdown()


def test_waveplus_failed_fetch_does_not_populate_cache():
    server, base_url = _serve(b"upstream error", status=500)
    try:
        dev = _device(base_url, cache_time="60s")
        scheduler = Scheduler({"office": dev})
        scheduler.tick(now=0.0)
        assert dev.get("temperature") is None
        assert server.hit_count == 1
        scheduler.tick(now=0.1)  # cache was never populated by the failure -- retries
        scheduler.close()
        assert server.hit_count == 2
    finally:
        server.shutdown()
