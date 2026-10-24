"""WebSocket client for a matterjs-server (Open Home Foundation Matter server).

Deliberately free of any PHC imports: this is a plain protocol client,
driven by phc.devices.matter.device for PHC and by demo.py on its own.

Every request is `{message_id, command, args}`; the reply echoes the
`message_id` and carries either `result` or `error_code` + `details`.
The server first sends one unsolicited server-info frame, then nothing
until `start_listening`, whose result is the full node list. From then on
`{event, data}` frames keep the cache current.

Attribute values are cached as the server reports them, keyed by
"endpoint/cluster/attribute" paths. Struct values arrive as dicts keyed by
the numeric field tag as a string (e.g. {"0": 64000, "1": 2}), never by
field name -- see `field` in to_phc()."""

import asyncio
import fnmatch
import itertools
import json
import logging
import time
import weakref
from collections.abc import Callable
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

# Reconnect delays, in seconds; the last one repeats forever.
BACKOFF = (1.0, 10.0, 60.0, 300.0)

# Basic Information cluster (0x28): NodeLabel and SerialNumber, on endpoint 0.
LABEL_PATH = "0/40/5"
SERIAL_PATH = "0/40/15"


def _release(connector):
    """Drop a connector's transports without needing a running loop.

    aiohttp's own cleanup schedules work on the loop, which on PHC
    shutdown is already closed; this is the part that can still run."""
    try:
        connector._close()
    except Exception:                                     # noqa: BLE001 - best effort at exit
        pass


class MatterError(Exception):
    """Any failure talking to the server, or translating a value."""


class MatterRequestError(MatterError):
    """The server answered a request with an error."""

    def __init__(self, error_code: Any, details: str, command: str):
        super().__init__(f"{command} failed: {details or error_code}")
        self.error_code = error_code
        self.details = details
        self.command = command


class MatterLookupError(MatterError):
    """A node pattern matched no node, or more than one."""


