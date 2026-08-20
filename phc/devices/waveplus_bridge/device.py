"""WavePlusBridgeDevice: reads live radon/air-quality readings for one
Airthings Wave Plus unit from a shared WavePlus_Bridge HTTP endpoint."""

import asyncio
import time

import aiohttp

from phc.core.device import Device
from phc.core.intervals import parse_duration
from phc.core.registry import register_module

# Shared by every WavePlusBridgeDevice instance pointed at the same bridge:
# several sensor units behind one bridge share one JSON payload, so caching
# by base_url means concurrently-due sensor devices coalesce into one HTTP
# GET instead of each independently re-fetching the identical response --
# same pattern as phc/devices/meteoswiss's _csv_cache, just JSON instead of CSV.
_response_cache: dict[str, tuple[float, dict]] = {}   # base_url -> (fetched_at, payload)
_response_cache_lock = asyncio.Lock()


@register_module("waveplus_bridge")
class WavePlusBridgeDevice(Device):
    """One Airthings Wave Plus sensor unit via a WavePlus_Bridge HTTP server.
    Fetches shared bridge payload, selects this device's sensor by sensor_id,
    and marks it unavailable if its reading (per update_time vs current_time)
    is stale, even if the bridge itself is healthy. Read-only.
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
        """Fetch shared bridge payload, select+validate this device's sensor,
        return {endpoint_key: value}."""
        try:
            payload = await self._get_payload()
        except (TimeoutError, aiohttp.ClientError) as exc:
            # The fetch itself still "succeeds" (every endpoint just
            # reports None), so this device only registers as unhealthy
            # if the failure is reported explicitly -- see
            # Device.report_failure.
            self.report_failure(f"{type(exc).__name__}: {exc}")
            payload = None
        sensor = self._select_sensor(payload)
        return {key: self._extract(sensor, ep.params.get("field"))
                for key, ep in self.endpoints.items()}

    def _select_sensor(self, payload: dict | None) -> dict | None:
        """Return this device's sensor sub-dict, or None if bridge is
        unreachable, sensor_id missing, or sensor reading is stale
        (current_time - update_time > data_validity_time). If either
        timestamp is missing, staleness check is skipped (fails open)."""
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
        """Return shared bridge payload, reusing cached copy if fresh.
        Uses double-checked locking to avoid cache stampedes."""
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
        """Return one field from sensor dict, or None if unavailable."""
        if sensor is None or field is None:
            return None
        return sensor.get(field)
