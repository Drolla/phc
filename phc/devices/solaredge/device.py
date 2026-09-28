"""SolarEdgeDevice: live power/energy readings for one SolarEdge PV site.

From SolarEdge's cloud monitoring API (monitoringapi.solaredge.com)."""

import asyncio
import time
from datetime import datetime, timedelta

import aiohttp

from phc.core.device import Device
from phc.core.intervals import parse_duration
from phc.core.registry import register_module

_BASE_URL = "https://monitoringapi.solaredge.com"

# SolarEdge's own timestamp format, e.g. "2026-09-27 18:00:00" (site-local).
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class _SolarEdgeState:
    """Response caches shared by every SolarEdgeDevice of ONE system.

    Two independent caches (overview, power details) rather than one: the
    two endpoints have different natural freshness and can fail
    independently, so caching them together would make one's failure or
    staleness bleed into the other. Keyed by site_id, so devices sharing a
    site coalesce into one fetch per cache window -- same shape as
    phc/devices/meteoswiss's _MeteoSwissState.

    Per-system, not per-process: see _MeteoSwissState for why a
    module-scope cache and its asyncio.Lock are the wrong lifetime.
    """

    def __init__(self):
        self.overview_cache: dict[str, tuple[float, dict]] = {}
        self.overview_lock = asyncio.Lock()
        self.power_details_cache: dict[str, tuple[float, dict]] = {}
        self.power_details_lock = asyncio.Lock()


@register_module("solaredge")
class SolarEdgeDevice(Device):
    """One SolarEdge site, polled over its cloud monitoring API.

    Async, cached per site_id. Read-only.
    """

    def setup(self):
        """Read this device's resolved params and join the shared cache."""
        # One _SolarEdgeState per system, shared by every solaredge device
        # in it -- `get`-then-assign rather than setdefault, which would
        # build (and immediately discard) fresh asyncio.Locks for every
        # device after the first.
        state = self.context.get("solaredge")
        if state is None:
            state = self.context["solaredge"] = _SolarEdgeState()
        self._state = state

        self._api_key = self.params["api_key"]
        self._site_id = str(self.params["site_id"])
        # .get(..., default) mirrors module.yaml's declared defaults for a
        # device constructed directly (bypassing load_system()'s parameter
        # merge), e.g. in tests.
        self._cache_time = parse_duration(self.params.get("cache_time", "5m"))
        self._request_timeout = parse_duration(self.params.get("request_timeout", "10s"))
        self._power_details_window = parse_duration(
            self.params.get("powerdetails_window", "30m")
        )

    async def receive_async(self) -> dict:
        """Fetch overview + power details, return {endpoint_key: value}.

        The two fetches fail independently -- a power-details error
        shouldn't blank out overview-derived endpoints (current_power,
        the energy totals), and vice versa.
        """
        overview, power_details = await asyncio.gather(
            self._get_overview(), self._get_power_details()
        )
        values = {}
        for key, ep in self.endpoints.items():
            source = ep.params.get("source")
            if source == "overview":
                values[key] = self._extract_overview(overview, ep.params.get("field"))
            else:
                values[key] = self._extract_meter(power_details, ep.params.get("meter"))
        return values

    async def _get_overview(self) -> dict | None:
        """Return the site's `overview` block, reusing a cached copy if fresh."""
        return await self._cached_fetch(
            cache=self._state.overview_cache,
            lock=self._state.overview_lock,
            fetch=self._download_overview,
        )

    async def _get_power_details(self) -> dict | None:
        """Return the site's `powerDetails` block, reusing a cached copy if fresh."""
        return await self._cached_fetch(
            cache=self._state.power_details_cache,
            lock=self._state.power_details_lock,
            fetch=self._download_power_details,
        )

    async def _cached_fetch(self, *, cache: dict, lock: asyncio.Lock, fetch) -> dict | None:
        """Return `fetch()`'s result for this site, reusing it while fresh.

        Double-checked locking: the fast path outside the lock keeps
        uncontended polls cheap, and the re-check inside it means that when
        several sibling devices come due in the same tick, exactly one of
        them performs the fetch and the rest reuse its result.
        """
        mono = time.monotonic()
        cached = cache.get(self._site_id)
        if cached is not None and (mono - cached[0]) < self._cache_time:
            return cached[1]
        async with lock:
            # Re-check: another device may have refreshed the cache while
            # this one waited for the lock.
            mono = time.monotonic()
            cached = cache.get(self._site_id)
            if cached is not None and (mono - cached[0]) < self._cache_time:
                return cached[1]
            try:
                payload = await fetch()
            except (TimeoutError, aiohttp.ClientError) as exc:
                # The fetch itself still "succeeds" from the Scheduler's
                # side (every endpoint just reports None), so this device
                # only registers as unhealthy because the failure is
                # reported explicitly -- see Device.report_failure.
                self.report_failure(f"{type(exc).__name__}: {exc}")
                return None
            # Only a successful fetch reaches here, so a failure never
            # populates the cache and the next poll retries instead of
            # serving an error for a whole cache_time.
            cache[self._site_id] = (mono, payload)
            return payload

    async def _download_overview(self) -> dict:
        """GET /site/{id}/overview.json and return its `overview` block."""
        url = f"{_BASE_URL}/site/{self._site_id}/overview.json"
        payload = await self._get_json(url, {"api_key": self._api_key})
        return payload["overview"]

    async def _download_power_details(self) -> dict:
        """GET /site/{id}/powerDetails.json for a trailing window.

        Returns {meter_type: [(datetime, value), ...]}, oldest first, as
        reported for each of the site's meters over the trailing
        powerdetails_window.
        """
        end = datetime.now()
        start = end - timedelta(seconds=self._power_details_window)
        url = f"{_BASE_URL}/site/{self._site_id}/powerDetails.json"
        payload = await self._get_json(url, {
            "api_key": self._api_key,
            "startTime": start.strftime(_TIME_FORMAT),
            "endTime": end.strftime(_TIME_FORMAT),
        })
        meters = payload["powerDetails"]["meters"]
        return {meter["type"]: meter["values"] for meter in meters}

    async def _get_json(self, url: str, params: dict) -> dict:
        """GET `url` with query `params` and return the parsed JSON body."""
        timeout = aiohttp.ClientTimeout(total=self._request_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                return await response.json(content_type=None)

    @staticmethod
    def _extract_overview(overview: dict | None, field: str | None):
        """Return one dotted field (e.g. "currentPower.power") from overview.

        None when overview is unavailable, field is undeclared, or any
        step of the path is missing.
        """
        if overview is None or field is None:
            return None
        value = overview
        for step in field.split("."):
            if not isinstance(value, dict) or step not in value:
                return None
            value = value[step]
        return value

    @staticmethod
    def _extract_meter(meters: dict | None, meter: str | None):
        """Return one meter's latest non-null reading from power_details.

        SolarEdge's most recent 15-minute bucket sometimes hasn't posted
        yet (its `value` key is simply absent, distinct from a legitimate
        reading of 0), so this walks backward from the newest bucket and
        returns the first one that actually reports a value. None if the
        meter is missing entirely or every bucket in the window is unset.
        """
        if meters is None or meter is None:
            return None
        for entry in reversed(meters.get(meter, [])):
            if "value" in entry:
                return entry["value"]
        return None
