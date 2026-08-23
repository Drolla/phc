"""TemplateDevice: a complete, working device module -- copy this to start a new one.

This is the canonical starting point for a new PHC device module. It is
real, running, tested code rather than a skeleton: every framework pattern
below (shared state, caching, failure reporting, endpoint parameters,
reads and writes) is the way a production module does it, so the parts you
keep are already correct.

It simulates an "Acme Hub" -- a fictional controller serving several
sensor/actuator units over one connection -- so that it needs no hardware
and no network. ALL of the fake I/O is confined to the two methods under
the SIMULATED HARDWARE banner near the bottom of this file. Replacing them
with real I/O is the single edit that turns this into a real module.

To use it:

1. Copy this directory to phc/devices/<your_name>/ (bundled) or into a
   `plugin_paths:` directory (out-of-tree -- see the "Shipping a Module
   Outside PHC" section of docs/developer/writing-a-device-module.md).
2. Rename the module in @register_module(), the class, and module.yaml.
3. Replace _read_payload()/_write_payload() with your real protocol.
4. Delete what your device does not need -- a read-only device drops the
   whole write half, a device with nothing to share between instances
   drops _TemplateState and self.context.
5. Delete the `simulate_failure` parameter, `_SIMULATED_UNITS`, and every
   comment marked SIMULATION ONLY.

See docs/developer/writing-a-device-module.md for the reference
explanation of everything used here.
"""

import asyncio
import time

from phc.core.device import Device
from phc.core.intervals import parse_duration
from phc.core.registry import register_module

# SIMULATION ONLY -- the readings the fake hub reports for each unit before
# anything is written to it. Delete with _read_payload()/_write_payload().
_SIMULATED_UNITS = {
    "hall": {"temp": 21.5, "hum": 44.0, "relay": 0},
    "cellar": {"temp": 12.0, "hum": 71.0, "relay": 0},
}


class _TemplateState:
    """State shared by every device of this module within ONE system.

    A module usually needs something shared between its own device
    instances: a session, a connection pool, or -- as here -- a response
    cache that lets sibling devices behind the same controller coalesce
    their reads into a single request per poll.

    This lives in the shared device context (see
    phc.core.device.Device.context), NOT at module scope. Module-level
    state is created once per process and so is shared by every system
    loaded in it, which is the wrong lifetime: it outlives the System it
    belongs to, leaks between two systems loaded together, and forces
    tests to reach in and reset module internals by hand. An asyncio.Lock
    is especially bad there -- it binds to the first event loop that
    contends for it, then raises "bound to a different event loop" against
    any later one.

    Delete this class entirely if your module's devices share nothing.
    """

    def __init__(self):
        # host -> (fetched_at_monotonic, payload)
        self.cache: dict[str, tuple[float, dict]] = {}
        self.lock = asyncio.Lock()
        # SIMULATION ONLY (delete with _read_payload()/_write_payload()):
        # the fake hub's mutable state, and a counter the tests use to
        # prove that two sibling devices really did coalesce into one read.
        self.hub: dict[str, dict] = {}
        self.reads = 0


