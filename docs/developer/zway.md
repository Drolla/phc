# zway Internals

The module is split in two, along the line "does this need to know what PHC
is?":

- [`zway_ws.py`](../../phc/devices/zway/zway_ws.py) — a plain
  ZAutomation WebSocket client with **no PHC imports at all**. It owns the
  protocol: framing, the request/response correlation, push events, the
  device cache, pattern matching, and the value/command translation tables.
- [`device.py`](../../phc/devices/zway/device.py) — a thin `Device`
  adapter over it. It reads params, binds endpoints to device ids, and
  turns the cache into `{endpoint_key: value}`.

[`demo.py`](../../phc/devices/zway/demo.py) enforces that line: it drives
`zway_ws.py` alone from the command line, importing nothing else from
PHC. If it ever needed a `Device`, a `Scheduler` or the config loader, the
protocol layer would have stopped being the standalone client it is meant
to be.


## The Transport

The controller encapsulates its own REST API in WebSocket frames. A request
carries a `responseEvent` id which the reply echoes back as its `type`, so
several requests can be in flight on one socket at once — `_pending` maps
that id to a future, and `_dispatch` routes each incoming frame to its
waiting request or, failing that, to the push cache.

Two measured controller behaviours shape the design, and both are the
reason for code that otherwise looks over-careful:

- **A bad (or empty) token still completes the handshake.** The controller
  accepts the connection and then never answers anything. A request timeout
  (`request_timeout`, default `5s`) is the only way to detect it — which is
  why `_connect` ends with the bulk device read (`_prime`) and counts its
  failure as a failed connect, rather than connecting optimistically and
  discovering the problem on the first poll.
- **The controller answers `200 OK` to nonsense.** `exact?level=50` on a
  binary switch, `command/on` on a temperature sensor: accepted, and
  nothing happens. Nothing is validated server-side, so `command_for()`
  derives the legal commands from the device's own `deviceType` and raises
  locally before anything reaches the wire.


## State: Push, Prime, Resync

`levels`/`titles`/`types` are the latest known state, keyed by device id.
They are primed by one bulk `GET /ZAutomation/api/v1/devices` on connect,
then kept current by unsolicited `me.z-wave.devices.level` events, each
carrying a full device object. So `receive_async()` reads a cache and
performs no I/O of its own in the common case — a short `update:` interval
costs nothing on the wire, and a value is typically fresh within a tick of
changing rather than within one poll interval.

`resync_if_due()` re-primes every `resync_interval` (default `5m`) as the
safety net: a dropped event, or a device type that reports through some
other event, would otherwise leave `levels` quietly stale forever. An idle
socket being silent is indistinguishable from a broken one at the
application layer, so the periodic re-read is what makes staleness bounded
instead of unbounded.

A device the controller flags `metrics.isFailed` is stored as `None` rather
than with its last level: an unreachable node has no trustworthy value.


## Resolve Once, Address by Id

`generation` is bumped on every prime. `ZWayDevice._bind()` re-resolves
every endpoint's `device:` pattern only when it sees a generation it hasn't
bound against, so globs are matched **once per connection** rather than
once per access — config-time convenience, not a per-poll cost (there is a
test that pins the call count at exactly one across several reads and
writes). Everything after that addresses the controller by device id.

That also defines when a rename on the controller takes effect: on the next
reconnect or resync, not on the next read. One pattern that matches nothing
(or several things) costs only its own endpoint — it is reported as a
device failure and the sibling endpoints keep working, rather than failing
the whole device or the shared connection.

`match_devices()` is deliberately **case-insensitive** by default, which is
the one place in PHC that differs from
[`phc/core/selectors.py`](../../phc/core/selectors.py). Z-Way titles are
hand-written on the controller and inconsistently capitalised, so a
case-sensitive default would mostly produce "no such device" for a name the
user can plainly see. `match_case: true` restores the strict behaviour per
endpoint.


## Translation Tables in `module.yaml`

`zway_types:` in [`module.yaml`](../../phc/devices/zway/module.yaml) maps
each `deviceType` the controller reports to its PHC value type, its
wire-level → PHC-value `read:` mapping, and the `write:`/`write_numeric:`
command templates that write it. `type_profiles()` reads that key straight
out of the packaged YAML rather than through PHC's descriptor loader, which
only keeps the keys it knows about — `zway_types` is the module's own.

The point is that adding support for a new Z-Way device type is a change to
that table and nothing else. `to_phc()` returns `None` for anything
unreadable, empty or unrecognised rather than a stand-in value, and passes
an entirely unknown `deviceType` through unchanged rather than guessing;
`command_for()` refuses to write to one.


## Connection Lifetime and the Backoff Ladder

`_ZWayState` (in `self.context`, so per-`System` rather than per-process —
see [Sharing state between a module's
devices](writing-a-device-module.md#sharing-state-between-a-modules-devices))
keys connections by `(url, token, id(loop))`. The loop is part of the key
*and* compared by identity, because a connection's asyncio primitives
belong to the loop they were built on and each `Scheduler` runs its own;
`id()` alone can be reused once a loop is collected.

`ensure_started()` returns `False` without attempting anything while
`_retry_at` has not elapsed. That gate is what makes the ladder a ladder
rather than a reconnect attempt on every single poll: `BACKOFF` is
`1s, 10s, 60s, 300s`, with the last repeating forever. While down, every
endpoint reads `None` and the device reports unhealthy.

PHC has no device teardown hook — on shutdown the `Scheduler` simply closes
its loop — so a connection is never explicitly closed in normal operation.
`force_close=True` on the connector plus a `weakref.finalize` that drops
the connector's transports (which needs no running loop) is what keeps
aiohttp from complaining out of `__del__` about a session whose loop is
already gone. `close()` exists for the things that *do* have a teardown
point: `demo.py` and the tests.


## References

- [Z-Way manual](https://z-wave.me/manual/z-way)
- [JavaScript Engine and API](https://z-wave.me/manual/z-way/JavaScript_Engine.html) —
  background for [`phc_zWay.js`](../../phc/devices/zway/phc_zWay.js) and the
  tag-reader setup script in [`docs/zway.md`](../zway.md#tag-readers).
- [Topics for Developers](https://z-wave.me/manual/z-way/Special_topics_Developers.html)
