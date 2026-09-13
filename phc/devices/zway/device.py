"""ZWayDevice: virtual devices on a Z-Way controller over WebSocket.

Thin adapter over phc.devices.zway.zway_ws, which owns the protocol
and knows nothing about PHC. One PHC device groups any set of the
controller's virtual devices, one per endpoint."""

import asyncio

from phc.core.device import Device
from phc.core.intervals import parse_duration
from phc.core.registry import register_module
from phc.devices.zway.zway_ws import (
    ZWayConnection,
    ZWayError,
    command_for,
    to_phc,
)


class _ZWayState:
    """The connections one system's zway devices share.

    Per-system rather than per-process (see phc.core.device.Device.context):
    a connection outliving its System would carry a stale device cache into
    the next one, and its asyncio primitives are bound to the event loop it
    was built on. Holds no asyncio objects itself, so it can be built in
    sync setup() before any loop exists."""

    def __init__(self):
        # (url, token, id(loop)) -> (loop, connection). Keyed by loop as well
        # because each Scheduler runs its own, and a connection may never be
        # used from a loop other than the one whose primitives it holds.
        self.connections: dict[tuple[str, str, int],
                               tuple[asyncio.AbstractEventLoop, ZWayConnection]] = {}

    def conn_for(self, url: str, token: str, **options) -> ZWayConnection:
        """Return the shared connection for this controller on this loop.

        Only call with a running loop: this is where the connection (and
        its asyncio primitives) get built."""
        loop = asyncio.get_running_loop()
        # id() can be reused once a loop is collected, so the loop itself is
        # kept and compared rather than trusting the key alone.
        key = (url, token, id(loop))
        existing = self.connections.get(key)
        if existing is not None and existing[0] is loop:
            return existing[1]
        connection = ZWayConnection(url, token, **options)
        self.connections[key] = (loop, connection)
        return connection


@register_module("zway")
class ZWayDevice(Device):
    """Virtual devices on one Z-Way controller.

    Each endpoint names a virtual device with `device:` (an id/title
    glob). Reads come from the connection's cache, which its push events
    keep current; writes become the command the device's own deviceType
    defines."""

    def setup(self):
        """Read params and record each endpoint's device pattern.

        Nothing is resolved or connected here: setup() runs before any
        event loop exists, and the controller's device list -- which
        patterns match against -- is only known once connected."""
        state = self.context.get("zway")
        if state is None:
            # get-then-assign, not setdefault, which would build a state
            # object per device just to discard all but the first.
            state = self.context["zway"] = _ZWayState()
        self._state = state

        self._url = self.params["url"]
        self._token = self.params["token"]
        # The .get() defaults mirror module.yaml, for devices built directly
        # rather than through load_system() (tests, scripts).
        self._options = {
            "request_timeout": parse_duration(self.params.get("request_timeout", "5s")),
            "resync_interval": parse_duration(self.params.get("resync_interval", "5m")),
        }

        self._patterns = {
            key: (endpoint.params.get("device"), bool(endpoint.params.get("match_case")))
            for key, endpoint in self.endpoints.items()
        }
        # endpoint key -> device id, filled in by _bind once connected.
        self._device_ids: dict[str, str] = {}
        self._bound_generation = -1

    def _bind(self, connection: ZWayConnection):
        """Resolve every endpoint's pattern to a device id, once.

        A no-op unless the connection has re-read its device list, so
        globs are matched once per connection rather than per access --
        and a device renamed on the controller is picked up on the next
        reconnect or resync."""
        if self._bound_generation == connection.generation:
            return
        self._bound_generation = connection.generation
        self._device_ids = {}
        for key, (pattern, match_case) in self._patterns.items():
            if not pattern:
                self.report_failure(f"endpoint {key!r} has no device: pattern")
                continue
            try:
                self._device_ids[key] = connection.resolve(pattern, match_case)
            except ZWayError as exc:
                # One unmatched pattern must not cost this device its other
                # endpoints, nor the controller its connection.
                self.report_failure(str(exc))

    async def receive_async(self) -> dict:
        """Return {endpoint_key: value} from the connection's cache."""
        connection = self._state.conn_for(self._url, self._token, **self._options)
        if not await connection.ensure_started():
            self.report_failure(f"z-way {self._url} unavailable: {connection.last_error}")
            return dict.fromkeys(self.endpoints)

        await connection.resync_if_due()
        self._bind(connection)

        values: dict[str, object] = {}
        for key in self.endpoints:
            device_id = self._device_ids.get(key)
            if device_id is None:
                values[key] = None
                continue
            values[key] = to_phc(connection.levels.get(device_id),
                                 connection.types.get(device_id, ""))
        return values

    async def transmit_async(self, state: dict) -> None:
        """Write endpoint values as the commands their device types define."""
        connection = self._state.conn_for(self._url, self._token, **self._options)
        if not await connection.ensure_started():
            self.report_failure(f"z-way {self._url} unavailable: {connection.last_error}")
            return

        self._bind(connection)
        for key, value in state.items():
            device_id = self._device_ids.get(key)
            if device_id is None:
                self.report_failure(f"endpoint {key!r} is not bound to a z-way device")
                continue
            try:
                # Validated here because the controller accepts nonsense
                # commands with 200 OK -- see connection.command_for.
                command = command_for(value, connection.types.get(device_id, ""))
                await connection.command(device_id, command)
            except ZWayError as exc:
                self.report_failure(f"write to {device_id} failed: {exc}")
