"""Async MeteoSwiss device: receive_async() reads the SwissMetNet CSV over
aiohttp, awaited on the event loop (no worker thread). These tests drive it
through the real async HTTP path against a throwaway local HTTP server, so they
cover the aiohttp fetch, the CSV/station parsing, the failure-to-None
behavior, and the shared cache_time behavior, end to end via the Scheduler.
"""

import http.server
import threading
import time

import pytest

from phc.core.endpoint import Endpoint
from phc.core.scheduler import Scheduler
from phc.devices.meteoswiss import device as meteoswiss_device
from phc.devices.meteoswiss.device import MeteoSwissDevice

CSV = (
    "Station/Location;tre200s0;ure200s0\n"
    "BER;12.3;65\n"
    "ZRH;9.9;80\n"
)


@pytest.fixture(autouse=True)
def _clear_csv_cache():
    """The CSV cache is shared module-level state (by design -- see
    MeteoSwissDevice). Each test already uses a fresh random-port URL so
    cache keys never actually collide, but clearing explicitly keeps these
    tests airtight against that assumption."""
    meteoswiss_device._csv_cache.clear()
    yield
    meteoswiss_device._csv_cache.clear()


def _serve(body: bytes, status: int = 200):
    """Start a throwaway local HTTP server returning `body`/`status` for any
    GET. Returns (server, url); call server.shutdown() when done. The
    server's `hit_count` attribute counts GET requests received, for tests
    that verify caching did (or did not) avoid a re-fetch."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.hit_count += 1
            self.send_response(status)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.hit_count = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/VQHA80.csv"
    return server, url


def _device(url, station="BER", cache_time=None):
    params = {"data_url": url, "station": station}
    if cache_time is not None:
        params["cache_time"] = cache_time
    return MeteoSwissDevice(
        "weather",
        params=params,
        endpoints=[
            Endpoint("temperature", params={"column": "tre200s0"}),
            Endpoint("humidity", params={"column": "ure200s0"}),
        ],
        update_interval=0.0,
    )


def test_meteoswiss_fetches_and_parses_over_http():
    server, url = _serve(CSV.encode("utf-8"))
    try:
        dev = _device(url)
        scheduler = Scheduler({"weather": dev})
        scheduler.tick(now=0.0)   # drives receive_async() -> aiohttp GET -> parse
        scheduler.close()
        assert dev.get("temperature") == 12.3   # BER's tre200s0 column
        assert dev.get("humidity") == 65.0       # BER's ure200s0 column
    finally:
        server.shutdown()


def test_meteoswiss_reports_none_on_http_error():
    server, url = _serve(b"upstream error", status=500)
    try:
        dev = _device(url)
        scheduler = Scheduler({"weather": dev})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert dev.get("temperature") is None
        assert dev.get("humidity") is None
    finally:
        server.shutdown()


def test_meteoswiss_reuses_cached_csv_within_cache_time():
    server, url = _serve(CSV.encode("utf-8"))
    try:
        dev = _device(url, cache_time="60s")
        scheduler = Scheduler({"weather": dev})
        scheduler.tick(now=0.0)
        scheduler.tick(now=0.1)  # well within cache_time -- must hit the cache
        scheduler.close()
        assert server.hit_count == 1
        assert dev.get("temperature") == 12.3
    finally:
        server.shutdown()


def test_meteoswiss_cache_expires_after_cache_time():
    server, url = _serve(CSV.encode("utf-8"))
    try:
        dev = _device(url, cache_time="50ms")
        scheduler = Scheduler({"weather": dev})
        scheduler.tick(now=0.0)
        assert server.hit_count == 1
        time.sleep(0.1)  # past cache_time
        scheduler.tick(now=1.0)
        scheduler.close()
        assert server.hit_count == 2
        assert dev.get("temperature") == 12.3
    finally:
        server.shutdown()


def test_meteoswiss_shares_cache_across_two_device_instances():
    server, url = _serve(CSV.encode("utf-8"))
    try:
        dev_ber = _device(url, station="BER", cache_time="60s")
        dev_zrh = _device(url, station="ZRH", cache_time="60s")
        # Both devices are due in the same tick, so the Scheduler gathers
        # their fetches concurrently -- exercising the cache lock's
        # double-checked locking, not just sequential reuse.
        scheduler = Scheduler({"ber": dev_ber, "zrh": dev_zrh})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert server.hit_count == 1
        assert dev_ber.get("temperature") == 12.3
        assert dev_zrh.get("temperature") == 9.9
    finally:
        server.shutdown()


def test_meteoswiss_failed_fetch_does_not_populate_cache():
    server, url = _serve(b"upstream error", status=500)
    try:
        dev = _device(url, cache_time="60s")
        scheduler = Scheduler({"weather": dev})
        scheduler.tick(now=0.0)
        assert dev.get("temperature") is None
        assert server.hit_count == 1
        scheduler.tick(now=0.1)  # cache was never populated by the failure -- retries
        scheduler.close()
        assert server.hit_count == 2
    finally:
        server.shutdown()
