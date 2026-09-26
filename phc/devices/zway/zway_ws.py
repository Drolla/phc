"""ZAutomation WebSocket client for a Z-Way controller.

Deliberately free of any PHC imports: this is a plain protocol client,
driven by phc.devices.zway.device for PHC and by demo.py on its own.

The controller encapsulates its REST API in WebSocket frames. A request
carries a `responseEvent` id which the reply echoes back as `type`, so
several requests can be in flight on one socket at once. The same socket
also delivers unsolicited `me.z-wave.devices.level` events carrying a
full device object whenever any device changes state -- that is what
keeps `levels` current between polls.

Two measured behaviours drive the design:

* A bad (or empty) token still completes the WebSocket handshake; the
  controller simply never answers. So a request timeout is the only way
  to detect it, and connecting therefore ends with a bulk GET whose
  failure counts as a failed connect.
* The controller answers 200 OK to commands that make no sense for the
  device (`exact?level=` on a binary switch, `on` to a temperature
  sensor). Nothing is validated server-side, so commands are derived
  from the device's own deviceType here instead."""

import asyncio
import fnmatch
import functools
import itertools
import json
import logging
import time
import weakref
from pathlib import Path
from typing import Any

import aiohttp
import yaml

log = logging.getLogger(__name__)


def _release(connector):
    """Drop a connector's transports without needing a running loop.

    aiohttp's own cleanup schedules work on the loop, which on PHC
    shutdown is already closed; this is the part that can still run."""
    try:
        connector._close()
    except Exception:                                     # noqa: BLE001 - best effort at exit
        pass

API = "/ZAutomation/api/v1"
DEVICES_PATH = f"{API}/devices"
LEVEL_EVENT = "me.z-wave.devices.level"

# Reconnect delays, in seconds; the last one repeats forever.
BACKOFF = (1.0, 10.0, 60.0, 300.0)

# Distinguishes "mapped to None" from "not in the mapping".
_MISSING = object()


class ZWayError(Exception):
    """Any failure talking to the controller."""


class ZWayRequestError(ZWayError):
    """The controller answered, but with a non-2xx status."""

    def __init__(self, status: int, message: str, path: str):
        super().__init__(f"{status} {message} ({path})")
        self.status = status
        self.message = message
        self.path = path


class ZWayLookupError(ZWayError):
    """A device pattern matched no device, or more than one."""


@functools.cache
def type_profiles() -> dict[str, dict]:
    """The zway_types table from module.yaml, keyed by deviceType.

    Read straight from the packaged module.yaml: PHC's own descriptor
    loader only keeps the keys it knows about, and zway_types is ours.

    Located next to this file rather than via importlib.resources(
    __package__): demo.py can import this module either as
    phc.devices.zway.zway_ws or, run as a plain script, as a top-level
    zway_ws with no package at all -- __package__ would be "" in the
    latter case, which importlib.resources rejects."""
    resource = Path(__file__).resolve().parent / "module.yaml"
    raw = yaml.safe_load(resource.read_text(encoding="utf-8")) or {}
    return raw.get("zway_types") or {}


