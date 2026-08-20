"""Scheduler: the fixed-heartbeat loop that ticks devices and tasks."""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Awaitable, Callable

from phc.core.device import Device, _write_collector
from phc.core.task import Task

logger = logging.getLogger("phc.scheduler")
# Device availability gets its own logger so an installation can raise or
# lower the volume of "this sensor stopped answering" independently of
# everything else the scheduler says -- it is the one scheduler-level
# message a user actually wants to see, and to be able to route.
health_logger = logging.getLogger("phc.health")


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
    phc.extensions.web_ui's aiohttp server) that needs to start once before the
    first tick and stop once after the last. See tick_hooks for the
    per-tick equivalent.

    Two clocks, deliberately: every *interval* (a device's update:, a
    task's min_interval, run_forever()'s own heartbeat grid) is measured
    on time.monotonic(), which no NTP correction or DST change can move
    backwards; only a task's absolute due_time (a wall-clock time of day,
    see phc.core.intervals.parse_time) stays on time.time(). Before this
    split, a clock step backwards stalled every poll in the system for the
    size of the step, and a step forwards fired a burst of catch-up polls.
    Both are passed explicitly into _tick_async/tick() rather than read
    from the clock inside, so a caller (notably the test suite) can drive
    ticks at whatever times it likes; tick(now=X) alone applies X to both,
    so a single-clock caller keeps working unchanged.
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
        # One-time async lifecycle hooks (e.g. phc.extensions.web_ui starting/
        # stopping its aiohttp server) -- unlike tick_hooks (once per tick),
        # these run exactly once each, around run_forever()'s tick loop. See
        # _run_async()/_run_hooks() below. Each hook is `async def
        # hook(devices) -> None`.
        self._start_hooks = start_hooks if start_hooks is not None else []
        self._stop_hooks = stop_hooks if stop_hooks is not None else []
        self._running = False

        self._loop: asyncio.AbstractEventLoop | None = None
        self._pool: ThreadPoolExecutor | None = None
        # Set by stop() to break run_forever()'s heartbeat sleep immediately
        # instead of leaving it to time out (see _run_async/stop). Created
        # inside _run_async, on the loop that will await it.
        self._stop_event: asyncio.Event | None = None
        # True while the tick loop is behind its heartbeat grid -- gates the
        # overrun WARNING to the transition into that state, so a system
        # that overruns every single tick logs once, not forever.
        self._overrun_logged = False

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

    def tick(self, now: float | None = None, now_mono: float | None = None):
        """Run one tick synchronously (creating the runtime on first call).
        Only calls _tick_async -- start_hooks/stop_hooks never run on this
        path, only around run_forever()'s own loop (see _run_async()).

        `now` is wall-clock (task due_times), `now_mono` monotonic (device
        intervals, task cooldowns). Passing only `now` applies it to both,
        so a caller driving ticks at explicit times (the test suite) gets
        one coherent timeline out of one argument -- see class docstring."""
        now = now if now is not None else time.time()
        now_mono = now_mono if now_mono is not None else now
        self._ensure_runtime()
        self._loop.run_until_complete(self._tick_async(now, now_mono))

    async def _tick_async(self, now: float, now_mono: float):
        """Run one scheduler tick: fetch due devices, run due tasks, then
        commit state and events for every device. See class docstring for
        the three-pass structure, its concurrency guarantees, and the
        wall-clock (`now`) vs monotonic (`now_mono`) split."""
        # Pass 1: fetch only -- stage raw values concurrently, do not commit or
        # compute change events yet. Every due device is launched directly
        # off the flat index: Device.fetch() stages only its own endpoints
        # and never recurses into children (which are separate entries in
        # this same dict, each due on its own update: interval), so there is
        # no double-fetch to avoid and no subtree bookkeeping to do here. (A
        # device whose previous fetch is still running is skipped inside
        # Device.fetch()'s own in-flight guard, not here.)
        due_ids = {qid for qid, d in self._devices.items() if d.due(now_mono)}
        if due_ids:
            await asyncio.gather(*(self._fetch_one(q) for q in due_ids))

        # Pass 2: log the countdown, then run due tasks against the state/events
        # committed by the PREVIOUS tick's update_state() (see
        # docs/configuration.md#tasks-and-the-heartbeat-a-one-tick-lag).
        # Writes issued by task actions (set() -> transmit()) are collected
        # here and flushed concurrently below, instead of each one blocking
        # inline.
        self._log_task_countdown(now)
        token = _write_collector.set([])
        try:
            # Snapshot the list: a create_task/kill_task action firing this
            # tick mutates self._tasks in place (see phc.core.task.register_task/
            # kill_tasks) -- iterating that same live list would risk "list
            # changed size during iteration" and non-deterministic same-
            # tick-vs-next-tick firing for the newly spawned/removed task.
            for task in list(self._tasks):
                try:
                    fired = task.run(now, self._devices, now_mono)
                    if fired:
                        logger.info("task %s executed", task.tag)
                except Exception:
                    logger.exception("task %s failed", task.tag)
                if task.finished:  # Task never touches its own list -- see Task's class docstring
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
                    device.mark_run(now_mono)

        # Pass 4: run per-tick hooks (e.g. a logger's sticky-value tracking,
        # see phc.extensions.logdb) now that this tick's state is fully
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
        the same gather) and an optional bound (fetch_timeout).

        This is the single place an I/O attempt is seen to succeed or fail,
        so it is also where each device's health is recorded (see
        phc.core.health.DeviceHealth). Swallowing the failure keeps one
        flaky device from stalling the tick, but used to make it invisible
        too -- the health record is what makes it observable instead."""
        try:
            if self.fetch_timeout is not None:
                async with asyncio.timeout(self.fetch_timeout):
                    await awaitable
            else:
                await awaitable
        except TimeoutError:
            self._record_failure(device, kind,
                                  f"{kind} timed out after {self.fetch_timeout:.3f}s")
        except Exception as exc:
            self._record_failure(device, kind, f"{type(exc).__name__}: {exc}", traceback=True)
        else:
            # The I/O did not raise -- but a module that catches its own
            # network errors and reports None values (most of them do) says
            # so here instead. See Device.report_failure.
            reported = device.consume_reported_failure()
            if reported is not None:
                self._record_failure(device, kind, reported)
            elif device.health.record_success():
                health_logger.warning("device %s recovered (%s ok after %d failure(s))",
                                       device.qualified_id, kind, device.health.total_failures)

    def _record_failure(self, device: Device, kind: str, error: str, traceback: bool = False) -> None:
        """Record one failed I/O attempt and log it -- in full the first
        time, then quietly.

        A device that has been unreachable for hours would otherwise repeat
        the same traceback on every tick, burying everything else; but
        dropping the repeats entirely would leave no evidence it is still
        failing. So the transition into failure is a WARNING (with the
        traceback, when there is one), and continued failure stays at DEBUG
        with a running count."""
        first = device.health.record_failure(error)
        if first:
            health_logger.warning("device %s %s failed: %s", device.qualified_id, kind, error)
            if traceback:
                logger.debug("device %s %s failed", device.qualified_id, kind, exc_info=True)
        else:
            health_logger.debug("device %s %s still failing (%d consecutive): %s",
                                 device.qualified_id, kind,
                                 device.health.consecutive_failures, error)

    def _log_task_countdown(self, now: float):
        """At DEBUG level, log an in-place status line of seconds-until-due
        for every task. A task with no due_time schedule (condition-only,
        or neither condition nor time given) shows 0 -- it's due every
        tick, gated only by its condition if any (see Task.run()'s check
        order). A due-time-gated task shows its countdown."""
        if not logger.isEnabledFor(logging.DEBUG):
            return
        parts = []
        for task in self._tasks:
            if task.due_time is None:
                seconds = 0
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
        """Run start_hooks once, then tick on a fixed heartbeat grid until
        stopped, then run stop_hooks once -- all on the SAME loop
        run_forever() drives, so a hook (e.g. phc.extensions.web_ui binding its
        aiohttp server) can freely use asyncio primitives tied to this
        loop. stop_hooks run here, inside this coroutine's own finally --
        NOT in run_forever()'s finally: self.close(), which only runs after
        this coroutine returns and would already have started tearing the
        loop down.

        Ticks are scheduled against a monotonic deadline grid
        (`next_tick += heartbeat`), sleeping only the time REMAINING until
        the next grid point. Sleeping a full heartbeat after each tick
        instead -- as this loop used to -- makes the real period
        `heartbeat + tick duration`, so a system with a 1s heartbeat and a
        200ms tick actually ticks every 1.2s, and every scheduled interval
        in the system silently runs ~17% slow, forever.

        The sleep waits on _stop_event rather than sleeping blindly, so
        stop() takes effect immediately instead of after up to one full
        heartbeat -- which, for the 10s+ heartbeats a quiet installation
        might use, is the difference between a prompt SIGTERM shutdown and
        one that looks hung."""
        self._stop_event = asyncio.Event()
        await self._run_hooks(self._start_hooks, "start")
        try:
            next_tick = time.monotonic()
            while self._running:
                await self._tick_async(time.time(), time.monotonic())
                if self.heartbeat <= 0:
                    continue    # free-running: no grid to keep, no sleep to take
                next_tick += self.heartbeat
                remaining = next_tick - time.monotonic()
                if remaining <= 0:
                    next_tick = self._handle_overrun(next_tick)
                    continue
                self._overrun_logged = False
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=remaining)
                except TimeoutError:
                    pass        # the normal path: slept to the next grid point
        finally:
            await self._run_hooks(self._stop_hooks, "stop")

    def _handle_overrun(self, next_tick: float) -> float:
        """Advance a missed heartbeat deadline to the next grid point that
        is still in the future, and return it. Skipping ahead (rather than
        letting `next_tick` fall further and further behind) means a
        temporary stall -- one slow fetch, a suspended laptop -- costs the
        ticks it actually missed and nothing more; the alternative is a
        catch-up burst of back-to-back ticks racing to work off a backlog
        that no longer corresponds to anything real.

        Logged once per overrun episode, not once per overrunning tick: a
        system whose tick genuinely exceeds its heartbeat would otherwise
        emit this warning on every single tick forever."""
        now_mono = time.monotonic()
        missed = int((now_mono - next_tick) // self.heartbeat) + 1
        if not self._overrun_logged:
            logger.warning(
                "tick overran its %.3fs heartbeat; skipping %d missed tick(s) "
                "(further overruns not logged until it recovers)",
                self.heartbeat, missed)
            self._overrun_logged = True
        return next_tick + missed * self.heartbeat

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
        """Signal run_forever()'s loop to exit after its current tick.

        Safe to call from any thread (a signal handler, a test driving
        run_forever() on a background thread): the flag alone ends the
        loop, and waking _stop_event -- which must happen ON the loop --
        is handed over via call_soon_threadsafe so the pending heartbeat
        sleep is cut short rather than waited out. A loop that is already
        closed/stopping raises RuntimeError there, which is harmless: it
        can only mean the loop is on its way out anyway."""
        self._running = False
        loop, stop_event = self._loop, self._stop_event
        if loop is None or stop_event is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(stop_event.set)
        except RuntimeError:
            pass
