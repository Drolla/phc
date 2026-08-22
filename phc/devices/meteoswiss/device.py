"""MeteoSwissDevice: live measurements for one MeteoSwiss weather station.

From the shared, periodically-published SwissMetNet CSV."""

import asyncio
import csv
import time

import aiohttp

from phc.core.device import Device
from phc.core.intervals import parse_duration
from phc.core.registry import register_module

# Shared by every MeteoSwissDevice instance: all stations read the same
# published CSV (data_url has scope: module in module.yaml, i.e. one value
# for every instance of this module), so caching by URL means
# concurrently-due stations coalesce into one HTTP GET instead of each
# independently re-downloading the identical file.
_csv_cache: dict[str, tuple[float, str]] = {}   # data_url -> (fetched_at, text)
_csv_cache_lock = asyncio.Lock()


@register_module("meteoswiss")
class MeteoSwissDevice(Device):
    """One MeteoSwiss weather station from the shared SwissMetNet CSV.

    Async, cached per data_url to batch multiple stations into one
    HTTP request. Read-only.
    """

    def setup(self):
        """Read this device's resolved params (data_url, station, cache_time)."""
        self._data_url = self.params["data_url"]
        self._station = self.params["station"]
        # .get(..., "10m") mirrors module.yaml's default for devices
        # constructed directly (bypassing load_system()/_merge_params), e.g.
        # in tests.
        self._cache_time = parse_duration(self.params.get("cache_time", "10m"))

    async def receive_async(self) -> dict:
        """Fetch this station's row from CSV, return {endpoint_key: value}."""
        try:
            row = await self._fetch_station_row()
        except (TimeoutError, aiohttp.ClientError) as exc:
            # The fetch itself still "succeeds" (every endpoint just
            # reports None), so this device only registers as unhealthy
            # if the failure is reported explicitly -- see
            # Device.report_failure.
            self.report_failure(f"{type(exc).__name__}: {exc}")
            row = None
        return {key: self._extract(row, ep.params.get("column"))
                for key, ep in self.endpoints.items()}

    async def _fetch_station_row(self) -> dict | None:
        """Return this device's row from the shared CSV.

        None if the station code isn't present in it."""
        text = await self._get_csv_text()
        reader = csv.DictReader(text.splitlines(), delimiter=";")
        for row in reader:
            if row["Station/Location"] == self._station:
                return row
        return None

    async def _get_csv_text(self) -> str:
        """Return CSV text, reusing cached copy if fresh.

        Cache is shared by data_url; freshness is per-caller (per-device
        cache_time)."""
        mono = time.monotonic()
        cached = _csv_cache.get(self._data_url)
        if cached is not None and (mono - cached[0]) < self._cache_time:
            return cached[1]
        async with _csv_cache_lock:
            # Re-check: another device may have refreshed the cache while
            # this one was waiting for the lock.
            mono = time.monotonic()
            cached = _csv_cache.get(self._data_url)
            if cached is not None and (mono - cached[0]) < self._cache_time:
                return cached[1]
            text = await self._download_csv()
            _csv_cache[self._data_url] = (mono, text)
            return text

    async def _download_csv(self) -> str:
        """Download CSV and return as text."""
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(self._data_url) as response:
                response.raise_for_status()
                return await response.text(encoding="utf-8")

    @staticmethod
    def _extract(row: dict | None, column: str | None):
        """Extract one numeric value from row.

        None for missing row/column or MeteoSwiss markers ("-"/empty)."""
        if row is None or column is None:
            return None
        value = row.get(column, "-")
        if value in ("", "-"):
            return None
        try:
            return float(value)
        except ValueError:
            return None