def _as_number(value: Any, want_int: bool = False) -> int | float | None:
    """Convert a wire value to a number, or None if it isn't one."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if want_int else number


def to_phc(raw: Any, params: dict, value_type: str | None = None) -> Any:
    """Translate a wire attribute value into the PHC value.

    `params` are the endpoint's protocol fields: `field` picks a struct
    member, `mask` turns a bitmap into "any of these bits set", and
    `scale` multiplies a number. None for anything missing or
    unconvertible -- never a stand-in value."""
    if raw is None:
        return None
    field = params.get("field")
    if field is not None:
        raw = raw.get(str(field)) if isinstance(raw, dict) else None
        if raw is None:
            return None
    mask = params.get("mask")
    if mask is not None:
        bits = _as_number(raw, want_int=True)
        return None if bits is None else bool(int(bits) & int(mask))
    if value_type == "bool":
        if isinstance(raw, bool):
            return raw
        number = _as_number(raw)
        return None if number is None else number != 0
    number = _as_number(raw)
    if number is None:
        return None
    number *= params.get("scale", 1)
    # Rounded, not truncated: 99.99999 must read as 100.
    return round(number) if value_type == "int" else number


def write_for(value: Any, params: dict) -> tuple[str, dict]:
    """Return the (command name, payload) that writes `value`.

    A bool picks from `commands`; a number is sent as a level through
    `level_command`, divided by `scale` to get back the wire unit. Raises
    MatterError for a value the endpoint cannot take, which includes every
    write to an endpoint declaring neither -- the server would otherwise
    only reject it after a round trip."""
    commands = params.get("commands") or {}
    level_command = params.get("level_command")
    if isinstance(value, str):
        folded = value.strip().casefold()
        value = {"on": True, "true": True, "off": False, "false": False}.get(
            folded, _as_number(folded))
    if isinstance(value, bool):
        command = commands.get(value)
        if command is None:
            raise MatterError(f"cannot write {value!r} to this endpoint")
        return command, {}
    number = _as_number(value)
    if number is None:
        raise MatterError(f"cannot write {value!r} to this endpoint")
    if level_command is None:
        command = commands.get(bool(number))
        if command is None:
            raise MatterError(f"cannot write {value!r} to this endpoint")
        return command, {}
    return level_command, {
        "level": round(number / params.get("scale", 1)),
        "transitionTime": 0,
        "optionsMask": 0,
        "optionsOverride": 0,
    }


def match_nodes(pattern: str, names: dict[int, list[str]],
                match_case: bool = False) -> list[int]:
    """Return the node ids with a name matching the fnmatch `pattern`.

    `names` maps a node id to its label and serial number. Case-insensitive
    unless `match_case`: labels are hand-written, like Z-Way titles."""
    if not match_case:
        pattern = pattern.casefold()
    return [node_id for node_id, candidates in names.items()
            if any(fnmatch.fnmatchcase(c if match_case else c.casefold(), pattern)
                   for c in candidates)]


class MatterConnection:
    """One WebSocket to one Matter server, shared by everything using it.

    Owns every asyncio primitive it needs, so it must only be built with
    a running loop (see device.py's conn_for, which also keys them by
    loop). Not closed anywhere in PHC, which has no device teardown hook:
    the socket dies with its loop, and force_close keeps that quiet."""

    def __init__(self, url: str, *, request_timeout: float = 10.0,
                 resync_interval: float = 300.0):
        self.url = url
        self._request_timeout = request_timeout
        self._resync_interval = resync_interval

        self.server_info: dict = {}
        # Called with (event, data) after the cache has applied each event;
        # lets demo.py print the stream without a second socket.
        self.on_event: Callable[[str, Any], None] | None = None
        # node_id -> {"available": bool, "attributes": {path: value}}
        self.nodes: dict[int, dict] = {}
        # Bumped whenever a node appears, disappears or changes label, so
        # callers resolving patterns to node ids know when to re-resolve.
        self.generation = 0

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._finalizer: weakref.finalize | None = None
        self._reader: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._seq = itertools.count()
        self._lock = asyncio.Lock()

        self._connected = False
        self._attempt = 0
        self._retry_at = 0.0
        self._last_error: str | None = None
        self._last_resync = 0.0

    @property
    def connected(self) -> bool:
        """Whether the socket is up and the cache primed."""
        return self._connected

    @property
    def last_error(self) -> str | None:
        """The reason for the most recent failed connect, if any."""
        return self._last_error

    # ---------- connecting ----------

    async def ensure_started(self) -> bool:
        """Connect if needed; return whether the connection is usable.

        Returns False without attempting anything while the backoff delay
        has not elapsed -- that gate is what makes the ladder a ladder
        rather than a reconnect attempt on every poll."""
        if self._connected:
            return True
        if time.monotonic() < self._retry_at:
            return False
        async with self._lock:
            # Another caller may have connected while we waited.
            if self._connected:
                return True
            if time.monotonic() < self._retry_at:
                return False
            try:
                await self._connect()
            except Exception as exc:                      # noqa: BLE001 - any failure backs off
                self._fail(f"{type(exc).__name__}: {exc}")
                return False
            return True

    async def _connect(self):
        """Open the socket, read the server info and prime the cache.

        `start_listening` is part of connecting, not a later step: it is
        both what makes `nodes` valid from the first poll and what starts
        the event stream. A server that accepts the socket but never
        answers fails the connect by timeout."""
        session = aiohttp.ClientSession(
            # No teardown hook exists to close this, so avoid keeping sockets
            # alive past the loop that owns them.
            connector=aiohttp.TCPConnector(force_close=True),
        )
        try:
            ws = await session.ws_connect(self.url, heartbeat=30)
            async with asyncio.timeout(self._request_timeout):
                self.server_info = await ws.receive_json()
        except Exception:
            await session.close()
            raise

        self._session, self._ws = session, ws
        self._reader = asyncio.get_running_loop().create_task(self._read_loop())
        # PHC has no device teardown hook: on shutdown the Scheduler just
        # closes its loop, and aiohttp would then complain from __del__ about
        # a session whose loop is already gone. Releasing the connector's
        # transports needs no loop, so hang it off this connection's own
        # finalization instead.
        self._finalizer = weakref.finalize(self, _release, session.connector)
        # The reader is parked on a socket that dies with the loop; its
        # cancellation at that point is expected, not something to report.
        self._reader._log_destroy_pending = False
        try:
            self._prime(await self.request("start_listening"))
        except Exception:
            # Half-open: drop it so the next attempt starts clean rather than
            # reusing a socket whose cache was never filled.
            await self._teardown()
            raise

        self._connected = True
        self._attempt = 0
        self._retry_at = 0.0
        self._last_error = None
        log.info("matter %s connected, %d node(s)", self.url, len(self.nodes))

    def _prime(self, nodes: list[dict]):
        """Replace the cache with a full node list, and bump the generation."""
        self.nodes = {}
        for node in nodes or []:
            self._store(node)
        self.generation += 1
        self._last_resync = time.monotonic()

    def _store(self, node: dict):
        """Record one node object from a full read or a node event."""
        node_id = node.get("node_id")
        if node_id is None:
            return
        old_label = self.label(node_id)
        self.nodes[node_id] = {
            "available": bool(node.get("available", True)),
            "attributes": node.get("attributes") or {},
        }
        if old_label != self.label(node_id):
            self.generation += 1

    def _fail(self, error: str):
        """Record a failed connect/disconnect and arm the backoff ladder."""
        delay = BACKOFF[min(self._attempt, len(BACKOFF) - 1)]
        self._attempt += 1
        self._retry_at = time.monotonic() + delay
        self._last_error = error
        log.warning("matter %s unavailable (%s); retrying in %gs", self.url, error, delay)

    async def _teardown(self):
        """Drop the socket and session, leaving the cache empty."""
        reader, self._reader = self._reader, None
        ws, self._ws = self._ws, None
        session, self._session = self._session, None
        self._connected = False
        if self._finalizer is not None:
            # Closing properly below; no need for the at-exit fallback.
            self._finalizer.detach()
            self._finalizer = None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
            except Exception:                             # noqa: BLE001 - going away regardless
                pass
        if ws is not None:
            try:
                await ws.close()
            except Exception:                             # noqa: BLE001 - already going away
                pass
        if session is not None:
            try:
                await session.close()
            except Exception:                             # noqa: BLE001 - already going away
                pass
        self.nodes = {}

    async def close(self):
        """Shut the connection down.

        PHC never calls this -- it has no device teardown hook, so a
        connection there dies with its loop. Anything driving this class
        directly (demo.py, tests) should call it to leave no unclosed
        session behind."""
        await self._teardown()

    async def _on_disconnect(self, reason: str):
        """Handle the socket going away, from either side."""
        if not self._connected and self._ws is None:
            return
        for future in self._pending.values():
            if not future.done():
                # An exception, not cancel(): a CancelledError here would look
                # to the caller like the scheduler's own fetch timeout.
                future.set_exception(MatterError(f"connection lost: {reason}"))
        self._pending.clear()
        await self._teardown()
        self._fail(reason)

    # ---------- requests ----------

    async def request(self, command: str, **args) -> Any:
        """Send one command and return its `result`.

        Raises MatterRequestError for an error reply, MatterError if the
        socket is gone or the server never answers."""
        ws = self._ws
        if ws is None:
            raise MatterError("not connected")

        message_id = f"phc-{next(self._seq)}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        message: dict[str, Any] = {"message_id": message_id, "command": command}
        if args:
            message["args"] = args
        try:
            await ws.send_str(json.dumps(message))
            log.debug("matter %s --> %s %s", self.url, command, args)
            async with asyncio.timeout(self._request_timeout):
                frame = await future
        except TimeoutError:
            raise MatterError(
                f"no response within {self._request_timeout}s ({command})") from None
        finally:
            # Every exit path, or a timed-out request leaks its future and the
            # reader later resolves one nobody is waiting on.
            self._pending.pop(message_id, None)

        if "error_code" in frame:
            raise MatterRequestError(frame["error_code"], frame.get("details") or "", command)
        return frame.get("result")

    async def _read_loop(self):
        """Dispatch incoming frames until the socket closes.

        Never raises out: an exception here would surface as a lost task
        exception rather than the disconnect it actually is."""
        reason = "closed"
        try:
            ws = self._ws
            assert ws is not None
            async for message in ws:
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    frame = json.loads(message.data)
                except json.JSONDecodeError:
                    continue
                if isinstance(frame, dict):
                    self._dispatch(frame)
            reason = "closed by server"
        except asyncio.CancelledError:
            raise
        except Exception as exc:                          # noqa: BLE001 - report, never propagate
            reason = f"{type(exc).__name__}: {exc}"
        await self._on_disconnect(reason)

    def _dispatch(self, frame: dict):
        """Route one frame to its pending request, or to the event cache."""
        future = self._pending.get(str(frame.get("message_id")))
        if future is not None:
            if not future.done():
                future.set_result(frame)
            return
        if "event" in frame:
            self._on_event(frame["event"], frame.get("data"))
            if self.on_event is not None:
                self.on_event(frame["event"], frame.get("data"))

    def _on_event(self, event: str, data: Any):
        """Apply one server event to the cache; unknown events are ignored."""
        if event == "attribute_updated":
            node_id, path, value = data
            node = self.nodes.get(node_id)
            if node is not None:
                node["attributes"][path] = value
                if path == LABEL_PATH:
                    self.generation += 1
        elif event in ("node_added", "node_updated"):
            self._store(data)
        elif event == "node_removed":
            if self.nodes.pop(data, None) is not None:
                self.generation += 1

    async def resync_if_due(self):
        """Re-read every node periodically, as a safety net for events.

        A dropped event would otherwise leave `nodes` quietly stale
        forever."""
        if not self._connected:
            return
        if time.monotonic() - self._last_resync < self._resync_interval:
            return
        try:
            self._prime(await self.request("get_nodes"))
        except MatterError as exc:
            log.debug("matter %s resync failed: %s", self.url, exc)

    # ---------- node lookup and access ----------

    def label(self, node_id: int) -> str:
        """The node's NodeLabel, or "" if it has none."""
        node = self.nodes.get(node_id)
        return str(node["attributes"].get(LABEL_PATH) or "") if node else ""

    def read(self, node_id: int, path: str) -> Any:
        """Return a cached attribute value; None if unknown or unavailable."""
        node = self.nodes.get(node_id)
        if node is None or not node["available"]:
            return None
        return node["attributes"].get(path)

    def resolve(self, pattern: int | str, match_case: bool = False) -> int:
        """Return the one node id matching `pattern`, else raise.

        An int is a node id. A string is an fnmatch glob over NodeLabel and
        SerialNumber. Call once per generation and keep the id: this is
        config-time convenience, not something to repeat per read."""
        if isinstance(pattern, int):
            if pattern not in self.nodes:
                raise MatterLookupError(f"no matter node with id {pattern}")
            return pattern
        names = {}
        for node_id, node in self.nodes.items():
            attributes = node["attributes"]
            names[node_id] = [str(attributes[path]) for path in (LABEL_PATH, SERIAL_PATH)
                              if attributes.get(path)]
        matches = match_nodes(pattern, names, match_case)
        if not matches:
            raise MatterLookupError(f"no matter node matches {pattern!r}")
        if len(matches) > 1:
            listed = ", ".join(f"{node_id} ({names[node_id][0]})"
                               for node_id in sorted(matches))
            raise MatterLookupError(f"{pattern!r} matches {len(matches)} matter nodes: {listed}")
        return matches[0]

    async def command(self, node_id: int, endpoint: int, cluster: int,
                      name: str, payload: dict | None = None):
        """Send one cluster command, e.g. On to cluster 6."""
        return await self.request(
            "device_command", node_id=node_id, endpoint_id=endpoint,
            cluster_id=cluster, command_name=name, payload=payload or {})
