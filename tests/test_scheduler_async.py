"""Concurrency behavior of the Scheduler's device I/O.

These tests exercise the asyncio/executor layer added to the Scheduler: due
devices' blocking fetch()/transmit() run concurrently within a tick, a slow or
failing device is isolated, fetch_timeout bounds the tick, and the in-flight
guard prevents a device slower than a tick from being re-fetched (piling up
threads). They rely only on wall-clock timing of deliberately-slow in-memory
devices, not on real hardware.
"""

import logging
import threading
import time

import pytest

from phc.core.device import Device, _write_collector
from phc.core.endpoint import Endpoint
from phc.core.scheduler import Scheduler
from phc.core.task import SetAction, Task


class SleepReadDevice(Device):
    """Device whose receive() blocks for `delay` seconds, then reports a value."""

    def setup(self):
        self._delay = self.params.get("delay", 0.3)
        self._value = self.params.get("value", 1)

    def receive(self) -> dict:
        time.sleep(self._delay)
        return {"value": self._value}


class SleepWriteDevice(Device):
    """Device whose transmit() blocks for `delay` seconds; receive() is instant."""

    def setup(self):
        self._delay = self.params.get("delay", 0.3)
        self._pending: dict = {}

    def receive(self) -> dict:
        pending, self._pending = self._pending, {}
        return pending

    def transmit(self, state: dict) -> None:
        time.sleep(self._delay)
        self._pending.update(state)


def _read_device(id: str, delay: float, value=1, interval: float = 0.0) -> SleepReadDevice:
    return SleepReadDevice(id, params={"delay": delay, "value": value},
                           endpoints=[Endpoint("value", writable=True)],
                           update_interval=interval)


def test_reads_run_concurrently():
    # Three devices, each 0.3s to read, all due the same tick. Serial would be
    # ~0.9s; concurrent should be ~0.3s.
    devices = {f"r{i}": _read_device(f"r{i}", delay=0.3, value=i) for i in range(3)}
    scheduler = Scheduler(devices)

    start = time.perf_counter()
    scheduler.tick(now=0.0)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.7, f"reads not concurrent: tick took {elapsed:.3f}s"
    for i in range(3):
        assert devices[f"r{i}"].get("value") == i
    scheduler.close()


def test_writes_run_concurrently():
    # Three devices, each 0.3s to write, all written in one tick by tasks.
    # Serial would be ~0.9s; concurrent flush should be ~0.3s.
    devices = {
        f"w{i}": SleepWriteDevice(f"w{i}", params={"delay": 0.3},
                                  endpoints=[Endpoint("state", writable=True)],
                                  update_interval=None)
        for i in range(3)
    }
    tasks = [Task(f"on{i}", due_time=0.0,
                  actions=[SetAction(device_id=f"w{i}", endpoint_key="state", value="on")])
             for i in range(3)]
    scheduler = Scheduler(devices, tasks=tasks)

    start = time.perf_counter()
    scheduler.tick(now=0.0)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.7, f"writes not concurrent: tick took {elapsed:.3f}s"
    # Each device's transmit staged the write into its own buffer.
    for i in range(3):
        assert devices[f"w{i}"]._pending == {"state": "on"}
    scheduler.close()


def test_one_failing_device_does_not_break_the_tick():
    class RaisingDevice(Device):
        def receive(self) -> dict:
            raise RuntimeError("boom")

    good = {f"g{i}": _read_device(f"g{i}", delay=0.05, value=7) for i in range(2)}
    bad = RaisingDevice("bad", endpoints=[Endpoint("value")], update_interval=0.0)
    devices = {**good, "bad": bad}
    scheduler = Scheduler(devices)

    scheduler.tick(now=0.0)  # must not raise

    for i in range(2):
        assert good[f"g{i}"].get("value") == 7
    scheduler.close()


def test_fetch_timeout_bounds_the_tick(caplog):
    slow = _read_device("slow", delay=1.0, value=99)
    fast = _read_device("fast", delay=0.05, value=5)
    scheduler = Scheduler({"slow": slow, "fast": fast}, fetch_timeout=0.2)

    # Attach caplog's handler directly to "phc.scheduler": once any earlier
    # test's load_system() has run configure_logging(), that logger has
    # propagate=False, so caplog's root handler would otherwise miss it (same
    # workaround as test_scheduler.py's task_log fixture).
    sched_logger = logging.getLogger("phc.scheduler")
    sched_logger.addHandler(caplog.handler)
    sched_logger.setLevel(logging.WARNING)
    try:
        start = time.perf_counter()
        scheduler.tick(now=0.0)
        elapsed = time.perf_counter() - start
    finally:
        sched_logger.removeHandler(caplog.handler)

    assert elapsed < 0.8, f"fetch_timeout did not bound the tick: {elapsed:.3f}s"
    assert fast.get("value") == 5          # fast device unaffected
    assert slow.get("value") is None       # slow device abandoned this tick
    assert "timed out" in caplog.text
    scheduler.close()


def test_in_flight_guard_prevents_refetch_of_slow_device():
    # A device slower than a tick (0.3s read) with a short 0.1s fetch_timeout:
    # repeated ticks while its first fetch is still running must NOT launch a
    # second fetch (which would put two threads in its receive()).
    class TrackingSlowDevice(Device):
        def setup(self):
            self._delay = self.params["delay"]
            self.calls = 0
            self.max_concurrent = 0
            self._active = 0
            self._lock = threading.Lock()

        def receive(self) -> dict:
            with self._lock:
                self.calls += 1
                self._active += 1
                self.max_concurrent = max(self.max_concurrent, self._active)
            try:
                time.sleep(self._delay)
                return {"value": 1}
            finally:
                with self._lock:
                    self._active -= 1

    dev = TrackingSlowDevice("slow", params={"delay": 0.3},
                             endpoints=[Endpoint("value")], update_interval=0.0)
    scheduler = Scheduler({"slow": dev}, fetch_timeout=0.1)

    # Three ticks in quick succession, all within the 0.3s the first fetch runs.
    scheduler.tick(now=0.0)
    scheduler.tick(now=0.01)
    scheduler.tick(now=0.02)
    assert dev.calls == 1, f"slow device re-fetched while in flight (calls={dev.calls})"
    assert dev.max_concurrent == 1

    # Once the in-flight fetch has finished, the device is polled again.
    time.sleep(0.4)
    scheduler.tick(now=1.0)
    assert dev.calls == 2
    assert dev.max_concurrent == 1
    scheduler.close()


def test_direct_set_outside_a_tick_transmits_immediately():
    # Regression guard for the write-collector gating: with no active tick,
    # set() must transmit right away, not defer.
    dev = SleepWriteDevice("d", params={"delay": 0.0},
                           endpoints=[Endpoint("state", writable=True)],
                           update_interval=1.0)

    assert _write_collector.get() is None
    dev.set("on")
    # transmit() ran immediately, staging into the buffer that receive() drains.
    assert dev.receive() == {"state": "on"}
