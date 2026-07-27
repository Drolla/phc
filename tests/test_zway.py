"""Async ZWayDevice: receive_async()/transmit_async() talk to a Razberry/
zWay controller's thc_zWay.js helper over aiohttp (GET /JS/Run/<expr>,
POST /ZAutomation/api/v1/login). These tests drive it through the real
async HTTP path against a throwaway local HTTP server, so they cover the
combined-Get batching across sibling devices sharing one base_url, the
per-endpoint None-on-failure behavior, cache reuse/expiry/invalidation-on-
write, the login/cookie flow (including re-login on an expired session),
and the one-time TagReader configure call.
"""

import asyncio
import http.server
import json
import re
import threading
import time
import urllib.parse

import pytest

from core.endpoint import Endpoint
from core.scheduler import Scheduler
from core.task import SetAction, Task
from devices.zway import device as zway_device
from devices.zway.device import ZWayDevice


@pytest.fixture(autouse=True)
def _clear_module_state():
    """All zway module-level state is process-global by design (see
    ZWayDevice) -- clear it between tests. The two asyncio.Lock()s are also
    replaced, not just left alone: each test builds its own Scheduler (->
    its own event loop), and asyncio.Lock only binds to a specific loop
    lazily on first genuinely-contended acquire(), so a lock left over from
    a previous test's already-closed loop would fail cross-test with "bound
    to a different event loop" the moment two sibling devices actually
    contend for it -- same hazard tests/test_waveplus_bridge.py documents
    for its own cache lock."""
    zway_device._identifiers.clear()
    zway_device._response_cache.clear()
    zway_device._response_cache_lock = asyncio.Lock()
    zway_device._session_cookies.clear()
    zway_device._session_lock = asyncio.Lock()
    zway_device._configured_tag_readers.clear()
    yield
    zway_device._identifiers.clear()
    zway_device._response_cache.clear()
    zway_device._session_cookies.clear()
    zway_device._configured_tag_readers.clear()


def _parse_get_args(path: str) -> list:
    """Pull the [[group, address], ...] argument list out of a
    .../JS/Run/Get([...]) request path."""
    expr = urllib.parse.unquote(path.split("/JS/Run/", 1)[1])
    match = re.fullmatch(r"Get\((.*)\)", expr)
    return json.loads(match.group(1))


def _serve(*, login_ok: bool = True, get_response=None, get_status: int = 200,
           require_cookie: str | None = None):
    """Start a throwaway local HTTP server faking a zWay controller running
    thc_zWay.js. `get_response` is either a fixed JSON-able value returned
    for every Get() call, or a callable(idents) -> JSON-able value computed
    from the requested identifier list (for order-preserving-decode
    checks). `require_cookie`, if set, means /JS/Run/... only succeeds when
    the request carries that exact Cookie header (else 401) -- used to
    exercise the login/re-login flow. Returns (server, base_url); call
    server.shutdown() when done. The server's `get_hits`/`login_hits`
    attributes count requests, for cache/batching assertions.
    """
    class Handler(http.server.BaseHTTPRequestHandler):
        def _send_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path == "/ZAutomation/api/v1/login":
                self.server.login_hits += 1
                if not login_ok:
                    self._send_json(403, {"error": "bad credentials"})
                    return
                self.send_response(200)
                self.send_header("Set-Cookie", "ZWAYSession=abc123; Path=/")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            if require_cookie is not None and self.headers.get("Cookie") != require_cookie:
                self._send_json(401, {"error": "unauthorized"})
                return
            if "/JS/Run/Configure_TagReader(" in self.path:
                self.server.configure_hits += 1
                self._send_json(get_status, [])
                return
            if "/JS/Run/Set(" in self.path:
                self.server.set_hits += 1
                self._send_json(get_status, [1])
                return
            self.server.get_hits += 1
            idents = _parse_get_args(self.path)
            if callable(get_response):
                payload = get_response(idents)
            elif get_response is not None:
                payload = get_response
            else:
                payload = [0] * len(idents)
            self._send_json(get_status, payload)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.get_hits = 0
    server.set_hits = 0
    server.login_hits = 0
    server.configure_hits = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, base_url


