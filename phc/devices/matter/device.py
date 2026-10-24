"""MatterDevice: Matter nodes behind a matterjs-server, over WebSocket.

Thin adapter over phc.devices.matter.matter_ws, which owns the protocol
and knows nothing about PHC. One PHC device groups any set of nodes'
attributes, one per endpoint."""

import asyncio

from phc.core.device import Device
from phc.core.intervals import parse_duration
from phc.core.registry import register_module
from phc.devices.matter.matter_ws import (
    MatterConnection,
    MatterError,
    to_phc,
    write_for,
)

# Endpoint number used when an endpoint doesn't set `endpoint:`; the first
# application endpoint of nearly every single-function Matter device.
DEFAULT_ENDPOINT = 1


class _MatterState:
    """The connections one system's matter devices share.

    Per-system rather than per-process (see phc.core.device.Device.context):
    a connection outliving its System would carry a stale node cache into
    the next one, and its asyncio primitives are bound to the event loop it
    was built on. Holds no asyncio objects itself, so it can be built in
    sync setup() before any loop exists."""

    def __init__(self):
        # (url, id(loop)) -> (loop, connection). Keyed by loop as well
        # because each Scheduler runs its own, and a connection may never be
        # used from a loop other than the one whose primitives it holds.
        self.connections: dict[tuple[str, int],
                               tuple[asyncio.AbstractEventLoop, MatterConnection]] = {}

    def conn_for(self, url: str, **options) -> MatterConnection:
        """Return the shared connection for this server on this loop.

        Only call with a running loop: this is where the connection (and
        its asyncio primitives) get built."""
        loop = asyncio.get_running_loop()
        # id() can be reused once a loop is collected, so the loop itself is
        # kept and compared rather than trusting the key alone.
        key = (url, id(loop))
        existing = self.connections.get(key)
        if existing is not None and existing[0] is loop:
            return existing[1]
        connection = MatterConnection(url, **options)
        self.connections[key] = (loop, connection)
        return connection


@register_module("matter")
class MatterDevice(Device):
    """Attributes of Matter nodes on one matterjs-server.

    Each endpoint names a node with `node:` (a label/serial glob or a node
    id), set on the device or overridden per endpoint, and an attribute
    with `endpoint:` + `attribute:`. Reads come from the connection's
    cache, which the server's push events keep current; writes become the
    cluster command the endpoint declares."""

    def setup(self):
        """Read params and record each endpoint's node and attribute.

        Nothing is resolved or connected here: setup() runs before any
        event loop exists, and the node list -- which patterns match
        against -- is only known once connected."""
        state = self.context.get("matter")
        if state is None:
            # get-then-assign, not setdefault, which would build a state
            # object per device just to discard all but the first.
            state = self.context["matter"] = _MatterState()
        self._state = state

        self._url = self.params["url"]
        # The .get() defaults mirror module.yaml, for devices built directly
        # rather than through load_system() (tests, scripts).
        self._options = {
            "request_timeout": parse_duration(self.params.get("request_timeout", "10s")),
            "resync_interval": parse_duration(self.params.get("resync_interval", "5m")),
        }

        default_node = self.params.get("node")
        # key -> (node pattern, match_case, "endpoint/cluster/attribute")
        self._specs: dict[str, tuple] = {}
        for key, endpoint in self.endpoints.items():
            params = endpoint.params
            attribute = params.get("attribute")
            path = None if attribute is None else (
                f"{params.get('endpoint', DEFAULT_ENDPOINT)}/{attribute}")
            self._specs[key] = (params.get("node", default_node),
                                bool(params.get("match_case")), path)
        # endpoint key -> node id, filled in by _bind once connected.
        self._node_ids: dict[str, int] = {}
        self._bound_generation = -1

    def _bind(self, connection: MatterConnection):
        """Resolve every endpoint's node pattern to a node id, once.

        A no-op unless the connection's nodes changed, so globs are
        matched once per generation rather than per access -- and a node
        relabelled on the server is picked up as soon as the event
        arrives."""
        if self._bound_generation == connection.generation:
            return
        self._bound_generation = connection.generation
        self._node_ids = {}
        for key, (pattern, match_case, path) in self._specs.items():
            if pattern is None or path is None:
                self.report_failure(f"endpoint {key!r} needs a node and an attribute")
                continue
            try:
                self._node_ids[key] = connection.resolve(pattern, match_case)
            except MatterError as exc:
                # One unmatched pattern must not cost this device its other
                # endpoints, nor the server its connection.
                self.report_failure(str(exc))

    async def receive_async(self) -> dict:
        """Return {endpoint_key: value} from the connection's cache."""
        connection = self._state.conn_for(self._url, **self._options)
        if not await connection.ensure_started():
            self.report_failure(f"matter {self._url} unavailable: {connection.last_error}")
            return dict.fromkeys(self.endpoints)

        await connection.resync_if_due()
        self._bind(connection)

        values: dict[str, object] = {}
        for key, endpoint in self.endpoints.items():
            node_id = self._node_ids.get(key)
            if node_id is None:
                values[key] = None
                continue
            raw = connection.read(node_id, self._specs[key][2])
            values[key] = to_phc(raw, endpoint.params, endpoint.value_type)
        return values

    async def transmit_async(self, state: dict) -> None:
        """Write endpoint values as the commands their endpoints declare."""
        connection = self._state.conn_for(self._url, **self._options)
        if not await connection.ensure_started():
            self.report_failure(f"matter {self._url} unavailable: {connection.last_error}")
            return

        self._bind(connection)
        for key, value in state.items():
            node_id = self._node_ids.get(key)
            if node_id is None:
                self.report_failure(f"endpoint {key!r} is not bound to a matter node")
                continue
            params = self.endpoints[key].params
            cluster = int(self._specs[key][2].split("/")[1])
            try:
                # Validated here, before anything reaches the wire -- see
                # matter_ws.write_for.
                name, payload = write_for(value, params)
                await connection.command(
                    node_id, int(params.get("endpoint", DEFAULT_ENDPOINT)),
                    cluster, name, payload)
            except MatterError as exc:
                self.report_failure(f"write to node {node_id} failed: {exc}")