@register_module("device_template")
class TemplateDevice(Device):
    """One unit behind an Acme Hub controller.

    Devices sharing a `host` batch their reads into a single fetch per
    poll (see _TemplateState). Reads temperature/humidity, writes a relay.
    """

    def setup(self):
        """Resolve this device's params and join the system-wide shared state.

        setup() runs once per device, before the Scheduler starts, and is
        the only place self.endpoints/self.params are guaranteed built.
        """
        # One _TemplateState per system, shared by every device of this
        # module in it. `get`-then-assign rather than setdefault(): the
        # latter would build (and immediately discard) a fresh
        # asyncio.Lock for every device after the first.
        state = self.context.get("device_template")
        if state is None:
            state = self.context["device_template"] = _TemplateState()
        self._state = state

        self._host = self.params["host"]
        self._unit = self.params["unit"]
        # .get(..., default) mirrors module.yaml's declared defaults for a
        # device constructed directly (bypassing load_system()'s parameter
        # merge), e.g. in a unit test.
        self._cache_time = parse_duration(self.params.get("cache_time", "10s"))
        self._request_timeout = parse_duration(self.params.get("request_timeout", "5s"))
        # SIMULATION ONLY -- delete this parameter and its module.yaml entry.
        self._simulate_failure = bool(self.params.get("simulate_failure", False))

    async def receive_async(self) -> dict:
        """Read this unit's values -> {endpoint_key: raw_value}.

        Override receive_async() (not receive()) when the I/O is genuinely
        async-friendly -- an aiohttp client, an async SDK. Unlike a
        blocking call bridged onto a worker thread, a native coroutine is
        actually cancellable on timeout. See the BLOCKING ALTERNATIVE at
        the bottom of this file for the synchronous version.
        """
        try:
            payload = await self._get_payload()
        except OSError as exc:
            # Catching the error and reporting every endpoint as None keeps
            # one unreachable controller from disturbing the whole tick.
            # From the Scheduler's side that is indistinguishable from a
            # completely successful fetch, so the failure has to be
            # reported explicitly for this device to register as unhealthy
            # (see Device.report_failure). Without this call, health would
            # never trip for exactly the modules it matters most for.
            self.report_failure(f"{type(exc).__name__}: {exc}")
            payload = None
        unit = (payload or {}).get(self._unit)
        # Each endpoint names the field it reads via its `channel` endpoint
        # parameter, declared under endpoint_parameters: in module.yaml --
        # so adding an endpoint is a YAML change, not a code change.
        return {key: self._extract(unit, ep.params.get("channel"))
                for key, ep in self.endpoints.items()}

    async def transmit_async(self, state: dict) -> None:
        """Push a write to the hub. `state` is {endpoint_key: value}.

        Only ever called with the writable endpoints that were actually
        written during this tick. Delete this method (and the `relay`
        endpoint) for a read-only device -- the base class's no-op is then
        exactly right.
        """
        for key, value in state.items():
            channel = self.endpoints[key].params.get("channel")
            if channel is None:
                continue
            await self._write_payload(channel, value)
        # A write makes the cached payload stale: drop it so the next poll
        # reads the value back from the hub rather than serving the copy
        # taken before the write landed.
        self._state.cache.pop(self._host, None)

    async def _get_payload(self) -> dict:
        """Return the hub's payload, reusing a cached copy while it is fresh.

        Double-checked locking: the fast path outside the lock keeps
        uncontended polls cheap, and the re-check inside it means that when
        several sibling devices come due in the same tick, exactly one of
        them performs the fetch and the rest reuse its result.
        """
        mono = time.monotonic()
        cached = self._state.cache.get(self._host)
        if cached is not None and (mono - cached[0]) < self._cache_time:
            return cached[1]
        async with self._state.lock:
            # Re-check: another device may have refreshed the cache while
            # this one waited for the lock.
            mono = time.monotonic()
            cached = self._state.cache.get(self._host)
            if cached is not None and (mono - cached[0]) < self._cache_time:
                return cached[1]
            payload = await self._read_payload()
            # Only a successful read reaches here, so a failure never
            # populates the cache and the next poll retries instead of
            # serving an error for a whole cache_time.
            self._state.cache[self._host] = (mono, payload)
            return payload

    @staticmethod
    def _extract(unit: dict | None, channel: str | None):
        """Return one channel's value from a unit's readings.

        None whenever the value is unavailable -- unreachable hub, unknown
        unit, or an endpoint whose `channel` was never declared. PHC
        renders a None reading as unavailable rather than as a value.
        """
        if unit is None or channel is None:
            return None
        return unit.get(channel)

    # ------------------------------------------------------------------
    # SIMULATED HARDWARE -- REPLACE EVERYTHING BELOW THIS BANNER
    #
    # These two methods are the module's entire I/O surface. Swap their
    # bodies for your real protocol (an HTTP request, a serial exchange, a
    # vendor SDK call) and the rest of this file needs no changes.
    #
    # A real async implementation looks like:
    #
    #     import aiohttp
    #
    #     async def _read_payload(self) -> dict:
    #         timeout = aiohttp.ClientTimeout(total=self._request_timeout)
    #         async with aiohttp.ClientSession(timeout=timeout) as session:
    #             async with session.get(f"http://{self._host}/units") as response:
    #                 response.raise_for_status()
    #                 return await response.json(content_type=None)
    #
    # and would catch aiohttp.ClientError/TimeoutError in receive_async()
    # above instead of the OSError this simulation raises.
    # ------------------------------------------------------------------

    async def _read_payload(self) -> dict:
        """SIMULATION ONLY: return every unit's readings from the fake hub."""
        if self._simulate_failure:
            raise OSError(f"cannot reach Acme Hub at {self._host}")
        self._state.reads += 1
        return self._hub()

    async def _write_payload(self, channel: str, value) -> None:
        """SIMULATION ONLY: store a written value in the fake hub."""
        if self._simulate_failure:
            raise OSError(f"cannot reach Acme Hub at {self._host}")
        self._hub()[self._unit][channel] = value

    def _hub(self) -> dict:
        """SIMULATION ONLY: this host's fake hub, seeded on first access."""
        hub = self._state.hub.get(self._host)
        if hub is None:
            hub = self._state.hub[self._host] = {
                unit: dict(readings) for unit, readings in _SIMULATED_UNITS.items()
            }
        return hub


# ----------------------------------------------------------------------
# BLOCKING ALTERNATIVE
#
# If your device's I/O is blocking -- a serial port, a synchronous vendor
# SDK -- override receive()/transmit() instead of the _async pair above.
# PHC bridges them onto a worker thread automatically, so you write plain
# synchronous code and still get concurrency. Override one pair or the
# other for a given direction, never both.
#
#     def receive(self) -> dict:
#         try:
#             payload = self._client.read_all()
#         except SerialException as exc:
#             self.report_failure(f"{type(exc).__name__}: {exc}")
#             return {key: None for key in self.endpoints}
#         return {key: payload.get(ep.params.get("channel"))
#                 for key, ep in self.endpoints.items()}
#
#     def transmit(self, state: dict) -> None:
#         for key, value in state.items():
#             self._client.write(self.endpoints[key].params.get("channel"), value)
#
# Note that a device overriding only transmit_async() (as this template
# does) does not receive writes issued by a bare set() outside a scheduler
# tick -- use set_text_async() there. A device overriding transmit() has no
# such caveat. See phc/core/device.py's _emit()/_emit_async().
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# ENDPOINT AND DEVICE PROFILES
#
# This module's module.yaml declares its endpoints explicitly, which is the
# right choice for a module serving one shape of device. A module
# supporting many similar products can instead ship a reusable profile
# library (endpoint_profiles/device_profiles) and let a config opt in per
# device -- but device_profiles is mutually exclusive with a non-empty
# `endpoints:` list on the same module, so that is an either/or decision,
# not an addition. See phc/devices/zway/module.yaml for a full-size example
# and docs/profiles.md for the reference.
# ----------------------------------------------------------------------