def _switch_endpoints():
    return [Endpoint("state", writable=True,
                     parameters={"command_group": "SwitchBinary", "address": "20.1"})]


def _sensor_endpoints():
    return [
        Endpoint("state", parameters={"command_group": "SensorBinary", "address": "26"}),
        Endpoint("battery", parameters={"command_group": "Battery", "address": "26.0"}),
    ]


def _tagreader_endpoints():
    return [Endpoint("state", parameters={
        "command_group": "TagReader", "address": "22", "node_id": "22"})]


def _device(base_url, device_id="dev", endpoints=None, **params):
    return ZWayDevice(device_id, params={"base_url": base_url, **params},
                       endpoints=endpoints if endpoints is not None else _switch_endpoints(),
                       update_interval=0.0)


def test_zway_fetches_single_device():
    server, base_url = _serve(get_response=[1])
    try:
        dev = _device(base_url)
        scheduler = Scheduler({"light": dev})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert dev.get("state") == 1
    finally:
        server.shutdown()


def test_zway_batches_three_sibling_devices_into_one_get():
    server, base_url = _serve(get_response=lambda idents: list(range(len(idents))))
    try:
        light = _device(base_url, "light")
        sensor = _device(base_url, "sensor", endpoints=_sensor_endpoints())
        tag = _device(base_url, "tag", endpoints=_tagreader_endpoints())
        scheduler = Scheduler({"light": light, "sensor": sensor, "tag": tag})
        scheduler.tick(now=0.0)
        scheduler.close()
        # One combined Get() covering all three devices, not three separate Gets.
        assert server.get_hits == 1
        assert server.configure_hits == 1
        assert light.get("state") == 0
        assert sensor.get("state") == 1
        assert sensor.get("battery") == 2
    finally:
        server.shutdown()


def test_zway_response_order_maps_back_to_identifiers():
    def responder(idents):
        # Echo each identifier's address as a number, proving positional
        # decode uses the actual request order, not declaration order.
        return [float(address) for _group, address in idents]

    server, base_url = _serve(get_response=responder)
    try:
        sensor = _device(base_url, "sensor", endpoints=_sensor_endpoints())
        scheduler = Scheduler({"sensor": sensor})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert sensor.get("state") == 26.0
        assert sensor.get("battery") == 26.0
    finally:
        server.shutdown()


def test_zway_reports_none_on_http_error():
    server, base_url = _serve(get_status=500, get_response=[1])
    try:
        dev = _device(base_url)
        scheduler = Scheduler({"light": dev})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert dev.get("state") is None
    finally:
        server.shutdown()


def test_zway_reports_none_on_short_response_array():
    server, base_url = _serve(get_response=[])   # sensor expects 2 values, gets 0
    try:
        sensor = _device(base_url, "sensor", endpoints=_sensor_endpoints())
        scheduler = Scheduler({"sensor": sensor})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert sensor.get("state") is None
        assert sensor.get("battery") is None
    finally:
        server.shutdown()


def test_zway_failed_fetch_does_not_populate_cache():
    server, base_url = _serve(get_status=500, get_response=[1])
    try:
        dev = _device(base_url, cache_time="60s")
        scheduler = Scheduler({"light": dev})
        scheduler.tick(now=0.0)
        assert dev.get("state") is None
        assert server.get_hits == 1
        scheduler.tick(now=0.1)   # cache was never populated by the failure -- retries
        scheduler.close()
        assert server.get_hits == 2
    finally:
        server.shutdown()


def test_zway_reuses_cached_response_within_cache_time():
    server, base_url = _serve(get_response=[1])
    try:
        dev = _device(base_url, cache_time="60s")
        scheduler = Scheduler({"light": dev})
        scheduler.tick(now=0.0)
        scheduler.tick(now=0.1)   # well within cache_time -- must hit the cache
        scheduler.close()
        assert server.get_hits == 1
        assert dev.get("state") == 1
    finally:
        server.shutdown()


