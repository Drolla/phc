"""MeteoSwissDevice: reads live measurements for one MeteoSwiss automatic
weather station from the shared, periodically-published SwissMetNet CSV."""

import asyncio
import csv
import time

import aiohttp

from core.device import Device
from core.intervals import parse_duration
from core.registry import register_module

# Shared by every MeteoSwissDevice instance: all stations read the same
# published CSV (data_url has scope: module in module.yaml, i.e. one value
# for every instance of this module), so caching by URL means
# concurrently-due stations coalesce into one HTTP GET instead of each
# independently re-downloading the identical file.
_csv_cache: dict[str, tuple[float, str]] = {}   # data_url -> (fetched_at, text)
_csv_cache_lock = asyncio.Lock()


@register_module("meteoswiss")
class MeteoSwissDevice(Device):
    """One MeteoSwiss automatic weather station. Native-async: receive_async()
    awaits the shared SwissMetNet CSV (all stations, refreshed by MeteoSwiss
    every ~10 minutes) over aiohttp -- awaited directly on the event loop, not
    offloaded to a worker thread like the default Device bridge -- and picks
    out this device's station row. The downloaded CSV text is cached (shared
    across every station device, keyed by data_url) for up to `cache_time`,
    so polling faster than MeteoSwiss's own refresh cadence, or configuring
    several stations, doesn't re-download identical content. Staging into
    endpoints and recursing into any children is handled generically by the
    base Device.fetch(). Read-only: transmit() is not overridden, so writes
    are simply dropped.

    Data set: https://opendata.swiss/en/dataset/automatische-wetterstationen-aktuelle-messwerte
    Station codes: https://data.geo.admin.ch/ch.meteoschweiz.messwerte-aktuell/info/VQHA80_en.txt
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
        """Async counterpart of the base receive(): await the station row over
        aiohttp and return it as {endpoint_key: raw_value}."""
        try:
            row = await self._fetch_station_row()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # Network/HTTP failure or the request's own 10s timeout: report
            # every endpoint as unavailable, as the previous urllib version did
            # on OSError. (If the Scheduler's fetch_timeout fires instead, it
            # cancels this coroutine -- that CancelledError is not caught here,
            # so the fetch is cleanly abandoned.)
            row = None
        return {key: self._extract(row, ep.parameters.get("column"))
                for key, ep in self.endpoints.items()}

    async def _fetch_station_row(self) -> dict | None:
        """Return this device's row from the shared CSV, or None if the
        station code isn't present in it."""
        text = await self._get_csv_text()
        reader = csv.DictReader(text.splitlines(), delimiter=";")
        for row in reader:
            if row["Station/Location"] == self._station:
                return row
        return None

    async def _get_csv_text(self) -> str:
        """Return the shared CSV text, reusing a cached copy if it is still
        within this device's own cache_time. The cache itself is shared by
        data_url, but freshness is judged per caller -- so two stations may
        configure different cache_time values against the same cached fetch
        without needing to reconcile whose value "wins"."""
        now = time.monotonic()
        cached = _csv_cache.get(self._data_url)
        if cached is not None and (now - cached[0]) < self._cache_time:
            return cached[1]
        async with _csv_cache_lock:
            # Re-check: another device may have refreshed the cache while
            # this one was waiting for the lock.
            now = time.monotonic()
            cached = _csv_cache.get(self._data_url)
            if cached is not None and (now - cached[0]) < self._cache_time:
                return cached[1]
            text = await self._download_csv()
            _csv_cache[self._data_url] = (now, text)
            return text

    async def _download_csv(self) -> str:
        """Download the shared SwissMetNet CSV and return it as text."""
        # A short-lived session per download is fine here: even uncached,
        # this fetches on the order of once every ~10 minutes, so there is no
        # connection reuse to gain, and it avoids holding an open session
        # across the device's whole lifetime (which would need an async
        # teardown hook the framework doesn't have).
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(self._data_url) as response:
                response.raise_for_status()
                return await response.text(encoding="utf-8")

    @staticmethod
    def _extract(row: dict | None, column: str | None):
        """Pull one numeric value out of `row` by CSV column name, returning
        None for a missing row/column or MeteoSwiss's own "-"/empty markers."""
        if row is None or column is None:
            return None
        value = row.get(column, "-")
        if value in ("", "-"):
            return None
        try:
            return float(value)
        except ValueError:
            return None
