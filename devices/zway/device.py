"""ZWayDevice: one physical Z-Wave node's values, read/written via a
Razberry/zWay controller's thc_zWay.js helper script over HTTP."""

import asyncio
import json
import time
from urllib.parse import quote

import aiohttp

from core.device import Device
from core.intervals import parse_duration
from core.registry import register_module

# Every ZWayDevice instance registers its own endpoints' (command_group,
# value_id) identifiers here, keyed by base_url, during setup() -- since
# core/config.py::load_system() fully constructs (and setup()s) every device
# before the Scheduler is ever created/ticked, this registry is guaranteed
# complete before the first fetch. The shared per-base_url fetch below then
# requests the FULL currently-registered set in one combined Get() call, not
# just the identifiers the one triggering device happens to own -- so
# sibling devices behind one controller (sharing update_interval, and
# therefore becoming due together) coalesce into a single HTTP request per
# poll, reproducing the old THC zWay module's batching behavior. Same
# cache-by-connection idea as devices/waveplus_bridge's _response_cache,
# extended since here the "request" is itself parameterized by which
# devices exist, not a fixed URL.
_identifiers: dict[str, dict[tuple[str, str], None]] = {}   # base_url -> ordered set of (group, value_id)
_response_cache: dict[str, tuple[float, dict, int]] = {}    # base_url -> (fetched_at, {ident: value}, n_idents)
_response_cache_lock = asyncio.Lock()
# Session cookie only, not a long-lived aiohttp.ClientSession -- see
# _js_run()'s docstring for why a fresh session is still opened per request.
_session_cookies: dict[str, str] = {}   # base_url -> Cookie header value
_session_lock = asyncio.Lock()
_configured_tag_readers: set[tuple[str, str]] = set()   # (base_url, node_id) already configured


