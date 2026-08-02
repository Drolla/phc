# Concepts

Pylon Home Control (PHC) polls and controls a tree of pluggable **devices**
(weather stations, sun position, virtual/test devices, and anything you
add), and runs **tasks** — condition- or time-driven automations — against
their state, all on a fixed-heartbeat scheduler.

- **Device** — a node in a tree that exposes zero or more **endpoints**
  (readable/writable state) and may have child devices. Devices are backed
  by a plugin **module** (e.g. `meteoswiss`, `open_meteo`, `waveplus_bridge`,
  `zway`, `sun`, `virtual`, `host`), declared in a system YAML file.
- **Module** — a device plugin: a `devices/<name>/device.py` (the `Device`
  subclass) plus a `devices/<name>/module.yaml` describing its parameters
  and endpoints declaratively. Modules are discovered automatically at
  startup.
- **Task** — an automation triggered either by a schedule (`time`/`repeat`)
  or by a device endpoint changing (`condition`), performing one or more
  **actions** (`set`, `toggle`, `log`, `create_task`, `kill_task`, `script`,
  ...), with an optional `min_interval` retrigger cooldown.
- **Scheduler** — drives each device's fetch on its own interval and
  evaluates tasks once per heartbeat tick, running device I/O concurrently.

See [`examples/`](../examples/) for complete system configurations, and the
`module.yaml` file in each [`devices/`](../devices/) subfolder for what
parameters/endpoints a given device module supports.

[`extensions/`](../extensions/) is the home for non-device PHC extensions
(e.g. [`extensions/logdb/`](../extensions/logdb/), a CSV-backed sample store,
and [`extensions/random_light/`](../extensions/random_light/), randomized
light control), following the same package-plus-descriptor pattern as
device modules.

## Endpoint types, units & text

Unless otherwise specified, an endpoint's value is untyped and passes
through unchanged. An endpoint definition may opt into:

- `type` — `int`, `float`, `bool`, or `str`.
- `unit` — a display unit, e.g. `"°C"`, appended when formatting a
  numeric value as text.
- `values` — a raw value → text label mapping, e.g. `{ 0: "off", 1: "on" }`.
- `min`/`max` — a numeric range hint, stored only (never enforced against
  a write) — e.g. used by [`extensions/web_ui/`](../extensions/web_ui/) to
  decide whether a writable numeric endpoint gets a bounded slider.
- `format` — a Python format-spec string (e.g. `".2f"`) applied by
  `to_text()`. Defaults to `".1f"` for a `float` endpoint, since `str()` on
  a raw float otherwise shows however many digits happen to round-trip
  (e.g. `3.140000000000001`); set `format: ""` to opt back into full,
  unrounded precision. Other `type`s default to no formatting.
- `name` — an optional per-instance display label (e.g. `"Corridor Light"`),
  distinct from `description` (free-form documentation text). A UI prefers
  `name` over `description` over the endpoint's own `key` when picking a
  label. Typically left unset on a module's own endpoints/profiles (which
  don't know what a specific installation will call the thing) and set at
  the system-config level instead — see [Endpoint and device
  profiles](profiles.md).
- `read_transform`/`write_transform` — restricted-Python expressions (the
  same sandbox as a task's `expr:`, see [Conditions, scripted actions &
  sticky values](scripting.md)) that correct a raw value on the way in or
  out, e.g. a sensor's calibration offset or an inverted polarity.
  `read_transform` runs on a value just read from hardware, before it
  becomes the endpoint's stored state (`value` is the raw reading, e.g.
  `read_transform: "value - 1.5"`); `write_transform` runs on a value about
  to be written to hardware, after `set_text()`'s `from_text()` conversion
  (`value` is the logical value being written, e.g.
  `write_transform: "value + 1.5"`). Both default to unset (pass through
  unchanged); a round-trip endpoint (read and written) needs both declared
  consistently — they aren't derived from one another.
- `history` — keeps a short in-memory buffer of this endpoint's past
  numeric values, sampled on a cadence, for the `history()`/`fractile()`/
  `median()`/`average()` functions in a task's `condition`/`script`/`set`
  `expr:` — see [Value history & fractiles](scripting.md#value-history--fractiles).
  Not the same thing as [`extensions/logdb`](logdb.md)'s long-term,
  disk-backed history used for graphing.

Given these, `Endpoint.to_text()`/`from_text()` (and the matching
`Device.get_text()`/`set_text()`) are the standard way to format a raw
value as display text and to parse text (or a raw value/label, e.g. `1` or
`"on"`) back into the endpoint's raw value — used by the `log` action's
`{text}` placeholder and the `set` action's `value:` parameter (or its
`expr:` alternative — see [Conditions, scripted actions & sticky
values](scripting.md)).
