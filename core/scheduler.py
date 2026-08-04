"""Scheduler: the fixed-heartbeat loop that ticks devices and tasks."""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Awaitable, Callable

from core.device import Device, _write_collector
from core.task import Task

logger = logging.getLogger("phc.scheduler")


def _ancestor_ids(qualified_id: str) -> list[str]:
    """Proper ancestor qualified ids of a device, e.g. "a.b.c" -> ["a", "a.b"].
    Qualified ids are ancestor ids joined with ".", so prefixes are ancestors."""
    parts = qualified_id.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts))]


class Scheduler:
    """Drives each device's fetch()/update_state() on its own interval, then
    evaluates tasks against the resulting state.

    A fixed-heartbeat loop, but the hardware I/O within a tick runs
    concurrently: the loop awaits every due device's fetch() (and every write
    transmit_async()) together. fetch() is always async; by default it awaits
    receive_async(), which bridges the device's blocking receive() onto a
    bounded thread pool, so one slow/flaky device no longer stretches the
    whole tick -- total tick I/O time is bounded by the slowest device, not the
    sum of all. A device may override receive_async()/transmit_async() to do
    native async I/O, awaited directly without a thread.

    The tick's structure is unchanged and stays deterministic: fetch (pass 1)
    -> tasks (pass 2) -> commit/events (pass 3) -> tick hooks (pass 4). Only
    the I/O inside passes 1 and 2 is parallelized; the task pass and the
    whole commit pass run single-threaded on the loop, so the
    Device/Endpoint public API and the one-tick event-lag semantics are
    preserved.

    start_hooks/stop_hooks are a separate, one-time (not per-tick) lifecycle:
    each runs exactly once, around run_forever()'s tick loop -- for a
    long-lived resource tied to the scheduler's own loop (e.g.
    extensions.web_ui's aiohttp server) that needs to start once before the
    first tick and stop once after the last. See tick_hooks for the
    per-tick equivalent.
    """

    def __init__(self, devices: dict[str, Device], tasks: list[Task] | None = None,
                 heartbeat: float = 1.0, max_workers: int | None = None,
                 fetch_timeout: float | None = None,
                 tick_hooks: list[Callable[[dict[str, Device]], None]] | None = None,
                 start_hooks: list[Callable[[dict[str, Device]], Awaitable[None]]] | None = None,
                 stop_hooks: list[Callable[[dict[str, Device]], Awaitable[None]]] | None = None):
        self._devices = devices
        self._tasks = tasks if tasks is not None else []
        self.heartbeat = heartbeat
        self.fetch_timeout = fetch_timeout
        self._max_workers = max_workers
        self._tick_hooks = tick_hooks if tick_hooks is not None else []
        # One-time async lifecycle hooks (e.g. extensions.web_ui starting/
        # stopping its aiohttp server) -- unlike tick_hooks (once per tick),
        # these run exactly once each, around run_forever()'s tick loop. See
        # _run_async()/_run_hooks() below. Each hook is `async def
        # hook(devices) -> None`.
        self._start_hooks = start_hooks if start_hooks is not None else []
        self._stop_hooks = stop_hooks if stop_hooks is not None else []
        self._running = False

        self._loop: asyncio.AbstractEventLoop | None = None
        self._pool: ThreadPoolExecutor | None = None

    # ---------- runtime (event loop + thread pool) lifecycle ----------

    def _ensure_runtime(self):
        """Lazily create the event loop and thread pool on first use."""
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        if self._pool is None:
            workers = self._max_workers
            if workers is None:
                # Serves both concurrent reads (scheduled devices) and concurrent
                # writes (any device a task writes to), so size off the total
                # device count, bounded so a large system can't explode threads.
                workers = max(1, min(32, len(self._devices)))
            self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="phc-io")
            # Device.receive_async/transmit_async bridge blocking I/O via
            # asyncio.to_thread, which uses the loop's default executor -- point
            # that at our bounded, configured pool so max_workers applies.
            self._loop.set_default_executor(self._pool)

    def close(self):
        """Release the thread pool and event loop. In-flight blocking fetches
        are not waited on (a thread can't be force-cancelled); they finish and
        are discarded."""
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None
        if self._loop is not None and not self._loop.is_closed():
            self._loop.close()
            self._loop = None

    # ---------- tick ----------

    def tick(self, now: float | None = None):
        """Run one tick synchronously (creating the runtime on first call).
        Only calls _tick_async -- start_hooks/stop_hooks never run on this
        path, only around run_forever()'s own loop (see _run_async())."""
        now = now if now is not None else time.time()
        self._ensure_runtime()
        self._loop.run_until_complete(self._tick_async(now))

    async def _tick_async(self, now: float):
        """Run one scheduler tick: fetch due devices, run due tasks, then
        commit state and events for every device. See class docstring for
        the three-pass structure and its concurrency guarantees."""
        # Pass 1: fetch only -- stage raw values concurrently, do not commit or
        # compute change events yet.
        due_ids = {qid for qid, d in self._devices.items() if d.due(now)}
        # fetch() recurses into children, so fetching both a device and a due
        # descendant would double-fetch it (a race when concurrent). Launch
        # only maximal due subtrees; each root's recursion covers its due
        # descendants. (A device whose previous fetch is still running is
        # skipped inside Device.fetch()'s own in-flight guard, not here.)
        roots = [q for q in due_ids if not any(a in due_ids for a in _ancestor_ids(q))]
        if roots:
            await asyncio.gather(*(self._fetch_one(q) for q in roots))

        # Pass 2: log the countdown, then run due tasks against the state/events
        # committed by the PREVIOUS tick's update_state(). Writes issued by task
        # actions (set() -> transmit()) are collected here and flushed
        # concurrently below, instead of each one blocking inline.
        self._log_task_countdown(now)
        token = _write_collector.set([])
        try:
            # Snapshot the list: a create_task/kill_task action firing this
            # tick mutates self._tasks in place (see core.task.register_task/
            # kill_tasks) -- iterating that same live list would risk
            # "list changed size during iteration" and non-deterministic
            # same-tick-vs-next-tick firing for the newly spawned/removed
            # task. A once-per-tick snapshot means such mutations only take
            # effect starting the next tick, consistent with tasks only ever
            # observing state committed by the PREVIOUS tick (see Task's
            # class docstring).
            spent = []
            for task in list(self._tasks):
                if task.due(now):
                    try:
                        fired = task.run(now, self._devices)
                        if fired:
                            logger.info("task %s executed", task.tag)
                    except Exception:
                        logger.exception("task %s failed", task.tag)
                    finally:
                        task.mark_run(now)
                    if task.spent:
                        spent.append(task)
            # Drop tasks that just fired their one and only run (time-driven,
            # repeat<=0) -- mark_run() already parked them at due_time=inf, so
            # this is cleanup, not scheduling; removed after the loop (not
            # in-place during it) for the same reason the loop iterates a
            # snapshot: a same-tick create_task/kill_task action may also be
            # mutating self._tasks.
            for task in spent:
                if task in self._tasks:
                    self._tasks.remove(task)
                    logger.info("task %s removed (one-shot, fired)", task.tag)
        finally:
            writes = _write_collector.get()
            _write_collector.reset(token)
        await self._flush_writes(writes)

        # Pass 3: commit + compute this tick's change events for EVERY device
        # (a device's _next_state may have been staged by a task's write, not
        # just its own fetch). mark_run() (the poll-interval bookkeeping) is
        # gated on due_ids, as before. A device whose fetch() skipped (guard
        # still in flight) keeps last-good state and is retried on a later due
        # tick; marking it run here is harmless.
        for qualified_id, device in self._devices.items():
            try:
                device.update_state()
            except Exception:
                logger.exception("device %s update_state failed", device.qualified_id)
            finally:
                if qualified_id in due_ids:
                    device.mark_run(now)

        # Pass 4: run per-tick hooks (e.g. a logger's sticky-value tracking,
        # see extensions.logdb) now that this tick's state is fully
        # committed -- unlike tasks (pass 2), a hook always observes THIS
        # tick's freshest state, not the previous tick's.
        for hook in self._tick_hooks:
            try:
                hook(self._devices)
            except Exception:
                logger.exception("tick hook %r failed", hook)

    # ---------- concurrent I/O helpers ----------

    async def _fetch_one(self, qualified_id: str):
        """Fetch one device (isolated/timed by _await_io)."""
        device = self._devices[qualified_id]
        await self._await_io(device.fetch(), device, "fetch")

    async def _flush_writes(self, writes: list[tuple[Device, dict]]):
        """Merge and concurrently transmit this tick's collected task writes."""
        if not writes:
            return
        # Merge multiple writes to the same device into one transmit (later
        # value wins), preserving first-seen order, then transmit concurrently.
        merged: dict[Device, dict] = {}
        for device, state in writes:
            merged.setdefault(device, {}).update(state)
        await asyncio.gather(*(self._transmit_one(d, s) for d, s in merged.items()))

    async def _transmit_one(self, device: Device, state: dict):
        """Transmit one device's merged write (isolated/timed by _await_io)."""
        await self._await_io(device.transmit_async(state), device, "transmit")

    async def _await_io(self, awaitable, device: Device, kind: str):
        """Await one device I/O with per-device isolation (a failure/timeout is
        logged and never propagates, so it can't cancel other devices' I/O in
        the same gather) and an optional bound (fetch_timeout)."""
        try:
            if self.fetch_timeout is not None:
                async with asyncio.timeout(self.fetch_timeout):
                    await awaitable
            else:
                await awaitable
        except TimeoutError:
            logger.warning("device %s %s timed out after %.3fs",
                           device.qualified_id, kind, self.fetch_timeout)
        except Exception:
            logger.exception("device %s %s failed", device.qualified_id, kind)

    def _log_task_countdown(self, now: float):
        """At DEBUG level, log an in-place status line of seconds-until-due
        for every task."""
        if not logger.isEnabledFor(logging.DEBUG):
            return
        parts = []
        for task in self._tasks:
            if task.condition is not None:
                seconds = 0
            elif task.due_time == float("inf"):
                seconds = -1
            else:
                seconds = max(0, int(task.due_time - now))
            parts.append(f"{seconds}:{task.tag}")
        logger.debug(" ".join(parts), extra={"in_place": True})

    # ---------- run loop ----------

    def run_forever(self):
        """Run ticks on the heartbeat interval until stop() is called, then
        release the runtime."""
        self._ensure_runtime()
        self._running = True
        try:
            self._loop.run_until_complete(self._run_async())
        finally:
            self.close()

    async def _run_async(self):
        """Run start_hooks once, then tick/sleep one heartbeat/repeat until
        self._running is False, then run stop_hooks once -- all on the SAME
        loop run_forever() drives, so a hook (e.g. extensions.web_ui binding
        its aiohttp server) can freely use asyncio primitives tied to this
        loop. stop_hooks run here, inside this coroutine's own finally --
        NOT in run_forever()'s finally: self.close(), which only runs after
        this coroutine returns and would already have started tearing the
        loop down."""
        await self._run_hooks(self._start_hooks, "start")
        try:
            while self._running:
                await self._tick_async(time.time())
                await asyncio.sleep(self.heartbeat)
        finally:
            await self._run_hooks(self._stop_hooks, "stop")

    async def _run_hooks(self, hooks: list, label: str) -> None:
        """Run every hook in `hooks` once, concurrently, each isolated so one
        hook's failure/exception is logged and never blocks the others or
        propagates -- mirrors the tick_hooks pass's per-hook isolation
        (_tick_async, pass 4)."""
        if hooks:
            await asyncio.gather(*(self._run_one_hook(hook, label) for hook in hooks))

    async def _run_one_hook(self, hook: Callable[[dict[str, Device]], Awaitable[None]],
                             label: str) -> None:
        try:
            await hook(self._devices)
        except Exception:
            logger.exception("%s hook %r failed", label, hook)

    def stop(self):
        """Signal run_forever()'s loop to exit after its current tick/sleep."""
        self._running = False
