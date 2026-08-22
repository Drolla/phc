"""ZWayDevice: one physical Z-Wave node's values.

Read/written via a Razberry/zWay controller's thc_zWay.js helper script
over HTTP."""

import asyncio
import json
import logging
import time
from urllib.parse import quote

import aiohttp

from phc.core.device import Device
from phc.core.intervals import parse_duration
from phc.core.registry import register_module

logger = logging.getLogger("phc.zway")

class _ZWayState:
    """State shared by every ZWayDevice of ONE system.

    Held in the shared device context (see phc.core.device.Device.context)
    rather than at module scope.

    Devices sharing a base_url register their endpoints' (command_group,
    address) identifiers in `identifiers` during setup(). Before the first
    fetch the registry is guaranteed complete (every device is setup()
    before the Scheduler starts). A shared per-base_url fetch then requests
    every currently-registered identifier in one combined Get() call, so
    sibling devices behind one controller batch into a single HTTP request
    per poll.

    Each identifier maps to an optional poll_interval (seconds): None means
    it rides along on every combined fetch. A set poll_interval (e.g.
    Battery endpoints, which change far more slowly than their sibling
    sensor) excludes that identifier from the combined Get() until the
    interval has elapsed since it was last actually fetched -- see
    _select_fetch_idents/_merge_throttled. This only throttles how often
    PHC asks the zWay controller for that value; it says nothing about
    whether zWay itself polls the physical node over the Z-Wave mesh to
    answer that Get().

    Per-system, not per-process, for three reasons. `response_cache`'s
    freshness check compares against len(identifiers), so identifiers left
    behind by a previous System would keep the cache permanently invalid.
    The cached session cookies and "helper script is loaded" markers belong
    to a specific controller connection, whose lifetime is the System's.
    And the asyncio.Lock()s must not outlive the event loop they end up
    bound to -- a lock left over from a closed loop fails with "bound to a
    different event loop" the moment two sibling devices genuinely contend
    for it.
    """

    def __init__(self):
        # base_url -> {(group, address): poll_interval | None}
        self.identifiers: dict[str, dict[tuple[str, str], float | None]] = {}
        # base_url -> (fetched_at, {ident: value}, n_idents)
        self.response_cache: dict[str, tuple[float, dict, int]] = {}
        self.response_cache_lock = asyncio.Lock()
        # base_url -> {ident: (last_fetched_monotonic, value)}, for
        # identifiers with a poll_interval override only -- holds the last
        # actually-fetched value so it can still be served on fetches where
        # that identifier isn't due yet.
        self.throttled_values: dict[str, dict[tuple[str, str], tuple[float, object]]] = {}
        # Session cookie only, not a long-lived aiohttp.ClientSession -- see
        # _js_run()'s docstring for why a fresh session is opened per request.
        self.session_cookies: dict[str, str] = {}
        self.session_lock = asyncio.Lock()
        self.configured_tag_readers: set[tuple[str, str]] = set()   # (base_url, node)
        self.helper_loaded: set[str] = set()   # base_urls with thc_zWay.js loaded
        self.helper_lock = asyncio.Lock()


# Marker identifier Get_IndexArray() is probed with to check thc_zWay.js is
# loaded on the zWay server, and its expected reply -- see
# ZWayDevice._ensure_helper_loaded. The value itself is arbitrary, chosen to
# be unlikely to collide with a real Z-Wave node/instance/datarecord
# combination.
_HELPER_PROBE_IDENT = "257.1"
_HELPER_PROBE_RESPONSE = [257, 1, 0]


