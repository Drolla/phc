"""debug_portal extension wiring: an explicitly-enabled, loopback-default
web view of the running system's internals -- the scheduler's task queue,
the device poll queue, and every selected endpoint's live state/event/
last_valid_state -- pushed once per Scheduler tick over Server-Sent
Events. See core.config._load_extensions for configure()'s call site,
core.config.load_system for how on_bind is invoked once the System
(including its tasks:) is fully built, and core.scheduler.Scheduler for
how on_tick/on_start/on_stop get invoked (tick_hooks/start_hooks/
stop_hooks)."""

import json
import logging
import time

from aiohttp import web

from core.device import Device
from core.selectors import resolve_selectors
from extensions.debug_portal.server import HUB, LAST_SNAPSHOT, SHUTDOWN_EVENT, build_app
from extensions.debug_portal.snapshot import build_snapshot

logger = logging.getLogger("phc.debug_portal")


def configure(params: dict, flat: dict[str, Device], instance_key: str,
              extensions_registry: dict | None = None) -> "DebugPortalInstance":
    pairs = resolve_selectors(params["selectors"], flat)
    app = build_app(pairs)
    return DebugPortalInstance(
        app=app, pairs=pairs, host=params["host"], port=int(params["port"]),
        shutdown_timeout=float(params["shutdown_timeout"]), instance_key=instance_key,
    )


class DebugPortalInstance:
    """One configured debug_portal instance. build_app() (safe with no
    running loop) happens in configure()/__init__; on_bind() then binds
    the fully-built System, needed for system.tasks/system.heartbeat,
    neither available at configure()-time (tasks: is parsed after
    extensions: -- see core.config.load_system); on_start()/on_stop() own
    the actual AppRunner/TCPSite lifecycle, invoked by
    core.scheduler.Scheduler on its own loop (start_hooks/stop_hooks); and
    on_tick() builds and broadcasts one snapshot per tick (tick_hooks)."""

    def __init__(self, app: web.Application, pairs: list[tuple[str, str]], host: str, port: int,
                 shutdown_timeout: float, instance_key: str):
        self._app = app
        self._pairs = pairs
        self._host = host
        self._port = port
        self._shutdown_timeout = shutdown_timeout
        self._instance_key = instance_key
        self._system = None
        self._tick = 0
        self._last_tick_time: float | None = None
        self._runner: web.AppRunner | None = None

    def on_bind(self, system) -> None:
        """Store the fully-built core.config.System -- see class
        docstring for why this can't happen in configure()/__init__."""
        self._system = system

    def on_tick(self, devices: dict[str, Device]) -> None:
        """Build this tick's snapshot and broadcast it to every connected
        SSE client -- auto-registered as a Scheduler tick hook (see
        core.config.load_system), called once per tick after that tick's
        state is fully committed. `period` is the measured wall-clock gap
        since the previous call (an overrun indicator: the actual tick
        period including the heartbeat sleep, not just this tick's own
        processing time)."""
        now = time.time()
        period = (now - self._last_tick_time) if self._last_tick_time is not None \
            else self._system.heartbeat
        self._last_tick_time = now
        self._tick += 1

        snapshot = build_snapshot(self._system, devices, self._pairs,
                                   tick=self._tick, now=now, period=period)
        self._app[LAST_SNAPSHOT].value = snapshot
        self._app[HUB].broadcast(json.dumps(snapshot))

    async def on_start(self, devices: dict[str, Device]) -> None:
        self._runner = web.AppRunner(self._app, shutdown_timeout=self._shutdown_timeout)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("%s listening on http://%s:%d",
                    self._instance_key, self._host, self._port)

    async def on_stop(self, devices: dict[str, Device]) -> None:
        # Wake every open SSE handler immediately (see server.py's
        # handle_events) rather than leaving AppRunner.cleanup() to wait
        # out the full shutdown_timeout per connection.
        self._app[SHUTDOWN_EVENT].set()
        if self._runner is not None:
            await self._runner.cleanup()
        logger.info("%s stopped", self._instance_key)
