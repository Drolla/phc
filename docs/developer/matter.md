# matter Internals

User documentation, including the server setup, is in
[`docs/matter.md`](../matter.md). This page covers how the module works.

The module is split in two, along the same line as the
[Z-Way module](zway.md): "does this need to know what PHC is?"

- [`matter_ws.py`](../../phc/devices/matter/matter_ws.py) — a plain
  matterjs-server WebSocket client with **no PHC imports at all**. It owns
  the protocol: framing, request/reply correlation, push events, the node
  cache, node matching, and the value/command translation.
- [`device.py`](../../phc/devices/matter/device.py) — a thin `Device`
  adapter over it. It reads params, binds endpoints to node ids, and turns
  the cache into `{endpoint_key: value}`.

[`demo.py`](../../phc/devices/matter/demo.py) enforces that line: it
drives `matter_ws.py` alone and imports nothing else from PHC.


## The Transport

Requests are `{message_id, command, args}`; the reply echoes `message_id`
and carries `result`, or `error_code` + `details` (raised as
`MatterRequestError`). `_pending` maps the id to a future and `_dispatch`
routes each incoming frame to its waiting request or, failing that, to the
event handler.

The connect sequence is part of the protocol, not an implementation choice:

1. The server sends one unsolicited **server-info** frame (kept as
   `server_info`; it says e.g. whether Bluetooth is enabled).
2. Nothing else arrives until the client sends `start_listening`. Its
   result is the full node list and, from then on, `{event, data}` frames.
   It may be sent **once per connection**, so a later re-read uses
   `get_nodes` instead.

`_connect` ends with `start_listening` and counts its failure as a failed
connect. A server that accepts the socket but never answers therefore
fails by `request_timeout` rather than leaving the module connected to
nothing.


## State: Push, Prime, Resync

`nodes` maps a node id to `{available, attributes}`, where attributes are
keyed by `"endpoint/cluster/attribute"` strings exactly as the server
reports them. Values are cached untouched; translation happens on read.

Struct values (e.g. Electrical Energy `145/1`) arrive as dicts keyed by
the **numeric field tag as a string** — `{"0": 64000, "1": 2, "2": 2}` —
never by field name. That is why `field:` in a profile is a tag number.

Events keep the cache current, so `receive_async()` performs no I/O in the
common case:

| Event | Effect |
|---|---|
| `attribute_updated` `[node_id, path, value]` | one value replaced |
| `node_added` / `node_updated` | whole node replaced (carries `available`) |
| `node_removed` `node_id` | node dropped |

`resync_if_due()` re-reads `get_nodes` every `resync_interval` (default
`5m`) as the safety net for a dropped event. An unavailable node reads
`None` rather than its last value.

`on_event`, if set, is called after the cache has applied an event;
`demo.py listen` uses it.


## Resolve Once, Address by Id

`generation` is bumped on every prime, whenever a node appears or
disappears, and whenever a node's label changes (`0/40/5`).
`MatterDevice._bind()` re-resolves every endpoint's `node:` only when it
sees a generation it hasn't bound against, so globs are matched **once
per generation** rather than once per access (a test pins the call count).
Everything after that addresses the server by node id.

`node:` is an int (a node id) or a glob over the node's label and serial
number, case-insensitive unless `match_case`. Labels are hand-written, the
same reason Z-Way's matching differs from PHC's case-sensitive
[selectors](../../phc/core/selectors.py). One pattern that matches
nothing, or several nodes, costs only its own endpoint: it is reported as
a device failure and the siblings keep working.

The device-level `node` parameter and the endpoint-level `node` field are
two separate declarations in `module.yaml` (`parameters:` and
`endpoint_parameters:`); the loader checks them against separate reserved
names, so one name serves both. The endpoint's own value wins.


## Translation Lives in the Profiles

Unlike Z-Way, whose device types live in a table `device.py` reads, the
protocol facts here are ordinary endpoint fields declared in
`endpoint_profiles:` of [`module.yaml`](../../phc/devices/matter/module.yaml):

| Field | Meaning |
|---|---|
| `attribute` | `"cluster/attribute"`; the endpoint number is `endpoint:` (default 1) |
| `field` | struct member tag to read |
| `mask` | read a bitmap as "any of these bits set" |
| `scale` | wire unit × scale = endpoint unit |
| `commands` | `{true: On, false: Off}`: cluster command a bool write sends |
| `level_command` | cluster command a number write sends, as `level = value / scale` |

So a new attribute is a YAML change, and a user can declare one on a single
endpoint without a profile. `to_phc()` and `write_for()` are pure
functions of the raw value and these fields, which is also what makes them
cheap to test. `to_phc()` returns `None` for anything unreadable rather
than a stand-in value, and rounds when the endpoint is an `int`. A write
to an endpoint declaring neither `commands` nor `level_command` raises
locally, before anything reaches the wire.

Writes are cluster **commands** (`device_command`), not attribute writes:
the state change is then reported back by the node through the normal
`attribute_updated` event, so a write is never assumed to have worked.

The cluster for a command is taken from `attribute` (`6/0` → cluster 6).
That holds for On/Off and Level Control; a profile whose command belongs
to a different cluster than its attribute would need an explicit field.


## Connection Lifetime and the Backoff Ladder

This part is copied from the Z-Way client on purpose; see
[Connection Lifetime](zway.md#connection-lifetime-and-the-backoff-ladder)
there for the reasoning. In short: `_MatterState` (in `self.context`)
keys connections by `(url, id(loop))`, `ensure_started()` returns `False`
while the backoff (`1s, 10s, 60s, 300s`, last repeating) has not elapsed,
and `force_close` plus a `weakref.finalize` stand in for the teardown hook
PHC doesn't have. While the server is down, every endpoint reads `None`
and the device reports unhealthy.

The two clients share about 150 lines of this machinery. Factoring a
common base class out of `zway_ws.py` and `matter_ws.py` is a candidate
once a third WebSocket protocol appears.


## Tests

[`tests/test_matter.py`](../../tests/test_matter.py) runs the real client
against a fake in-process server that reproduces the connect sequence,
`get_nodes`/`device_command` replies, error replies and pushed events.
Node 3 in its fixture is a capture of a real LEDVANCE plug.


## References

- [matterjs-server](https://github.com/matter-js/matterjs-server) —
  `python_client/matter_server/client/client.py` is the readable
  reference for command and event names, and `python_client/.../models.py`
  for the `APICommand` enum.
- [Matter specification, application clusters](https://csa-iot.org/developer-resource/specifications-download-request/)
  for the cluster and attribute numbers used by the profiles.
