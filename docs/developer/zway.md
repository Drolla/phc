# zway internals

[`devices/zway/device.py`](../../devices/zway/device.py) batches every
`zway` device behind the same controller (`base_url`) into shared caching/
auth state, keyed by `base_url`:

- **Identifier registry** (`_identifiers`) — every device registers its
  readable endpoints' `(command_group, address)` identifiers into a
  shared, module-level dict during `setup()`, before the Scheduler starts
  (so the registry is guaranteed complete by the first fetch). Whichever
  device is due first each poll window issues one combined `Get()` request
  covering every currently-registered identifier for that controller.
- **Response cache** (`_response_cache`, `_response_cache_lock`) — the
  combined fetch is cached for `cache_time` and reused by every sibling
  device polling within that window, using double-checked locking to avoid
  a cache stampede when several devices become due at once. A failed fetch
  is never cached, so callers retry on the next poll.
- **Session/auth** (`_session_cookies`, `_session_lock`) — only the
  extracted session cookie string is cached, not a long-lived
  `aiohttp.ClientSession`; a fresh session is opened per request to avoid
  lifecycle issues across `Scheduler` instances (e.g. in tests), while the
  cached cookie still survives a session's own cookie-jar reset. A 401/403
  response drops the cached cookie and retries once with a fresh login.
- **`TagReader` one-time setup** (`_configured_tag_readers`) — a device
  with a `TagReader` endpoint needs one `Configure_TagReader(node)` call
  before its readings are meaningful; deferred out of `setup()` (which is
  sync/no-I/O) into the first `receive_async()`, and tracked per
  `(base_url, node)` pair so it only runs once. Only recorded on success,
  so a transient failure retries on the next poll.

## Profile library notes

A few `device_profiles` entries in
[`module.yaml`](../../devices/zway/module.yaml) encode wiring that isn't
obvious from the endpoint list alone:

- `everspring-siren_300_series`'s `battery` endpoint addresses `"{node}.0"`
  (not the bare `"{node}"` most other profiles use).
- `benext-tag_reader`'s `ack` endpoint is a separate `switch_binary`
  endpoint for ack LED/lock feedback — kept apart from `state` because
  `TagReader` and `SwitchBinary` are different command groups.
- `everspring-pir_sensor` reports an inverted motion signal in hardware
  (not yet modeled/corrected here).
- `rm80-radiation_monitor` has no `brand`/`product` metadata available.