@register_module("zway")
class ZWayDevice(Device):
    """One physical Z-Wave node's values (e.g. a switch's state, a sensor's
    reading, a battery level), read/written through thc_zWay.js's Get/Set
    functions on a Razberry/zWay controller
    (https://github.com/Drolla/thc/tree/master/modules/thc_zWay -- install
    thc_zWay.js on the zWay server yourself, PHC does not push it). Each
    endpoint's `parameters` name the zWay identifier it maps to:
    `command_group` (one of SwitchBinary/SwitchMultilevel/SwitchMultiBinary/
    SensorBinary/SensorMultilevel/Battery/TagReader) and `value_id` (an
    opaque "node.instance[.datarecord]" string, passed through verbatim --
    PHC never parses its internal structure). A TagReader endpoint
    additionally needs `node_id`, used for the one-time Configure_TagReader
    call.

    Devices sharing one `base_url` share one cached Get() response per
    cache_time (see _identifiers/_response_cache above), so their reads
    coalesce into a single HTTP request per poll as long as they share the
    same update_interval -- set the same value on every device behind one
    controller to get full sharing, same recommendation as
    devices/waveplus_bridge's cache_time.
    """

    def setup(self):
        """Read this device's resolved params, then register its own
        readable endpoints' (command_group, value_id) identifiers into the
        shared per-base_url registry. Sync/no I/O -- safe because every
        device's setup() runs to completion before the Scheduler exists (see
        core.config.load_system())."""
        self._base_url = self.params["base_url"].rstrip("/")
        self._user = self.params.get("user")
        self._password = self.params.get("password")
        # .get(..., default) mirrors module.yaml's defaults for devices
        # constructed directly (bypassing load_system()/_merge_params), e.g.
        # in tests.
        self._cache_time = parse_duration(self.params.get("cache_time", "30s"))
        self._request_timeout = parse_duration(self.params.get("request_timeout", "10s"))

        self._idents: dict[str, tuple[str, str]] = {}   # endpoint_key -> (command_group, value_id)
        self._tag_reader_nodes: dict[str, str] = {}      # endpoint_key -> node_id

        registry = _identifiers.setdefault(self._base_url, {})
        for key, ep in self.endpoints.items():
            command_group = ep.parameters.get("command_group")
            value_id = ep.parameters.get("value_id")
            if command_group is None or value_id is None:
                continue   # misconfigured endpoint -- permanently reports None, never raises
            ident = (command_group, str(value_id))
            self._idents[key] = ident
            if ep.readable:
                registry[ident] = None
            if command_group == "TagReader":
                node_id = ep.parameters.get("node_id")
                if node_id is not None:
                    self._tag_reader_nodes[key] = str(node_id)

    async def receive_async(self) -> dict:
        """Async counterpart of the base receive(): configure any not-yet-
        configured TagReader nodes, await the shared batched Get() payload,
        and return {endpoint_key: raw_value} for this device's own
        endpoints -- None per endpoint on any failure, mirroring
        meteoswiss/open_meteo/waveplus_bridge."""
        await self._ensure_tag_readers_configured()
        try:
            values = await self._get_values()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            values = {}
        return {key: values.get(ident) for key, ident in self._idents.items()}

    async def transmit_async(self, state: dict) -> None:
        """Push each written endpoint's value with its own Set() call --
        never batched, since writes only happen when a task actually acts,
        not every heartbeat. A successful write invalidates the shared read
        cache for this base_url so the next poll reflects it immediately
        rather than waiting out cache_time. A failed write is swallowed; the
        next poll reports the real hardware state either way."""
        for key, value in state.items():
            ident = self._idents.get(key)
            if ident is None:
                continue
            command_group, value_id = ident
            expr = f'Set([["{command_group}","{value_id}"]],{json.dumps(value)})'
            try:
                await self._js_run(expr)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
            _response_cache.pop(self._base_url, None)

    # ---------- shared batched fetch ----------

    async def _get_values(self) -> dict:
        """Return the shared {identifier: raw_value} payload for this
        device's base_url, reusing a cached copy if it is still within
        cache_time AND still covers every currently-registered identifier
        (a sibling registering after the cache was last populated only
        matters for tests that build devices one at a time) -- same double-
        checked locking as waveplus_bridge's _get_payload. Never caches a
        failed fetch: an exception here propagates to receive_async(),
        which reports None without touching _response_cache, so the next
        tick retries instead of being stuck on a poisoned cache entry."""
        now = time.monotonic()
        registry = _identifiers[self._base_url]
        cached = _response_cache.get(self._base_url)
        if cached is not None and (now - cached[0]) < self._cache_time and cached[2] == len(registry):
            return cached[1]
        async with _response_cache_lock:
            now = time.monotonic()
            cached = _response_cache.get(self._base_url)
            if cached is not None and (now - cached[0]) < self._cache_time and cached[2] == len(registry):
                return cached[1]
            idents = list(registry)   # frozen, insertion-ordered -- matches the request order below
            values = await self._download(idents)
            _response_cache[self._base_url] = (time.monotonic(), values, len(idents))
            return values

    async def _download(self, idents: list[tuple[str, str]]) -> dict:
        """Issue one combined Get() for every identifier in idents and
        return {identifier: raw_value}. Raises ValueError if the response
        isn't a same-length JSON array -- treated as a total failure (no
        partial trust) by _get_values()'s caller."""
        args = json.dumps([[group, value_id] for group, value_id in idents], separators=(",", ":"))
        raw = await self._js_run(f"Get({args})")
        if not isinstance(raw, list) or len(raw) != len(idents):
            raise ValueError(f"zway: malformed/short Get() response for {self._base_url}")
        return dict(zip(idents, raw))

    # ---------- TagReader one-time configure ----------

    async def _ensure_tag_readers_configured(self) -> None:
        """Call Configure_TagReader(node_id) once for each of this device's
        TagReader nodes not yet configured on this base_url -- setup() is
        sync/no-I/O, so this one-time call is deferred to run lazily here,
        the first time this device is actually fetched. Added to
        _configured_tag_readers only on success, so a transient failure
        retries on the next poll instead of being silently skipped
        forever."""
        for node_id in self._tag_reader_nodes.values():
            key = (self._base_url, node_id)
            if key in _configured_tag_readers:
                continue
            try:
                await self._js_run(f"Configure_TagReader({node_id})")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
            _configured_tag_readers.add(key)

    # ---------- HTTP + session/auth ----------

    async def _js_run(self, expr: str):
        """GET {base_url}/JS/Run/{expr} and return its parsed JSON response.
        Attaches the cached session cookie (if any); on a 401/403 drops it
        and retries once after a fresh login. A fresh aiohttp.ClientSession
        is opened per request here, same as waveplus_bridge/open_meteo --
        NOT reused as a long-lived module-level session, which would outlive
        the Scheduler's own event loop across close()/multiple Scheduler
        instances (e.g. in tests), with no per-device stop hook to close it
        again. Only the extracted cookie *string* is cached/reattached
        manually, which gets the correctness benefit (surviving a fresh
        session's fresh, empty cookie jar) without that lifecycle debt."""
        url = f"{self._base_url}/JS/Run/{quote(expr, safe='')}"
        timeout = aiohttp.ClientTimeout(total=self._request_timeout)
        for attempt in range(2):
            cookie = await self._ensure_cookie()
            headers = {"Cookie": cookie} if cookie else {}
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as response:
                    if response.status in (401, 403) and cookie and attempt == 0:
                        _session_cookies.pop(self._base_url, None)
                        continue
                    response.raise_for_status()
                    return await response.json(content_type=None)
        raise aiohttp.ClientError(f"zway: login expired/rejected for {self._base_url}")

    async def _ensure_cookie(self) -> str | None:
        """Return the cached session cookie for this base_url, logging in
        first if none is cached yet. Returns None (no header attached) when
        no user is configured -- an unauthenticated zWay server needs
        none."""
        if self._user is None:
            return None
        cached = _session_cookies.get(self._base_url)
        if cached is not None:
            return cached
        async with _session_lock:
            cached = _session_cookies.get(self._base_url)
            if cached is not None:
                return cached
            cookie = await self._login()
            _session_cookies[self._base_url] = cookie
            return cookie

    async def _login(self) -> str:
        """POST username/password to zWay's login endpoint and return the
        session cookie to attach to subsequent requests."""
        timeout = aiohttp.ClientTimeout(total=self._request_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self._base_url}/ZAutomation/api/v1/login",
                json={"login": self._user, "password": self._password},
            ) as response:
                response.raise_for_status()
                cookie = response.headers.get("Set-Cookie")
                if not cookie:
                    raise aiohttp.ClientError(
                        f"zway: login did not return a session cookie for {self._base_url}")
                return cookie.split(";", 1)[0]
