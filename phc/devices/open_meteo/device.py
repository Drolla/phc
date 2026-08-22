"""OpenMeteoDevice: current weather conditions for one location.

From the free Open-Meteo forecast API."""

import asyncio
import time

import aiohttp

from phc.core.device import Device
from phc.core.intervals import parse_duration
from phc.core.registry import register_module

# Shared across every OpenMeteoDevice instance, keyed by full request URL
# (which already encodes lat/lon, so co-located devices coalesce into one
# HTTP GET instead of each independently re-downloading the identical
# response -- same pattern as phc/devices/meteoswiss's _csv_cache, just keyed
# more finely since each location has its own URL here).
_response_cache: dict[str, tuple[float, dict]] = {}   # url -> (fetched_at, current)
_response_cache_lock = asyncio.Lock()


@register_module("open_meteo")
class OpenMeteoDevice(Device):
    """One location's current weather from the free Open-Meteo API.

    Async, cached to avoid re-downloading on rapid polls. Read-only.
    """

    def setup(self):
        """Build this location's forecast request URL and read cache_time."""
        self._url = (
            f"{self.params['base_url']}"
            f"?latitude={self.params['latitude']}&longitude={self.params['longitude']}"
            f"&current=temperature_2m,relative_humidity_2m,precipitation,pressure_msl,"
            f"wind_speed_10m,wind_direction_10m,weather_code,is_day"
            f"&wind_speed_unit=ms&timezone=auto"
        )
        # .get(..., "10m") mirrors module.yaml's default for devices
        # constructed directly (bypassing load_system()/_merge_params), e.g.
        # in tests.
        self._cache_time = parse_duration(self.params.get("cache_time", "10m"))

    async def receive_async(self) -> dict:
        """Fetch current-weather block, return {endpoint_key: value}."""
        try:
            current = await self._get_current()
        except (TimeoutError, aiohttp.ClientError) as exc:
            # Network/HTTP failure or the request's own 10s timeout: report
            # every endpoint as unavailable, mirroring meteoswiss. The
            # fetch itself still "succeeds", so the failure has to be
            # reported explicitly for this device to register as unhealthy
            # (see Device.report_failure).
            self.report_failure(f"{type(exc).__name__}: {exc}")
            current = None
        return {key: self._extract(current, ep.params.get("field"))
                for key, ep in self.endpoints.items()}

    async def _get_current(self) -> dict | None:
        """Return `current` dict, reusing cached copy if fresh.

        Uses double-checked locking."""
        mono = time.monotonic()
        cached = _response_cache.get(self._url)
        if cached is not None and (mono - cached[0]) < self._cache_time:
            return cached[1]
        async with _response_cache_lock:
            mono = time.monotonic()
            cached = _response_cache.get(self._url)
            if cached is not None and (mono - cached[0]) < self._cache_time:
                return cached[1]
            current = await self._download_current()
            _response_cache[self._url] = (mono, current)
            return current

    async def _download_current(self) -> dict:
        """Fetch this location's forecast and return its `current` block."""
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(self._url) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
                return payload["current"]

    @staticmethod
    def _extract(current: dict | None, field: str | None):
        """Extract one field from current dict, or None if unavailable."""
        if current is None or field is None:
            return None
        return current.get(field)