@register_module("zway")
class ZWayDevice(Device):
    """One physical Z-Wave node's values.

    E.g. a switch's state, sensor reading, battery level -- read/written
    via Get/Set commands to a Razberry/zWay controller, using
    thc_zWay.js (see _ensure_helper_loaded -- installed in the zWay
    server's automation folder by the user, then loaded there
    automatically on first use). Each endpoint maps to a zWay
    identifier: `command_group` (one of
    SwitchBinary/SwitchMultilevel/SwitchMultiBinary/SensorBinary/
    SensorMultilevel/Battery/TagReader) and `address` (an opaque
    "node.instance[.datarecord]" string, passed verbatim). A device with a
    TagReader endpoint must also set its own `node` param for the one-time
    Configure_TagReader call. Devices sharing `base_url` batch their reads
    into a single HTTP request per poll (see _ZWayState).
    An endpoint's `poll_interval` lets it opt out of that per-poll cadence
    and only be re-fetched on its own, slower schedule (see
    _select_fetch_idents/_merge_throttled).
    """

    def setup(self):
        """Register this device's readable endpoints into the shared registry.

        Per-base_url identifiers, for batching. Sync/no-I/O because all
        devices are setup() before the Scheduler starts."""
        # One _ZWayState per system, shared by every zway device in it --
        # setdefault would build (and throw away) a fresh set of Locks on
        # every device after the first.
        state = self.context.get("zway")
        if state is None:
            state = self.context["zway"] = _ZWayState()
        self._state = state

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

        registry = self._state.identifiers.setdefault(self._base_url, {})
        for key, ep in self.endpoints.items():
            command_group = ep.params.get("command_group")
            address = ep.params.get("address")
            if command_group is None or address is None:
                continue
            ident = (command_group, str(address))
            self._idents[key] = ident
            if ep.readable:
                poll_interval = ep.params.get("poll_interval")
                registry[ident] = parse_duration(poll_interval) if poll_interval is not None else None
            if command_group == "TagReader":
                has_tag_reader = True
        node = self.params.get("node")
        self._tag_reader_node: str | None = str(node) if has_tag_reader and node is not None else None

    async def receive_async(self) -> dict:
        """Ensure thc_zWay.js is loaded, configure TagReaders, fetch values.

        Returns {endpoint_key: value} for this device's endpoints (None
        on any failure, like other weather/sensor modules -- logged at
        ERROR here, since phc.core.scheduler only logs a bare "fetch
        failed" for an unhandled exception, not one caught and turned
        into empty values)."""
        if not await self._ensure_helper_loaded():
            # Controller unreachable, or thc_zWay.js not installed on it.
            self.report_failure(f"thc_zWay.js not loaded on {self._base_url}")
            return {key: None for key in self._idents}
        await self._ensure_tag_readers_configured()
        try:
            values = await self._get_values()
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            # Caught rather than raised so one unreachable controller does
            # not disturb the tick -- which also means the Scheduler sees a
            # successful fetch, so the failure is reported explicitly (see
            # Device.report_failure) for this device to show as unhealthy.
            logger.error("%s: fetch failed: %s", self._base_url, exc)
            self.report_failure(f"{type(exc).__name__}: {exc}")
            values = {}
        return {key: values.get(ident) for key, ident in self._idents.items()}

    async def transmit_async(self, state: dict) -> None:
        """Write each endpoint via Set() individually.

        Not batched; writes are sporadic, not periodic. A successful
        write clears the shared read cache so the next poll reflects it
        immediately. Failed writes are silently ignored; the next poll
        reports the real state."""
        if not await self._ensure_helper_loaded():
            return
        for key, value in state.items():
            ident = self._idents.get(key)
            if ident is None:
                continue
            command_group, address = ident
            expr = f'Set([["{command_group}","{address}"]],{json.dumps(value)})'
            try:
                await self._js_run(expr)
            except (TimeoutError, aiohttp.ClientError) as exc:
                logger.error("%s: write failed for %s: %s", self._base_url, key, exc)
                continue
            self._state.response_cache.pop(self._base_url, None)

    # ---------- shared batched fetch ----------

    async def _get_values(self) -> dict:
        """Return {identifier: value} for this base_url.

        Reuses cached results if still fresh and covering all
        registered identifiers. Throttled identifiers are
        selected/served separately (see
        _select_fetch_idents/_merge_throttled, and _ZWayState for why).
        Uses double-checked locking to avoid cache stampedes (see
        waveplus_bridge). Failed fetches are never cached; callers retry."""
        mono = time.monotonic()
        registry = self._state.identifiers[self._base_url]
        cached = self._state.response_cache.get(self._base_url)
        if cached is not None and (mono - cached[0]) < self._cache_time and cached[2] == len(registry):
            return self._merge_throttled(cached[1], registry)
        async with self._state.response_cache_lock:
            mono = time.monotonic()
            cached = self._state.response_cache.get(self._base_url)
            if cached is not None and (mono - cached[0]) < self._cache_time and cached[2] == len(registry):
                return self._merge_throttled(cached[1], registry)
            idents = self._select_fetch_idents(registry, mono)
            # Nothing due (e.g. a controller with only throttled identifiers,
            # none of them elapsed yet) -- skip the request rather than
            # issuing an empty Get().
            values = await self._download(idents) if idents else {}
            self._state.response_cache[self._base_url] = (time.monotonic(), values, len(registry))
            self._record_throttled(values, registry)
            return self._merge_throttled(values, registry)

    def _select_fetch_idents(self, registry: dict, mono: float) -> list[tuple[str, str]]:
        """Return the identifiers to actually request this fetch.

        Every identifier without a poll_interval override, plus any
        overridden one whose poll_interval has elapsed since it was
        last fetched (or was never fetched yet -- always due on the
        first fetch)."""
        throttled = self._state.throttled_values.get(self._base_url, {})
        idents = []
        for ident, poll_interval in registry.items():
            if poll_interval is None:
                idents.append(ident)
                continue
            last = throttled.get(ident)
            if last is None or (mono - last[0]) >= poll_interval:
                idents.append(ident)
        return idents

    def _record_throttled(self, values: dict, registry: dict) -> None:
        """Remember the just-fetched value of every throttled identifier.

        That was actually included in this fetch, so later fetches
        where it's not due can still serve it (see _merge_throttled)."""
        throttled = self._state.throttled_values.setdefault(self._base_url, {})
        mono = time.monotonic()
        for ident, poll_interval in registry.items():
            if poll_interval is not None and ident in values:
                throttled[ident] = (mono, values[ident])

    def _merge_throttled(self, values: dict, registry: dict) -> dict:
        """Fill in identifiers missing from `values`.

        Throttled, not due this fetch -- from their last actually-fetched
        value, if any."""
        throttled = self._state.throttled_values.get(self._base_url, {})
        merged = dict(values)
        for ident, poll_interval in registry.items():
            if poll_interval is not None and ident not in merged:
                last = throttled.get(ident)
                if last is not None:
                    merged[ident] = last[1]
        return merged

    async def _download(self, idents: list[tuple[str, str]]) -> dict:
        """Issue one combined Get() for all identifiers.

        Returns {identifier: value}. Raises ValueError if response
        isn't a same-length JSON array (total failure, no partial
        trust)."""
        args = json.dumps([[group, address] for group, address in idents], separators=(",", ":"))
        raw = await self._js_run(f"Get({args})")
        # thc_zWay.js's Get() result comes back double-JSON-encoded: /JS/Run/
        # wraps whatever the script returns in its own JSON encoding, and
        # Get() itself already returns its array pre-stringified -- so the
        # HTTP body is a JSON *string* containing the array's JSON text
        # (e.g. the wire body is '"[0,0,100]"', which response.json() above
        # decodes to the Python str '[0,0,100]', not a list). Observed
        # against a real Razberry/zWay controller; undocumented. Un-wrap
        # once when that's what we got; a plain list is still accepted too,
        # in case some zWay/thc_zWay.js version returns it unwrapped.
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raise ValueError(
                    f"zway: malformed Get() response for {self._base_url}: {raw!r}") from None
        if not isinstance(raw, list) or len(raw) != len(idents):
            raise ValueError(f"zway: malformed/short Get() response for {self._base_url}")
        # zWay reports a node with no value yet (unresponsive/asleep) as ""
        # rather than null. Normalize to None so it's indistinguishable from
        # any other fetch failure (see receive_async).
        return {ident: (value if value != "" else None) for ident, value in zip(idents, raw, strict=True)}

    # ---------- helper script (thc_zWay.js) one-time load ----------

    async def _ensure_helper_loaded(self) -> bool:
        """Confirm thc_zWay.js is loaded on this base_url's zWay server.

        Loading it via executeFile() first if a Get_IndexArray() probe
        shows it's missing. Returns False if the controller is
        unreachable or the file isn't present in its automation folder
        -- callers treat that like any other fetch/write failure, so
        it's simply retried on the next poll instead of blocking
        startup (see _ensure_tag_readers_configured)."""
        if self._base_url in self._state.helper_loaded:
            return True
        async with self._state.helper_lock:
            if self._base_url in self._state.helper_loaded:
                return True
            if await self._probe_helper():
                self._state.helper_loaded.add(self._base_url)
                return True
            try:
                await self._js_run('executeFile("thc_zWay.js")')
            except (TimeoutError, aiohttp.ClientError) as exc:
                logger.error("%s: could not reach zWay server to load thc_zWay.js: %s",
                             self._base_url, exc)
                return False
            if not await self._probe_helper():
                logger.error(
                    "%s: thc_zWay.js did not load -- is it in the zWay automation folder?",
                    self._base_url)
                return False
            logger.info("%s: loaded thc_zWay.js", self._base_url)
            self._state.helper_loaded.add(self._base_url)
            return True

    async def _probe_helper(self) -> bool:
        """True if Get_IndexArray() responds as expected.

        I.e. thc_zWay.js is already loaded. False on any HTTP failure
        or mismatched reply (script not loaded, or not loaded yet)."""
        try:
            result = await self._js_run(f"Get_IndexArray({_HELPER_PROBE_IDENT})")
        except (TimeoutError, aiohttp.ClientError):
            return False
        return result == _HELPER_PROBE_RESPONSE

    # ---------- TagReader one-time configure ----------

    async def _ensure_tag_readers_configured(self) -> None:
        """Call Configure_TagReader(node) once per device/base_url pair.

        Deferred here instead of setup() since setup is sync/no-I/O.
        Only added to self._state.configured_tag_readers on success, so
        transient failures retry on the next poll."""
        node = self._tag_reader_node
        if node is None:
            return
        key = (self._base_url, node)
        if key in self._state.configured_tag_readers:
            return
        try:
            await self._js_run(f"Configure_TagReader({node})")
        except (TimeoutError, aiohttp.ClientError):
            return
        self._state.configured_tag_readers.add(key)
        logger.info("tag reader registered for node %s on %s", node, self._base_url)

    # ---------- HTTP + session/auth ----------

    async def _js_run(self, expr: str):
        """GET {base_url}/JS/Run/{expr}, return parsed JSON.

        Attaches cached session cookie; on 401/403, drops it and
        retries with fresh login. Opens a fresh aiohttp.ClientSession
        per request (not reused as a module-level session) to avoid
        lifecycle issues across Scheduler instances. Only the extracted
        cookie string is cached, preserving correctness (survives
        session cookie jar reset) without lifecycle debt.

        The one call site for every physical-device interaction (Get/Set/
        Configure_TagReader alike), so it's logged here at DEBUG rather than
        at each of its callers."""
        url = f"{self._base_url}/JS/Run/{quote(expr, safe='')}"
        timeout = aiohttp.ClientTimeout(total=self._request_timeout)
        for attempt in range(2):
            logger.debug("%s (%s): %s", self._base_url, attempt, expr)
            cookie = await self._ensure_cookie()
            headers = {"Cookie": cookie} if cookie else {}
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as response:
                    if response.status in (401, 403) and cookie and attempt == 0:
                        self._state.session_cookies.pop(self._base_url, None)
                        continue
                    response.raise_for_status()
                    result = await response.json(content_type=None)
                    logger.debug("%s (%s): -> %r", self._base_url, attempt, result)
                    return result
        logger.info("%s: -> login expired/rejected", self._base_url)
        raise aiohttp.ClientError(f"zway: login expired/rejected for {self._base_url}")

    async def _ensure_cookie(self) -> str | None:
        """Return cached session cookie, logging in first if uncached.

        Returns None when no user is configured (unauthenticated server)."""
        if self._user is None:
            return None
        cached = self._state.session_cookies.get(self._base_url)
        if cached is not None:
            return cached
        async with self._state.session_lock:
            cached = self._state.session_cookies.get(self._base_url)
            if cached is not None:
                return cached
            cookie = await self._login()
            self._state.session_cookies[self._base_url] = cookie
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
                logger.info("session established for %s (user %s)", self._base_url, self._user)
                return cookie.split(";", 1)[0]
