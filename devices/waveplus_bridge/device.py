"""WavePlusBridgeDevice: reads live radon/air-quality readings for one
Airthings Wave Plus unit from a shared WavePlus_Bridge HTTP endpoint."""

import asyncio
import time

import aiohttp

from core.device import Device
from core.intervals import parse_duration
from core.registry import register_module

# Shared by every WavePlusBridgeDevice instance pointed at the same bridge:
# several sensor units behind one bridge share one JSON payload, so caching
# by base_url means concurrently-due sensor devices coalesce into one HTTP
# GET instead of each independently re-fetching the identical response --
# same pattern as devices/meteoswiss's _csv_cache, just JSON instead of CSV.
_response_cache: dict[str, tuple[float, dict]] = {}   # base_url -> (fetched_at, payload)
_response_cache_lock = asyncio.Lock()


@register_module("waveplus_bridge")
class WavePlusBridgeDevice(Device):
    """One Airthings Wave Plus sensor unit, read via a WavePlus_Bridge HTTP
    server (https://github.com/Drolla/WavePlus_Bridge). Native-async, same
    shape as MeteoSwissDevice: receive_async() awaits the shared bridge
    payload over aiohttp, picks out this device's sensor_id sub-dict, and
    additionally treats that sub-dict as unavailable if its own update_time
    is stale relative to the bridge's reported current_time -- even a
    reachable, healthy bridge can be carrying a stale reading for one
    specific unit (dead battery, out of BLE range) while its other units
    keep working. Read-only: transmit() is not overridden.
    """

    def setup(self):
        """Read this device's resolved params."""
        self._base_url = self.params["base_url"]
        self._sensor_id = self.params["sensor_id"]
        # .get(..., default) mirrors module.yaml's defaults for devices
        # constructed directly (bypassing load_system()/_merge_params), e.g.
        # in tests.
        self._cache_time = parse_duration(self.params.get("cache_time", "60s"))
        self._data_validity_time = parse_duration(self.params.get("data_validity_time", "5m"))

    async def receive_async(self) -> dict:
        """Async counterpart of the base receive(): await the shared bridge
        payload, select+validate this device's sensor, and return
        {endpoint_key: raw_value}."""
        try:
            payload = await self._get_payload()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # Network/HTTP failure or the request's own 10s timeout: report
            # every endpoint as unavailable, mirroring meteoswiss/open_meteo.
            payload = None
        sensor = self._select_sensor(payload)
        return {key: self._extract(sensor, ep.params.get("field"))
                for key, ep in self.endpoints.items()}

    def _select_sensor(self, payload: dict | None) -> dict | None:
        """Return this device's sensor sub-dict from `payload`, or None if
        the bridge is unreachable, this sensor_id isn't present, or this
        sensor's own reading is stale (bridge current_time minus this
        sensor's update_time exceeds data_validity_time) -- reproducing the
        old THC module's per-sensor staleness check, distinct from a plain
        fetch failure. If either timestamp is missing, the staleness check
        is skipped (fails open) rather than treated as stale."""
        if payload is None:
            return None
        sensor = (payload.get("devices") or {}).get(self._sensor_id)
        if sensor is None:
            return None
        current_time = payload.get("current_time")
        update_time = sensor.get("update_time")
        if current_time is not None and update_time is not None:
            if (current_time - update_time) > self._data_validity_time:
                return None
        return sensor

    async def _get_payload(self) -> dict:
        """Return the shared bridge payload, reusing a cached copy if it is
        still within this device's own cache_time -- same double-checked
        locking as meteoswiss's _get_csv_text/open_meteo's _get_current."""
        now = time.monotonic()
        cached = _response_cache.get(self._base_url)
        if cached is not None and (now - cached[0]) < self._cache_time:
            return cached[1]
        async with _response_cache_lock:
            now = time.monotonic()
            cached = _response_cache.get(self._base_url)
            if cached is not None and (now - cached[0]) < self._cache_time:
                return cached[1]
            payload = await self._download_payload()
            _response_cache[self._base_url] = (now, payload)
            return payload

    async def _download_payload(self) -> dict:
        """Fetch the shared bridge response and return the parsed payload."""
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(self._base_url) as response:
                response.raise_for_status()
                return await response.json(content_type=None)

    @staticmethod
    def _extract(sensor: dict | None, field: str | None):
        """Pull one value out of `sensor` by JSON field name, or None if the
        sensor or field is unavailable."""
        if sensor is None or field is None:
            return None
        return sensor.get(field)
