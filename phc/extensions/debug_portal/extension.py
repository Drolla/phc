"""debug_portal extension wiring: a web view of the system's internals.

Pushed over Server-Sent Events once per Scheduler tick.

Explicitly enabled, and bound to loopback by default. This module is the
lifecycle shell only: snapshot.py builds the payload, server.py serves it.
"""

import json
import logging

from aiohttp import web

from phc.core.clock import Now
from phc.core.device import Device
from phc.core.selectors import resolve_selectors
from phc.extensions.debug_portal.server import HUB, LAST_SNAPSHOT, SHUTDOWN_EVENT, build_app
from phc.extensions.debug_portal.snapshot import build_snapshot

logger = logging.getLogger("phc.debug_portal")


def configure(params: dict, flat: dict[str, Device], instance_key: str,
              extensions_registry: dict | None = None) -> "DebugPortalInstance":
    """Extension entry point (see phc.core.config._load_extensions).

    Resolves the selectors once against the static device tree, then
    builds the aiohttp app. `extensions_registry` is unused."""
    pairs = resolve_selectors(params["selectors"], flat)
    app = build_app(pairs)
    return DebugPortalInstance(
        app=app, pairs=pairs, host=params["host"], port=int(params["port"]),
        shutdown_timeout=float(params["shutdown_timeout"]), instance_key=instance_key,
    )


class DebugPortalInstance:
    """One configured debug_portal instance, across its four lifecycle hooks.

    configure()/__init__ build the aiohttp app, which is safe with no
    running loop. on_bind() then binds the System, the only source of
    system.tasks/system.heartbeat -- unavailable any earlier, because
    `tasks:` is parsed after `extensions:`. on_start()/on_stop() own the
    AppRunner/TCPSite lifecycle on the Scheduler's own loop, and on_tick()
    broadcasts one snapshot per tick.
    """

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
        """Bind the fully-built System.

        See class docstring for why this is a hook of its own."""
        self._system = system

    def on_tick(self, devices: dict[str, Device]) -> None:
        """Build and broadcast this tick's snapshot.

        Runs after the tick's state is committed, so it shows this
        tick, not the previous one.

        `period` is the wall-clock gap since the previous call -- an overrun
        indicator, so it spans the whole tick period including the heartbeat
        sleep, not just this tick's own processing."""
        now = Now.capture()
        period = (now.wall - self._last_tick_time if self._last_tick_time is not None
                  else self._system.heartbeat)
        self._last_tick_time = now.wall
        self._tick += 1

        snapshot = build_snapshot(self._system, devices, self._pairs,
                                   tick=self._tick, now=now, period=period)
        self._app[LAST_SNAPSHOT].value = snapshot
        self._app[HUB].broadcast(json.dumps(snapshot))

    async def on_start(self, devices: dict[str, Device]) -> None:
        """Start the aiohttp server on the Scheduler's loop."""
        self._runner = web.AppRunner(self._app, shutdown_timeout=self._shutdown_timeout)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("%s listening on http://%s:%d",
                    self._instance_key, self._host, self._port)

    async def on_stop(self, devices: dict[str, Device]) -> None:
        """Stop the aiohttp server."""
        # Wake every open SSE handler immediately (see server.py's
        # handle_events) rather than leaving AppRunner.cleanup() to wait
        # out the full shutdown_timeout per connection.
        self._app[SHUTDOWN_EVENT].set()
        if self._runner is not None:
            await self._runner.cleanup()
        logger.info("%s stopped", self._instance_key)