def _as_number(value: Any, want_int: bool) -> int | float | None:
    """Convert a wire level to a number, or None if it isn't one."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if want_int else number


def to_phc(level: Any, device_type: str) -> Any:
    """Translate a wire level into the PHC value for its device type.

    None for anything unreadable, empty or unrecognised -- never a
    stand-in value. In particular "off" must not become True the way
    bool("off") would."""
    if level is None or level == "":
        return None
    profile = type_profiles().get(device_type)
    if profile is None:
        # Unknown device type: pass the level through rather than guess.
        return level

    mapping = profile.get("read") or {}
    if isinstance(level, str):
        mapped = mapping.get(level.strip().casefold(), _MISSING)
        if mapped is not _MISSING:
            return mapped

    value_type = profile.get("type")
    if value_type == "bool":
        if isinstance(level, bool):
            return level
        number = _as_number(level, want_int=False)
        # A dimmer-backed switch reports 0..99 rather than on/off.
        return None if number is None else number != 0
    if value_type in ("int", "float"):
        return _as_number(level, want_int=value_type == "int")
    return level


def command_for(value: Any, device_type: str) -> str:
    """Return the command path fragment that writes `value`.

    Raises ZWayError if the device type takes no writes, or takes none
    of this shape -- the controller answers 200 OK to nonsense, so this
    is the only thing that catches it."""
    profile = type_profiles().get(device_type)
    if profile is None:
        raise ZWayError(f"unknown z-way device type {device_type!r}; refusing to write")
    if not profile.get("writable"):
        raise ZWayError(f"z-way device type {device_type!r} is read-only")

    writes = profile.get("write") or {}
    numeric = profile.get("write_numeric")

    # Accept the wire spelling and 0/1 as well as a real bool, so config and
    # scripts can write whichever is natural.
    if isinstance(value, str):
        folded = value.strip().casefold()
        # A string naming one of write:'s own keys directly (e.g. "clear").
        for key, command in writes.items():
            if isinstance(key, str) and key.casefold() == folded:
                return command
        if folded in ("on", "true", "open"):
            value = True
        elif folded in ("off", "false", "close"):
            value = False
        else:
            number = _as_number(folded, want_int=False)
            if number is None:
                raise ZWayError(f"cannot write {value!r} to a {device_type}")
            value = number
    if isinstance(value, bool):
        command = writes.get(value)
        if command is None:
            raise ZWayError(f"cannot write {value!r} to a {device_type}")
        return command

    number = _as_number(value, want_int=profile.get("type") == "int")
    if number is None:
        raise ZWayError(f"cannot write {value!r} to a {device_type}")
    if numeric is None:
        # No numeric command: fall back to on/off for a purely binary device.
        command = writes.get(bool(number))
        if command is None:
            raise ZWayError(f"cannot write {value!r} to a {device_type}")
        return command
    return numeric.format(value=number)


def match_devices(pattern: str, titles: dict[str, str], match_case: bool = False) -> list[str]:
    """Return the device ids whose id or title matches `pattern`.

    An fnmatch glob, tried against both fields; a hit in either counts.
    Case-insensitive unless `match_case`, because Z-Way titles are
    hand-written and inconsistently capitalised (this is the one place
    that deliberately differs from phc.core.selectors, which is
    case-sensitive)."""
    if match_case:
        return [device_id for device_id, title in titles.items()
                if fnmatch.fnmatchcase(device_id, pattern)
                or fnmatch.fnmatchcase(title, pattern)]
    folded = pattern.casefold()
    return [device_id for device_id, title in titles.items()
            if fnmatch.fnmatchcase(device_id.casefold(), folded)
            or fnmatch.fnmatchcase(title.casefold(), folded)]


class ZWayConnection:
    """One WebSocket to one controller, shared by everything using it.

    Owns every asyncio primitive it needs, so it must only be built with
    a running loop (see device.py's conn_for, which also keys them by
    loop). Not closed anywhere: PHC has no device teardown hook, so the
    socket simply dies with its loop -- force_close keeps that quiet."""

    def __init__(self, url: str, token: str, *, request_timeout: float = 5.0,
                 resync_interval: float = 300.0):
        self.url = url
        self._token = token
        self._request_timeout = request_timeout
        self._resync_interval = resync_interval

        # Latest known state, keyed by device id. Primed by the bulk GET on
        # connect, then kept current by push events.
        self.levels: dict[str, Any] = {}
        self.titles: dict[str, str] = {}
        self.types: dict[str, str] = {}
        # Bumped on every prime. Callers that resolve patterns to device ids
        # watch this to know when to re-resolve, so matching happens once per
        # connection rather than once per access.
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
        return self._connected

    @property
    def last_error(self) -> str | None:
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
        """Open the socket, start the reader, and prime the cache.

        The bulk GET is part of connecting, not a later step: it is both
        what makes `levels` valid from the first poll and what turns a
        bad token (handshake fine, requests unanswered) into a failure."""
        session = aiohttp.ClientSession(
            # No teardown hook exists to close this, so avoid keeping sockets
            # alive past the loop that owns them.
            connector=aiohttp.TCPConnector(force_close=True),
        )
        try:
            ws = await session.ws_connect(
                self.url,
                headers={"Authorization": f"Bearer {self._token}"},
                heartbeat=30,
            )
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
            await self._prime()
        except Exception:
            # Half-open: drop it so the next attempt starts clean rather than
            # reusing a socket whose cache was never filled.
            await self._teardown()
            raise

        self._connected = True
        self._attempt = 0
        self._retry_at = 0.0
        self._last_error = None
        log.info("z-way %s connected, %d device(s)", self.url, len(self.levels))

    async def _prime(self):
        """Load every device's level/title/type, and bump the generation."""
        devices = (await self.request(DEVICES_PATH)).get("devices") or []
        self.levels = {}
        self.titles = {}
        self.types = {}
        for device in devices:
            self._store(device)
        self.generation += 1
        self._last_resync = time.monotonic()

    def _store(self, device: dict):
        """Record one device object from a bulk read or a push event."""
        device_id = device.get("id")
        if not device_id:
            return
        metrics = device.get("metrics") or {}
        # A node the controller can't reach has no trustworthy level.
        self.levels[device_id] = None if metrics.get("isFailed") else metrics.get("level")
        self.titles[device_id] = metrics.get("title") or ""
        self.types[device_id] = device.get("deviceType") or ""

    def _fail(self, error: str):
        """Record a failed connect/disconnect and arm the backoff ladder."""
        delay = BACKOFF[min(self._attempt, len(BACKOFF) - 1)]
        self._attempt += 1
        self._retry_at = time.monotonic() + delay
        self._last_error = error
        log.warning("z-way %s unavailable (%s); retrying in %gs", self.url, error, delay)

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
        self.levels = {}

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
                future.set_exception(ZWayError(f"connection lost: {reason}"))
        self._pending.clear()
        await self._teardown()
        self._fail(reason)

    # ---------- requests ----------

    async def request(self, path: str) -> Any:
        """Send one encapsulated GET and return its decoded `data`.

        Raises ZWayRequestError for a non-2xx reply, ZWayError if the
        socket is gone or the controller never answers."""
        ws = self._ws
        if ws is None:
            raise ZWayError("not connected")

        request_id = f"phc-{next(self._seq)}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await ws.send_str(json.dumps({
                "event": "httpEncapsulatedRequest",
                "responseEvent": request_id,
                "data": {"method": "GET", "url": path},
            }))
            log.debug("z-way %s --> GET %s", self.url, path)
            async with asyncio.timeout(self._request_timeout):
                frame = await future
        except TimeoutError:
            # Also the bad-token symptom: connected, but nothing ever answers.
            raise ZWayError(f"no response within {self._request_timeout}s ({path})") from None
        finally:
            # Every exit path, or a timed-out request leaks its future and the
            # reader later resolves one nobody is waiting on.
            self._pending.pop(request_id, None)

        status = frame.get("status")
        body = frame.get("body")
        if isinstance(body, str):
            # The controller double-encodes: `body` is itself a JSON string.
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                raise ZWayError(f"malformed body for {path}") from None
        body = body or {}
        if status is None or not 200 <= status < 300:
            raise ZWayRequestError(status or 0, body.get("error") or body.get("message") or "", path)
        return body.get("data")

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
                self._dispatch(frame)
            reason = "closed by controller"
        except asyncio.CancelledError:
            raise
        except Exception as exc:                          # noqa: BLE001 - report, never propagate
            reason = f"{type(exc).__name__}: {exc}"
        await self._on_disconnect(reason)

    def _dispatch(self, frame: dict):
        """Route one frame to its pending request, or to the push cache."""
        frame_type = str(frame.get("type") or "")
        future = self._pending.get(frame_type)
        if future is not None:
            if not future.done():
                future.set_result(frame.get("data") or {})
            return
        if frame_type == LEVEL_EVENT:
            self._store(frame.get("data") or {})

    async def resync_if_due(self):
        """Re-read every device periodically, as a safety net for pushes.

        A dropped event or a device type that reports through some other
        event would otherwise leave `levels` quietly stale forever."""
        if not self._connected:
            return
        if time.monotonic() - self._last_resync < self._resync_interval:
            return
        try:
            await self._prime()
        except ZWayError as exc:
            log.debug("z-way %s resync failed: %s", self.url, exc)

    # ---------- device lookup ----------

    def resolve(self, pattern: str, match_case: bool = False) -> str:
        """Return the one device id matching `pattern`, else raise.

        Call once per connection and keep the id: this is config-time
        convenience, not something to repeat per read or write."""
        matches = match_devices(pattern, self.titles, match_case)
        if not matches:
            raise ZWayLookupError(f"no z-way device matches {pattern!r}")
        if len(matches) > 1:
            listed = ", ".join(f"{device_id} ({self.titles[device_id]})"
                               for device_id in sorted(matches))
            raise ZWayLookupError(f"{pattern!r} matches {len(matches)} z-way devices: {listed}")
        return matches[0]

    async def command(self, device_id: str, command: str):
        """Send one device command, e.g. "command/on"."""
        return await self.request(f"{DEVICES_PATH}/{device_id}/{command}")
