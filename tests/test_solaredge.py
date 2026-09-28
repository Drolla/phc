"""Async SolarEdge device: receive_async() reads overview.json and
powerDetails.json over aiohttp, awaited on the event loop (no worker
thread). These tests drive it through the real async HTTP path against a
throwaway local HTTP server, so they cover the aiohttp fetch, the JSON
extraction, the failure-to-None behavior, and the shared cache_time
behavior, end to end via the Scheduler.
"""

import http.server
import json
import threading

import pytest

import phc.devices.solaredge.device as solaredge_module
from phc.core.endpoint import Endpoint
from phc.core.scheduler import Scheduler
from phc.devices.solaredge.device import SolarEdgeDevice

OVERVIEW = {
    "overview": {
        "currentPower": {"power": 131.0},
        "lifeTimeData": {"energy": 36200268.0},
    }
}

POWER_DETAILS = {
    "powerDetails": {
        "meters": [
            {
                "type": "Production",
                "values": [
                    {"date": "2026-09-27 17:45:00", "value": 143.33333},
                    {"date": "2026-09-27 18:00:00", "value": 121.0},
                ],
            },
            {
                "type": "Purchased",
                "values": [
                    {"date": "2026-09-27 17:45:00", "value": 228.05678},
                    {"date": "2026-09-27 18:00:00"},  # latest bucket not yet posted
                ],
            },
        ]
    }
}


@pytest.fixture(autouse=True)
def _restore_base_url():
    """Restore the module's real API host after each test.

    _device() overwrites _BASE_URL at module scope to redirect requests
    to a test's local fake server; without this, that mutation would leak
    into whichever test runs next.
    """
    original = solaredge_module._BASE_URL
    yield
    solaredge_module._BASE_URL = original


def _serve(responses: dict[str, tuple[int, dict]]):
    """Start a throwaway local HTTP server routing by URL path.

    `responses` maps a path (e.g. "/site/123/overview.json") to
    (status, body_dict). Returns (server, base_url); call
    server.shutdown() when done. server.hit_counts[path] counts GETs
    per path, for tests that verify caching did (or did not) avoid a
    re-fetch.
    """
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            self.server.hit_counts[path] = self.server.hit_counts.get(path, 0) + 1
            status, body = responses.get(path, (404, {"error": "not found"}))
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.hit_counts = {}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, base_url


def _device(base_url, site_id="123", cache_time=None, context=None):
    """One SolarEdgeDevice pointed at a fake API server.

    `base_url` replaces the module's real API host for the duration of
    the calling test (see the autouse `_redirect_base_url` fixture) --
    SolarEdge's monitoring API has one fixed host, so there is no
    device/module parameter for it to override in production.

    `context` is the scratch dict load_system() would normally hand every
    device of one system (see Device.context); the response caches live
    there, so two devices only share them when they are passed the SAME
    dict. Omitted, each device gets its own -- an isolated cache per
    device.
    """
    solaredge_module._BASE_URL = base_url
    params = {"api_key": "test-key", "site_id": site_id}
    if cache_time is not None:
        params["cache_time"] = cache_time
    return SolarEdgeDevice(
        "site",
        params=params,
        endpoints=[
            Endpoint("current_power", params={"source": "overview", "field": "currentPower.power"}),
            Endpoint("lifetime_energy", params={"source": "overview", "field": "lifeTimeData.energy"}),
            Endpoint("production", params={"source": "power_details", "meter": "Production"}),
            Endpoint("purchased", params={"source": "power_details", "meter": "Purchased"}),
        ],
        update_interval=0.0,
        context=context,
    )


def _paths(site_id="123"):
    return f"/site/{site_id}/overview.json", f"/site/{site_id}/powerDetails.json"


def test_solaredge_fetches_and_parses_over_http():
    overview_path, power_details_path = _paths()
    server, base_url = _serve({
        overview_path: (200, OVERVIEW),
        power_details_path: (200, POWER_DETAILS),
    })
    try:
        dev = _device(base_url)
        scheduler = Scheduler({"site": dev})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert dev.get("current_power") == 131.0
        assert dev.get("lifetime_energy") == 36200268.0
        assert dev.get("production") == 121.0  # latest bucket
    finally:
        server.shutdown()


