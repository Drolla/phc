"""ZWayDevice: one physical Z-Wave node's values, read/written via a
Razberry/zWay controller's zWay.js helper script over HTTP."""

import asyncio
import json
import time
from urllib.parse import quote

import aiohttp

from core.device import Device
from core.intervals import parse_duration
from core.registry import register_module

# Devices sharing a base_url register their endpoints' (command_group, address)
# identifiers here during setup(). Before the first fetch, the registry is
# guaranteed complete (all devices are setup() before Scheduler starts). A shared
# per-base_url fetch then requests every currently-registered identifier in one
# combined Get() call, so sibling devices behind one controller batch into a
# single HTTP request per poll. Same cache-by-connection pattern as
# devices/waveplus_bridge's _response_cache, but parameterized by which devices
# exist, not a fixed URL.
_identifiers: dict[str, dict[tuple[str, str], None]] = {}   # base_url -> ordered set of (group, address)
_response_cache: dict[str, tuple[float, dict, int]] = {}    # base_url -> (fetched_at, {ident: value}, n_idents)
_response_cache_lock = asyncio.Lock()
# Session cookie only, not a long-lived aiohttp.ClientSession -- see
# _js_run()'s docstring for why a fresh session is still opened per request.
_session_cookies: dict[str, str] = {}   # base_url -> Cookie header value
_session_lock = asyncio.Lock()
_configured_tag_readers: set[tuple[str, str]] = set()   # (base_url, node) already configured


@register_module("zway")
class ZWayDevice(Device):
    """One physical Z-Wave node's values (e.g. a switch's state, sensor
    reading, battery level), read/written via Get/Set commands to a
    Razberry/zWay controller (using zWay.js, installed on the controller).
    Each endpoint maps to a zWay identifier: `command_group` (one of
    SwitchBinary/SwitchMultilevel/SwitchMultiBinary/SensorBinary/
    SensorMultilevel/Battery/TagReader) and `address` (an opaque
    "node.instance[.datarecord]" string, passed verbatim). A device with a
    TagReader endpoint must also set its own `node` param for the one-time
    Configure_TagReader call. Devices sharing `base_url` batch their reads
    into a single HTTP request per poll (see _identifiers/_response_cache).
    """

    def setup(self):
        """Register this device's readable endpoints' identifiers into the
        shared per-base_url registry for batching. Sync/no-I/O because all
        devices are setup() before the Scheduler starts."""
        self._base_url = self.params["base_url"].rstrip("/")
        self._user = self.params.get("user")
        self._password = self.params.get("password")
        # .get(..., default) mirrors module.yaml's defaults for devices
        # constructed directly (bypassing load_system()/_merge_params), e.g.
        # in tests.
        self._cache_time = parse_duration(self.params.get("cache_time", "30s"))
        self._request_timeout = parse_duration(self.params.get("request_timeout", "10s"))

        self._idents: dict[str, tuple[str, str]] = {}
        has_tag_reader = False

        registry = _identifiers.setdefault(self._base_url, {})
        for key, ep in self.endpoints.items():
            command_group = ep.params.get("command_group")
            address = ep.params.get("address")
            if command_group is None or address is None:
                continue
            ident = (command_group, str(address))
            self._idents[key] = ident
            if ep.readable:
                registry[ident] = None
            if command_group == "TagReader":
                has_tag_reader = True
        node = self.params.get("node")
        self._tag_reader_node: str | None = str(node) if has_tag_reader and node is not None else None

    async def receive_async(self) -> dict:
        """Configure any unconfigured TagReader nodes, fetch batched values,
        return {endpoint_key: value} for this device's endpoints (None on
        any failure, like other weather/sensor modules)."""
        await self._ensure_tag_readers_configured()
        try:
            values = await self._get_values()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            values = {}
        return {key: values.get(ident) for key, ident in self._idents.items()}

    async def transmit_async(self, state: dict) -> None:
        """Write each endpoint via Set() individually (not batched; writes
        are sporadic, not periodic). A successful write clears the shared
        read cache so the next poll reflects it immediately. Failed writes
        are silently ignored; the next poll reports the real state."""
        for key, value in state.items():
            ident = self._idents.get(key)
            if ident is None:
                continue
            command_group, address = ident
            expr = f'Set([["{command_group}","{address}"]],{json.dumps(value)})'
            try:
                await self._js_run(expr)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
            _response_cache.pop(self._base_url, None)

    # ---------- shared batched fetch ----------

    async def _get_values(self) -> dict:
        """Return {identifier: value} for this base_url, reusing cached
        results if still fresh and covering all registered identifiers.
        Uses double-checked locking to avoid cache stampedes (see
        waveplus_bridge). Failed fetches are never cached; callers retry."""
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
        """Issue one combined Get() for all identifiers, return
        {identifier: value}. Raises ValueError if response isn't a
        same-length JSON array (total failure, no partial trust)."""
        args = json.dumps([[group, address] for group, address in idents], separators=(",", ":"))
        raw = await self._js_run(f"Get({args})")
        if not isinstance(raw, list) or len(raw) != len(idents):
            raise ValueError(f"zway: malformed/short Get() response for {self._base_url}")
        return dict(zip(idents, raw))

    # ---------- TagReader one-time configure ----------

    async def _ensure_tag_readers_configured(self) -> None:
        """Call Configure_TagReader(node) once per device/base_url pair
        (deferred here instead of setup() since setup is sync/no-I/O).
        Only added to _configured_tag_readers on success, so transient
        failures retry on the next poll."""
        node = self._tag_reader_node
        if node is None:
            return
        key = (self._base_url, node)
        if key in _configured_tag_readers:
            return
        try:
            await self._js_run(f"Configure_TagReader({node})")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return
        _configured_tag_readers.add(key)

    # ---------- HTTP + session/auth ----------

    async def _js_run(self, expr: str):
        """GET {base_url}/JS/Run/{expr}, return parsed JSON. Attaches cached
        session cookie; on 401/403, drops it and retries with fresh login.
        Opens a fresh aiohttp.ClientSession per request (not reused as a
        module-level session) to avoid lifecycle issues across Scheduler
        instances. Only the extracted cookie string is cached, preserving
        correctness (survives session cookie jar reset) without lifecycle debt."""
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
        """Return cached session cookie, logging in first if uncached.
        Returns None when no user is configured (unauthenticated server)."""
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
        """POST credentials to login endpoint, return session cookie."""
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
