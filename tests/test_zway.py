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

from phc.core.endpoint import Endpoint
from phc.core.scheduler import Scheduler
from phc.core.task import SetAction, Task
from phc.devices.zway.device import ZWayDevice


# The per-system device context every device this module builds shares
# (see phc.core.device.Device.context) -- one System's worth of zway state,
# reset between tests by the fixture below. load_system() creates one of
# these per config; here one per test is the same isolation.
_context: dict = {}


@pytest.fixture(autouse=True)
def _fresh_context():
    """Give each test its own zway state.

    zway state (the batched-fetch identifier registry, response cache,
    session cookies, helper-loaded markers and their asyncio.Locks) hangs
    off the shared device context rather than module globals, so isolating
    tests is just a matter of starting from an empty context -- including
    the Locks, which must not outlive the event loop they bind to (each
    test builds its own Scheduler, hence its own loop)."""
    _context.clear()
    yield
    _context.clear()


def _parse_get_args(path: str) -> list:
    """Pull the [[group, address], ...] argument list out of a
    .../JS/Run/Get([...]) request path."""
    expr = urllib.parse.unquote(path.split("/JS/Run/", 1)[1])
    match = re.fullmatch(r"Get\((.*)\)", expr)
    return json.loads(match.group(1))


def _serve(*, login_ok: bool = True, get_response=None, get_status: int = 200,
           require_cookie: str | None = None, helper_preloaded: bool = True):
    """Start a throwaway local HTTP server faking a zWay controller running
    thc_zWay.js. `get_response` is either a fixed JSON-able value returned
    for every Get() call, or a callable(idents) -> JSON-able value computed
    from the requested identifier list (for order-preserving-decode
    checks). `require_cookie`, if set, means /JS/Run/... only succeeds when
    the request carries that exact Cookie header (else 401) -- used to
    exercise the login/re-login flow. `helper_preloaded` (default True)
    means Get_IndexArray(257.1) already answers correctly, as if
    thc_zWay.js were already loaded -- most tests aren't about the helper
    load itself, so they don't want the extra probe request. Set it False
    to simulate a fresh zWay server where the probe only starts succeeding
    once executeFile("thc_zWay.js") has been hit (see
    test_zway_loads_helper_script_when_not_yet_loaded). Returns (server,
    base_url); call server.shutdown() when done. The server's
    `get_hits`/`login_hits`/`helper_hits`/`execute_hits` attributes count
    requests, for cache/batching/load assertions.
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
            if "/JS/Run/Get_IndexArray(" in self.path:
                self.server.helper_hits += 1
                if self.server.helper_loaded:
                    self._send_json(200, [257, 1, 0])
                else:
                    self._send_json(500, {"error": "Get_IndexArray is not defined"})
                return
            if "/JS/Run/executeFile(" in self.path:
                self.server.execute_hits += 1
                self.server.helper_loaded = True
                self._send_json(200, None)
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
    server.helper_hits = 0
    server.execute_hits = 0
    server.helper_loaded = helper_preloaded
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, base_url


def _switch_endpoints():
    return [Endpoint("state", writable=True,
                     params={"command_group": "SwitchBinary", "address": "20.1"})]


def _sensor_endpoints(battery_poll_interval=None):
    battery_params = {"command_group": "Battery", "address": "26.0"}
    if battery_poll_interval is not None:
        battery_params["poll_interval"] = battery_poll_interval
    return [
        Endpoint("state", params={"command_group": "SensorBinary", "address": "26"}),
        Endpoint("battery", params=battery_params),
    ]


def _battery_only_endpoints(poll_interval="1h"):
    return [Endpoint("battery", params={"command_group": "Battery", "address": "26",
                                         "poll_interval": poll_interval})]


def _tagreader_endpoints():
    return [Endpoint("state", params={"command_group": "TagReader", "address": "22"})]


def _device(base_url, device_id="dev", endpoints=None, context_override=None, **params):
    """Build one ZWayDevice sharing this test's device context (so sibling
    devices batch their reads, as they do in a real system).
    `context_override` opts one device into a *different* context, for the
    test that checks two systems stay isolated."""
    return ZWayDevice(device_id, params={"base_url": base_url, **params},
                       endpoints=endpoints if endpoints is not None else _switch_endpoints(),
                       update_interval=0.0,
                       context=_context if context_override is None else context_override)


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
        tag = _device(base_url, "tag", endpoints=_tagreader_endpoints(), node=22)
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


def test_zway_decodes_double_json_encoded_get_response():
    # A real Razberry/zWay controller's /JS/Run/ returns Get()'s result
    # pre-stringified by thc_zWay.js, then JSON-encodes THAT string for the
    # HTTP body -- so the wire payload is a JSON string containing the
    # array's JSON text (e.g. '"[0,0,100]"'), not a plain JSON array. This
    # was only discovered by testing against real hardware; _serve()'s
    # default get_response path sends a plain array, so this test overrides
    # _send_json's encoding directly to reproduce the real shape.
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if "/JS/Run/Get_IndexArray(" in self.path:
                self._send_json(200, [257, 1, 0])
                return
            self.server.get_hits += 1
            idents = _parse_get_args(self.path)
            payload = [0, 0, 100][:len(idents)]
            body = json.dumps(json.dumps(payload)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
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
        sensor = _device(base_url, "sensor", endpoints=_sensor_endpoints())
        scheduler = Scheduler({"sensor": sensor})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert sensor.get("state") == 0
        assert sensor.get("battery") == 0
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


def test_zway_throttled_endpoint_not_refetched_before_poll_interval_elapses():
    requests = []

    def responder(idents):
        requests.append(idents)
        return [len(requests)] * len(idents)   # a distinct value per Get() call

    server, base_url = _serve(get_response=responder)
    try:
        sensor = _device(base_url, "sensor", endpoints=_sensor_endpoints(battery_poll_interval="1h"),
                          cache_time="50ms")
        scheduler = Scheduler({"sensor": sensor})
        scheduler.tick(now=0.0)
        time.sleep(0.1)   # past cache_time, nowhere near battery's poll_interval
        scheduler.tick(now=1.0)
        scheduler.close()
        assert len(requests) == 2
        assert ["Battery", "26.0"] in requests[0]      # never fetched yet -- due on the first Get()
        assert ["Battery", "26.0"] not in requests[1]  # not due yet -- excluded from the second
        assert sensor.get("state") == 2       # sibling endpoint still re-fetched every cache_time
        assert sensor.get("battery") == 1     # battery served from its throttled cache, unchanged
    finally:
        server.shutdown()


def test_zway_throttled_endpoint_refetched_after_poll_interval_elapses():
    requests = []

    def responder(idents):
        requests.append(idents)
        return [0] * len(idents)

    server, base_url = _serve(get_response=responder)
    try:
        sensor = _device(base_url, "sensor", endpoints=_sensor_endpoints(battery_poll_interval="50ms"),
                          cache_time="50ms")
        scheduler = Scheduler({"sensor": sensor})
        scheduler.tick(now=0.0)
        time.sleep(0.1)   # past both cache_time and battery's poll_interval
        scheduler.tick(now=1.0)
        scheduler.close()
        assert len(requests) == 2
        assert ["Battery", "26.0"] in requests[1]
    finally:
        server.shutdown()


def test_zway_skips_get_when_nothing_is_due():
    server, base_url = _serve(get_response=[42])
    try:
        dev = _device(base_url, "batt", endpoints=_battery_only_endpoints(), cache_time="50ms")
        scheduler = Scheduler({"batt": dev})
        scheduler.tick(now=0.0)
        assert server.get_hits == 1
        assert dev.get("battery") == 42
        time.sleep(0.1)   # past cache_time, but battery's 1h poll_interval hasn't elapsed
        scheduler.tick(now=1.0)
        scheduler.close()
        assert server.get_hits == 1   # nothing due -- no Get() issued at all
        assert dev.get("battery") == 42   # still served from the throttled cache
    finally:
        server.shutdown()


def test_zway_write_issues_one_set_and_invalidates_cache():
    # A write only ever reaches transmit_async() via the Scheduler's own
    # write-collector path (a task action within a tick) -- ZWayDevice, like
    # any native-async device, only overrides transmit_async(), not the
    # sync transmit() that a direct dev.set() outside a tick would call
    # instead (see phc.core.device.Device._emit/core/scheduler.py's write
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


def test_zway_probes_helper_once_when_already_loaded():
    server, base_url = _serve(get_response=[1], helper_preloaded=True)
    try:
        dev = _device(base_url, cache_time="50ms")
        scheduler = Scheduler({"light": dev})
        scheduler.tick(now=0.0)
        time.sleep(0.1)   # past cache_time -- forces a second Get, but not a second probe
        scheduler.tick(now=1.0)
        scheduler.close()
        assert server.helper_hits == 1
        assert server.execute_hits == 0
        assert server.get_hits == 2
        assert dev.get("state") == 1
    finally:
        server.shutdown()


def test_zway_loads_helper_script_when_not_yet_loaded():
    server, base_url = _serve(get_response=[1], helper_preloaded=False)
    try:
        dev = _device(base_url)
        scheduler = Scheduler({"light": dev})
        scheduler.tick(now=0.0)
        scheduler.close()
        # First probe fails (not loaded yet) -> executeFile -> second probe succeeds.
        assert server.helper_hits == 2
        assert server.execute_hits == 1
        assert dev.get("state") == 1
    finally:
        server.shutdown()


def test_zway_helper_load_shared_across_sibling_devices():
    server, base_url = _serve(get_response=lambda idents: [0] * len(idents), helper_preloaded=False)
    try:
        light = _device(base_url, "light")
        sensor = _device(base_url, "sensor", endpoints=_sensor_endpoints())
        scheduler = Scheduler({"light": light, "sensor": sensor})
        scheduler.tick(now=0.0)
        scheduler.close()
        # One device's load attempt covers both -- not a separate probe/load per device.
        assert server.execute_hits == 1
    finally:
        server.shutdown()


def test_zway_reports_none_and_retries_when_controller_unreachable():
    server, base_url = _serve(get_response=[1])
    server.shutdown()   # close it immediately -- simulates zWay not up yet
    server.server_close()
    dev = _device(base_url)
    scheduler = Scheduler({"light": dev})
    scheduler.tick(now=0.0)
    scheduler.close()
    assert dev.get("state") is None


def test_zway_recovers_once_controller_becomes_reachable():
    # Start with the controller down (simulating "not up yet"), then bring
    # a real server up on that exact port before the next poll -- the
    # device must retry the helper-load probe rather than giving up
    # permanently after the first failure.
    down_server, base_url = _serve(get_response=[1], helper_preloaded=False)
    port = down_server.server_address[1]
    down_server.shutdown()
    down_server.server_close()   # release the port so the real server below can bind it
    dev = _device(base_url)
    scheduler = Scheduler({"light": dev})
    scheduler.tick(now=0.0)
    assert dev.get("state") is None

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps([257, 1, 0]).encode("utf-8") if "/JS/Run/Get_IndexArray(" in self.path \
                else json.dumps([1]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        scheduler.tick(now=1.0)
        scheduler.close()
        assert dev.get("state") == 1
    finally:
        server.shutdown()


def test_zway_configure_tag_reader_fires_once():
    server, base_url = _serve(get_response=[1])
    try:
        tag = _device(base_url, "tag", endpoints=_tagreader_endpoints(), node=22, cache_time="50ms")
        scheduler = Scheduler({"tag": tag})
        scheduler.tick(now=0.0)
        time.sleep(0.1)   # past cache_time -- forces a second Get, but not a second configure
        scheduler.tick(now=1.0)
        scheduler.close()
        assert server.configure_hits == 1
        assert server.get_hits == 2
    finally:
        server.shutdown()


def test_zway_skips_configure_when_tag_reader_device_has_no_node():
    # Never raises -- a TagReader endpoint on a device with no `node` param
    # is simply not configured, mirroring how a misconfigured
    # command_group/address permanently reports None instead of erroring.
    server, base_url = _serve(get_response=[1])
    try:
        tag = _device(base_url, "tag", endpoints=_tagreader_endpoints())
        scheduler = Scheduler({"tag": tag})
        scheduler.tick(now=0.0)
        scheduler.close()
        assert server.configure_hits == 0
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
            if "/JS/Run/Get_IndexArray(" in self.path:
                body = json.dumps([257, 1, 0]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
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


def test_zway_state_is_per_system_not_per_process():
    """Regression guard: zway's batched-fetch registry, response cache and
    session cookies used to be module globals, so two systems loaded in one
    process shared them -- identifiers left behind by one system kept the
    other's response cache permanently invalid (its freshness check
    compares against len(identifiers)), and a cached cookie or
    helper-loaded marker outlived the connection it belonged to. State now
    hangs off the shared device context, so it is scoped to one System.

    Siblings within a system must still share it, since batching sibling
    reads into one HTTP request is the whole point."""
    system_a: dict = {}
    system_b: dict = {}
    a1 = _device("http://ctrl.example", "a1", context_override=system_a)
    a2 = _device("http://ctrl.example", "a2", context_override=system_a)
    b1 = _device("http://ctrl.example", "b1", context_override=system_b)

    assert a1._state is a2._state, "siblings in one system must share state (batching)"
    assert a1._state is not b1._state, "separate systems must not share state"
    # Each system registered its own devices' identifiers, not the other's.
    assert len(a1._state.identifiers["http://ctrl.example"]) == 1
    assert len(b1._state.identifiers["http://ctrl.example"]) == 1