def test_zway_cache_expires_after_cache_time():
    server, base_url = _serve(get_response=[1])
    try:
        dev = _device(base_url, cache_time="50ms")
        scheduler = Scheduler({"light": dev})
        scheduler.tick(now=0.0)
        assert server.get_hits == 1
        import time
        time.sleep(0.1)   # past cache_time
        scheduler.tick(now=1.0)
        scheduler.close()
        assert server.get_hits == 2
    finally:
        server.shutdown()


def test_zway_write_issues_one_set_and_invalidates_cache():
    # A write only ever reaches transmit_async() via the Scheduler's own
    # write-collector path (a task action within a tick) -- ZWayDevice, like
    # any native-async device, only overrides transmit_async(), not the
    # sync transmit() that a direct dev.set() outside a tick would call
    # instead (see core.device.Device._emit/core/scheduler.py's write
    # collector; test_scheduler_async.py's SleepWriteDevice is the only
    # existing device that overrides plain transmit(), so it's the one
    # that direct dev.set() actually exercises).
    server, base_url = _serve(get_response=[0])
    try:
        dev = _device(base_url, cache_time="60s")
        task = Task("turn_on", due_time=0.0,
                     actions=[SetAction(device_id="light", endpoint_key="state", value=1)])
        scheduler = Scheduler({"light": dev}, tasks=[task])
        scheduler.tick(now=0.0)   # fetch (Get) + task fires + write flushed (Set), same tick
        assert dev.get("state") == 0
        assert server.get_hits == 1
        assert server.set_hits == 1
        scheduler.tick(now=0.1)   # within cache_time, but the write invalidated it -- re-Get
        scheduler.close()
        assert server.get_hits == 2
    finally:
        server.shutdown()


def test_zway_write_ignores_unknown_endpoint_key():
    server, base_url = _serve(get_response=[0])
    try:
        dev = _device(base_url)
        # A stray write for a key this device doesn't know about must be a
        # no-op, not an error -- exercise transmit_async() directly since a
        # Device.set() call always validates the key against self.endpoints
        # first (see Device.set()/_resolve()), so this path is otherwise
        # unreachable through the public API.
        asyncio.run(dev.transmit_async({"missing": 1}))
        assert server.set_hits == 0
    finally:
        server.shutdown()


def test_zway_configure_tag_reader_fires_once():
    server, base_url = _serve(get_response=[1])
    try:
        tag = _device(base_url, "tag", endpoints=_tagreader_endpoints(), cache_time="50ms")
        scheduler = Scheduler({"tag": tag})
        scheduler.tick(now=0.0)
        time.sleep(0.1)   # past cache_time -- forces a second Get, but not a second configure
        scheduler.tick(now=1.0)
        scheduler.close()
        assert server.configure_hits == 1
        assert server.get_hits == 2
    finally:
        server.shutdown()


def test_zway_login_and_reuses_cookie():
    server, base_url = _serve(get_response=[1], require_cookie="ZWAYSession=abc123")
    try:
        dev = _device(base_url, user="admin", password="admin", cache_time="0s")
        scheduler = Scheduler({"light": dev})
        scheduler.tick(now=0.0)
        scheduler.tick(now=0.1)
        scheduler.close()
        assert dev.get("state") == 1
        assert server.login_hits == 1   # cookie reused across ticks, not re-logged-in
        assert server.get_hits == 2
    finally:
        server.shutdown()


def test_zway_relogs_in_on_expired_cookie():
    """The server only accepts the CURRENT cookie; force one expiry by
    rotating the required cookie after the first login, so the module must
    detect the 401 and re-login."""
    calls = {"n": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            calls["n"] += 1
            cookie = f"ZWAYSession=session{calls['n']}"
            self.send_response(200)
            self.send_header("Set-Cookie", f"{cookie}; Path=/")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_GET(self):
            # Only the FIRST issued cookie is considered already-expired;
            # any subsequent (re-logged-in) cookie is accepted.
            if self.headers.get("Cookie") == "ZWAYSession=session1":
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.server.get_hits += 1
            idents = _parse_get_args(self.path)
            body = json.dumps([1] * len(idents)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.get_hits = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        dev = _device(base_url, user="admin", password="admin")
        scheduler = Scheduler({"light": dev})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert dev.get("state") == 1
        assert calls["n"] == 2   # first login (expired), one re-login after the 401
    finally:
        server.shutdown()
