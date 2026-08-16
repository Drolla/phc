"""Async OpenMeteoDevice: receive_async() reads Open-Meteo's JSON forecast
response over aiohttp, awaited on the event loop (no worker thread). These
tests drive it through the real async HTTP path against a throwaway local
HTTP server, so they cover the aiohttp fetch, the JSON field extraction, the
failure-to-None behavior, and the shared cache_time behavior, end to end via
the Scheduler.
"""

import http.server
import json
import threading
import time

import pytest

from phc.core.endpoint import Endpoint
from phc.core.scheduler import Scheduler
from phc.devices.open_meteo import device as open_meteo_device
from phc.devices.open_meteo.device import OpenMeteoDevice

CURRENT = {
    "temperature_2m": 22.5,
    "relative_humidity_2m": 55,
    "precipitation": 0.0,
    "pressure_msl": 1015.2,
    "wind_speed_10m": 3.4,
    "wind_direction_10m": 210,
    "weather_code": 2,
    "is_day": 1,
}


@pytest.fixture(autouse=True)
def _clear_response_cache():
    """The response cache is shared module-level state (by design -- see
    OpenMeteoDevice). Each test already uses a fresh random-port URL so
    cache keys never actually collide, but clearing explicitly keeps these
    tests airtight against that assumption."""
    open_meteo_device._response_cache.clear()
    yield
    open_meteo_device._response_cache.clear()


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
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1/forecast"
    return server, base_url


def _device(base_url, latitude=47.37, longitude=8.55, cache_time=None):
    params = {"base_url": base_url, "latitude": latitude, "longitude": longitude}
    if cache_time is not None:
        params["cache_time"] = cache_time
    return OpenMeteoDevice(
        "weather",
        params=params,
        endpoints=[
            Endpoint("temperature", params={"field": "temperature_2m"}),
            Endpoint("humidity", params={"field": "relative_humidity_2m"}),
        ],
        update_interval=0.0,
    )


def test_open_meteo_fetches_and_parses_over_http():
    server, base_url = _serve(json.dumps({"current": CURRENT}).encode("utf-8"))
    try:
        dev = _device(base_url)
        scheduler = Scheduler({"weather": dev})
        scheduler.tick(now=0.0)   # drives receive_async() -> aiohttp GET -> JSON parse
        scheduler.close()
        assert dev.get("temperature") == 22.5
        assert dev.get("humidity") == 55
    finally:
        server.shutdown()


def test_open_meteo_requests_expected_query_params():
    dev = _device("http://example.invalid/v1/forecast", latitude=1.5, longitude=-2.5)
    assert "latitude=1.5" in dev._url
    assert "longitude=-2.5" in dev._url
    assert "wind_speed_unit=ms" in dev._url


def test_open_meteo_reports_none_on_http_error():
    server, base_url = _serve(b"upstream error", status=500)
    try:
        dev = _device(base_url)
        scheduler = Scheduler({"weather": dev})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert dev.get("temperature") is None
        assert dev.get("humidity") is None
    finally:
        server.shutdown()


def test_open_meteo_reuses_cached_response_within_cache_time():
    server, base_url = _serve(json.dumps({"current": CURRENT}).encode("utf-8"))
    try:
        dev = _device(base_url, cache_time="60s")
        scheduler = Scheduler({"weather": dev})
        scheduler.tick(now=0.0)
        scheduler.tick(now=0.1)  # well within cache_time -- must hit the cache
        scheduler.close()
        assert server.hit_count == 1
        assert dev.get("temperature") == 22.5
    finally:
        server.shutdown()


def test_open_meteo_cache_expires_after_cache_time():
    server, base_url = _serve(json.dumps({"current": CURRENT}).encode("utf-8"))
    try:
        dev = _device(base_url, cache_time="50ms")
        scheduler = Scheduler({"weather": dev})
        scheduler.tick(now=0.0)
        assert server.hit_count == 1
        time.sleep(0.1)  # past cache_time
        scheduler.tick(now=1.0)
        scheduler.close()
        assert server.hit_count == 2
        assert dev.get("temperature") == 22.5
    finally:
        server.shutdown()


def test_open_meteo_shares_cache_across_two_device_instances_at_same_location():
    server, base_url = _serve(json.dumps({"current": CURRENT}).encode("utf-8"))
    try:
        dev_a = _device(base_url, cache_time="60s")
        dev_b = _device(base_url, cache_time="60s")
        # Both devices are due in the same tick, so the Scheduler gathers
        # their fetches concurrently -- exercising the cache lock's
        # double-checked locking, not just sequential reuse.
        scheduler = Scheduler({"a": dev_a, "b": dev_b})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert server.hit_count == 1
        assert dev_a.get("temperature") == 22.5
        assert dev_b.get("temperature") == 22.5
    finally:
        server.shutdown()


def test_open_meteo_failed_fetch_does_not_populate_cache():
    server, base_url = _serve(b"upstream error", status=500)
    try:
        dev = _device(base_url, cache_time="60s")
        scheduler = Scheduler({"weather": dev})
        scheduler.tick(now=0.0)
        assert dev.get("temperature") is None
        assert server.hit_count == 1
        scheduler.tick(now=0.1)  # cache was never populated by the failure -- retries
        scheduler.close()
        assert server.hit_count == 2
    finally:
        server.shutdown()
