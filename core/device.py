"""Device: the base class every device module (devices/<name>/device.py) subclasses."""

import asyncio
from contextvars import ContextVar

from core.endpoint import Endpoint

# When a tick is active the Scheduler installs a write collector here so that
# transmit()s issued via set() during the tick are batched and flushed
# concurrently at the end of the tick, instead of each one blocking inline.
# Outside a tick (default None) set() transmits immediately, so direct use --
# e.g. dev.set("on") before a tick, as tests do -- keeps its current behavior.
_write_collector: ContextVar[list | None] = ContextVar("phc_write_collector", default=None)


class Device:
    """Base class for every device module.

    A device holds zero-or-more Endpoints and zero-or-more child Devices at
    the same time. A pure grouping/"host" device is simply a Device with no
    endpoints of its own -- there is no separate host/leaf class hierarchy.
    """

    def __init__(self, id: str, *, name: str = "", params: dict | None = None,
                 endpoints: list[Endpoint] | None = None,
                 children: list["Device"] | None = None,
                 update_interval: float | None = None,
                 parent_qualified_id: str | None = None):
        self.id = id
        self.name = name or id
        self.qualified_id = f"{parent_qualified_id}.{id}" if parent_qualified_id else id
        self.params = params or {}
        self.endpoints: dict[str, Endpoint] = {e.key: e for e in (endpoints or [])}
        self.children: dict[str, "Device"] = {c.id: c for c in (children or [])}
        self.update_interval = update_interval
        # Negative infinity so a freshly built device is immediately due on
        # the scheduler's first tick, rather than waiting a full interval.
        self._last_run = float("-inf")
        # In-flight guard: True while a blocking receive() is running on a
        # worker thread (default receive_async() bridge). Checked at the top
        # of fetch() -- see fetch()'s docstring for why it lives there rather
        # than inside receive_async() itself.
        self._fetch_thread_running = False

        self.setup()

    def setup(self):
        """Optional one-time init hook (e.g. open a connection). No-op by default."""
        pass

    # ---------- 1. Physical device interface (async; the Scheduler's only path) ----------
    #
    # receive_async()/transmit_async() are what fetch()/the Scheduler actually
    # await. By default they bridge a device's plain blocking receive()/
    # transmit() onto the event loop's executor (the Scheduler points that at
    # its bounded thread pool), so a device author writes only simple
    # synchronous I/O and still gets concurrency for free. A device whose I/O
    # is genuinely async-friendly (e.g. a network sensor over an async HTTP
    # client) overrides receive_async()/transmit_async() directly to await
    # real async I/O and skip the thread entirely -- and, unlike a thread,
    # such a coroutine is truly cancellable on timeout.

    def receive(self) -> dict:
        """Read raw state(s) from hardware -> {endpoint_key: raw_value}.
        Default (sync, blocking) override point; bridged onto a worker thread
        by the default receive_async(). No-op base implementation (returns
        {})."""
        return {}

    def transmit(self, state: dict) -> None:
        """Push a requested state change to hardware; state is
        {endpoint_key: value}. Default (sync, blocking) override point;
        bridged onto a worker thread by the default transmit_async(). No-op
        base implementation (read-only/group devices)."""
        pass

    async def receive_async(self) -> dict:
        """Default: bridge the blocking receive() onto the loop's executor. A
        native-async device overrides this directly instead of receive()."""
        self._fetch_thread_running = True
        return await asyncio.to_thread(self._receive_and_clear)

    def _receive_and_clear(self) -> dict:
        """Worker-thread body for the default receive_async: run receive(),
        then always release the in-flight guard -- regardless of whether the
        awaiting coroutine was itself cancelled/abandoned by a timeout in the
        meantime (a thread can't be force-cancelled, so this is the only place
        it's safe to clear the flag)."""
        try:
            return self.receive()
        finally:
            self._fetch_thread_running = False

    async def transmit_async(self, state: dict) -> None:
        """Default: bridge the blocking transmit() onto the loop's executor.
        No in-flight guard is needed: each tick's write is one-shot and never
        re-issued, so a timed-out write's thread finishing late is
        harmless."""
        await asyncio.to_thread(self.transmit, state)

    def _emit(self, state: dict) -> None:
        """Route a write from set(): if a tick's write collector is active,
        defer the write so the Scheduler can flush all of the tick's writes
        concurrently; otherwise transmit immediately (direct/standalone use)."""
        collector = _write_collector.get()
        if collector is None:
            self.transmit(state)
        else:
            collector.append((self, state))

    # ---------- 2. PHC control interface (driven by the scheduler) ----------

    async def fetch(self) -> None:
        """Stage this device's own raw state via receive_async(), then
        recurse into children. The single always-async orchestration method --
        identical for every device, sync-bridged or native-async.

        In-flight guard: if a previous receive_async() bridge call (this
        device's own blocking receive(), offloaded to a worker thread) is
        still running -- only possible when the Scheduler's fetch_timeout
        abandoned that await, since a thread can't be force-cancelled -- skip
        this WHOLE subtree fetch (no restage, no recursion) until that thread
        clears, so it's never entered a second time and children aren't
        redundantly re-walked either. A native-async device's receive_async()
        override never touches this flag, so the check is always a no-op for
        it (a real coroutine is actually cancelled by fetch_timeout -- no
        orphaned call to guard against)."""
        if self._fetch_thread_running:
            return
        raw = await self.receive_async()
        for key, value in raw.items():
            if key in self.endpoints:
                self.endpoints[key].set(value)
        for child in self.children.values():
            await child.fetch()

    def update_state(self):
        """Commit every endpoint's staged value (see Endpoint.update_state),
        recursing into children."""
        for ep in self.endpoints.values():
            ep.update_state()
        for child in self.children.values():
            child.update_state()

    def due(self, now: float) -> bool:
        """True if this device is unscheduled-eligible now: has an
        update_interval and at least that long has passed since mark_run()."""
        if self.update_interval is None:
            return False
        return (now - self._last_run) >= self.update_interval

    def mark_run(self, now: float):
        """Record `now` as this device's last poll time, for due()."""
        self._last_run = now

    # ---------- 3. User task API ----------

    def _resolve(self, key: str):
        """Return ('endpoint', Endpoint) or ('child', Device) for a given key."""
        if key in self.endpoints:
            return "endpoint", self.endpoints[key]
        if key in self.children:
            return "child", self.children[key]
        raise KeyError(f"{self.qualified_id}: no endpoint or child named {key!r}")

    def get(self, name: str | None = None):
        """No args: whole-device value (scalar shortcut for a single endpoint
        and no children, else a dict). With `name`: that one endpoint's or
        child's value directly."""
        if name is not None:
            _, target = self._resolve(name)
            return target.get()
        if self.endpoints and len(self.endpoints) == 1 and not self.children:
            return next(iter(self.endpoints.values())).get()
        result = {n: ep.get() for n, ep in self.endpoints.items()}
        result.update({cid: child.get() for cid, child in self.children.items()})
        return result

    def set(self, value, name: str | None = None):
        """No `name`: whole-device set (scalar for a single endpoint and no
        children, else a dict routed per-key). With `name`: set that one
        endpoint/child only. Endpoint writes always go through transmit() --
        a value only becomes observable via get() after the next
        fetch()/receive()/update_state() cycle."""
        if name is not None:
            kind, target = self._resolve(name)
            if kind == "endpoint":
                if not target.writable:
                    raise AttributeError(f"{self.qualified_id}: endpoint {name!r} is read-only")
                self._emit({name: value})
            else:
                target.set(value)
            return
        if self.endpoints and len(self.endpoints) == 1 and not self.children:
            ep = next(iter(self.endpoints.values()))
            if not ep.writable:
                raise AttributeError(f"{self.qualified_id}: endpoint {ep.key!r} is read-only")
            self._emit({ep.key: value})
            return
        if not isinstance(value, dict):
            raise TypeError(f"{self.qualified_id}: multi-endpoint/child device requires dict state")
        writes = {}
        for key, val in value.items():
            kind, target = self._resolve(key)
            if kind == "endpoint":
                if not target.writable:
                    raise AttributeError(f"{self.qualified_id}: endpoint {key!r} is read-only")
                writes[key] = val
            else:
                target.set(val)
        if writes:
            self._emit(writes)

    def get_text(self, name: str | None = None):
        """Like get(), but returns each endpoint's formatted display text
        (see Endpoint.to_text) instead of its raw value."""
        if name is not None:
            kind, target = self._resolve(name)
            return target.to_text() if kind == "endpoint" else target.get_text()
        if self.endpoints and len(self.endpoints) == 1 and not self.children:
            return next(iter(self.endpoints.values())).to_text()
        result = {n: ep.to_text() for n, ep in self.endpoints.items()}
        result.update({cid: child.get_text() for cid, child in self.children.items()})
        return result

    def set_text(self, text, name: str | None = None):
        """Like set(), but `text` is formatted display text (see
        Endpoint.from_text) rather than a raw value."""
        if name is not None:
            kind, target = self._resolve(name)
            if kind == "endpoint":
                if not target.writable:
                    raise AttributeError(f"{self.qualified_id}: endpoint {name!r} is read-only")
                self._emit({name: target.from_text(text)})
            else:
                target.set_text(text)
            return
        if self.endpoints and len(self.endpoints) == 1 and not self.children:
            ep = next(iter(self.endpoints.values()))
            if not ep.writable:
                raise AttributeError(f"{self.qualified_id}: endpoint {ep.key!r} is read-only")
            self._emit({ep.key: ep.from_text(text)})
            return
        if not isinstance(text, dict):
            raise TypeError(f"{self.qualified_id}: multi-endpoint/child device requires dict text")
        writes = {}
        for key, val in text.items():
            kind, target = self._resolve(key)
            if kind == "endpoint":
                if not target.writable:
                    raise AttributeError(f"{self.qualified_id}: endpoint {key!r} is read-only")
                writes[key] = target.from_text(val)
            else:
                target.set_text(val)
        if writes:
            self._emit(writes)

    def get_event(self, name: str | None = None):
        """Like get(), but returns each endpoint's change event (see
        Endpoint.get_event) instead of its current value."""
        if name is not None:
            _, target = self._resolve(name)
            return target.get_event()
        if self.endpoints and len(self.endpoints) == 1 and not self.children:
            return next(iter(self.endpoints.values())).get_event()
        result = {n: ep.get_event() for n, ep in self.endpoints.items()}
        result.update({cid: child.get_event() for cid, child in self.children.items()})
        return result

    def get_update_time(self, name: str | None = None):
        """No args: the most recent update_time across all endpoints/children
        (0.0 if none). With `name`: that one endpoint's or child's update_time."""
        if name is not None:
            _, target = self._resolve(name)
            return target.get_update_time()
        times = [ep.get_update_time() for ep in self.endpoints.values()]
        times += [child.get_update_time() for child in self.children.values()]
        return max(times, default=0.0)

    state = property(get, set)
    event = property(get_event)
    update_time = property(get_update_time)

    def endpoint(self, name: str) -> Endpoint:
        """Direct access to the Endpoint object itself, for callers that want
        its full interface (get/set/event/update_time, readable/writable/
        parameters) rather than the device-level get(name)/set(name, value)
        dispatchers."""
        return self.endpoints[name]

    def child(self, id: str) -> "Device":
        """Direct access to a child Device by its (unqualified) id."""
        return self.children[id]