def test_solaredge_falls_back_to_earlier_bucket_when_latest_is_missing():
    overview_path, power_details_path = _paths()
    server, base_url = _serve({
        overview_path: (200, OVERVIEW),
        power_details_path: (200, POWER_DETAILS),
    })
    try:
        dev = _device(base_url)
        scheduler = Scheduler({"site": dev})
        scheduler.tick(now=0.0)
        scheduler.close()
        # Purchased's latest bucket has no "value" key at all -- must fall
        # back to the prior bucket rather than reporting None or 0.
        assert dev.get("purchased") == 228.05678
    finally:
        server.shutdown()


def test_solaredge_reports_none_on_power_details_error_but_keeps_overview():
    overview_path, power_details_path = _paths()
    server, base_url = _serve({
        overview_path: (200, OVERVIEW),
        power_details_path: (500, {"error": "upstream error"}),
    })
    try:
        dev = _device(base_url)
        scheduler = Scheduler({"site": dev})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert dev.get("current_power") == 131.0
        assert dev.get("production") is None
        assert dev.get("purchased") is None
    finally:
        server.shutdown()


def test_solaredge_reports_none_on_overview_error_but_keeps_power_details():
    overview_path, power_details_path = _paths()
    server, base_url = _serve({
        overview_path: (500, {"error": "upstream error"}),
        power_details_path: (200, POWER_DETAILS),
    })
    try:
        dev = _device(base_url)
        scheduler = Scheduler({"site": dev})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert dev.get("current_power") is None
        assert dev.get("lifetime_energy") is None
        assert dev.get("production") == 121.0
    finally:
        server.shutdown()


def test_solaredge_reuses_cached_responses_within_cache_time():
    overview_path, power_details_path = _paths()
    server, base_url = _serve({
        overview_path: (200, OVERVIEW),
        power_details_path: (200, POWER_DETAILS),
    })
    try:
        dev = _device(base_url, cache_time="60s")
        scheduler = Scheduler({"site": dev})
        scheduler.tick(now=0.0)
        scheduler.tick(now=0.1)  # well within cache_time -- must hit the cache
        scheduler.close()
        assert server.hit_counts[overview_path] == 1
        assert server.hit_counts[power_details_path] == 1
        assert dev.get("current_power") == 131.0
    finally:
        server.shutdown()


def test_solaredge_cache_expires_after_cache_time():
    overview_path, power_details_path = _paths()
    server, base_url = _serve({
        overview_path: (200, OVERVIEW),
        power_details_path: (200, POWER_DETAILS),
    })
    try:
        dev = _device(base_url, cache_time="0s")
        scheduler = Scheduler({"site": dev})
        scheduler.tick(now=0.0)
        scheduler.tick(now=1.0)
        scheduler.close()
        assert server.hit_counts[overview_path] == 2
        assert server.hit_counts[power_details_path] == 2
    finally:
        server.shutdown()


def test_solaredge_shares_cache_across_two_device_instances():
    overview_path, power_details_path = _paths()
    server, base_url = _serve({
        overview_path: (200, OVERVIEW),
        power_details_path: (200, POWER_DETAILS),
    })
    try:
        # One shared context, as load_system() gives every device of a
        # system -- that is what puts both devices on the same cache.
        context = {}
        dev_a = _device(base_url, cache_time="60s", context=context)
        dev_b = _device(base_url, cache_time="60s", context=context)
        # Both devices are due in the same tick, so the Scheduler gathers
        # their fetches concurrently -- exercising the cache locks'
        # double-checked locking, not just sequential reuse.
        scheduler = Scheduler({"a": dev_a, "b": dev_b})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert server.hit_counts[overview_path] == 1
        assert server.hit_counts[power_details_path] == 1
        assert dev_a.get("current_power") == 131.0
        assert dev_b.get("current_power") == 131.0
    finally:
        server.shutdown()


def test_solaredge_failed_fetch_does_not_populate_cache():
    overview_path, power_details_path = _paths()
    server, base_url = _serve({
        overview_path: (500, {"error": "upstream error"}),
        power_details_path: (200, POWER_DETAILS),
    })
    try:
        dev = _device(base_url, cache_time="60s")
        scheduler = Scheduler({"site": dev})
        scheduler.tick(now=0.0)
        assert dev.get("current_power") is None
        assert server.hit_counts[overview_path] == 1
        scheduler.tick(now=0.1)  # cache was never populated by the failure -- retries
        scheduler.close()
        assert server.hit_counts[overview_path] == 2
    finally:
        server.shutdown()
