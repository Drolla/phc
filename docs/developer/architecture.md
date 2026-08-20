# Architecture

How PHC is put together, and why. For the user-facing view of the same
concepts, see [concepts](../concepts.md).

## The shape of it

PHC is a fixed-heartbeat loop over a tree of devices, with tasks evaluated
against their state. Three ideas carry most of the design:

- **A device is a plugin plus a descriptor.** Behaviour lives in Python
  (`device.py`); what it accepts and exposes is declared in data
  (`module.yaml`). The declarative half is what lets the web UI build a
  working widget for a device module it has never heard of.
- **Endpoints are two-phase.** A value is *staged*, then *committed*, so
  every task in a tick sees one consistent snapshot rather than a
  half-updated world.
- **Everything above the core is an extension.** History logging, the web
  UI, timers, mail — none of it is special-cased in the core.

## Layers

Dependencies point one way. `phc.core.errors` is at the bottom precisely
so anything may import it without a cycle:

```
                    errors        (exception types; imports nothing)
                      |
     +----------------+----------------+
     |                |                |
  endpoint         intervals       logging_setup
     |
  health --- device --- registry --- scripting
                 |          |            |
                 +--- task -+------------+     (TaskRegistry, Action kinds)
                 |
              selectors
                 |
              config/          (a package: see below)
                 |
             scheduler
                 |
            cli / extensions
```

`phc.core.config` is itself layered, one module per stage of a load:

```
yamlio       -> (none)        !include / !placeholder; file to nested dict
descriptors  -> (none)        parsed module.yaml / extension.yaml
params       -> descriptors   parameter scope/override resolution
endpoints    -> descriptors, params    profiles, {param} templating, Endpoint
extensions   -> descriptors   the extensions: section
devices      -> descriptors, endpoints, params, yamlio
tasks        -> (none)        conditions, actions, tasks
hooks        -> (none)        the tick hooks the loader synthesizes
system       -> everything above       System + load_system()
```

Two dependency inversions were removed and are worth not reintroducing:

- **`ConfigError` lives in `errors`, not `config`.** It used to live in the
  loader, so a leaf like `selectors` had to import the whole loader just to
  name an exception.
- **`config` imports `task`, never the reverse.** Building a `Task` from a
  spec is the loader's job, but `create_task` needs it at *runtime* from
  inside `task.py`. Rather than importing back, the builder is injected
  into `TaskRegistry`, which owns the live task list and the context needed
  to build more.

## A tick

`Scheduler._tick_async` runs four passes, in this order, and the order is
load-bearing:

1. **Fetch.** Every due device's `fetch()` is awaited together. Blocking
   `receive()` implementations are bridged onto a bounded thread pool, so
   one slow device costs the tick its own latency, not the sum of all.
   Values are *staged*, not committed.
2. **Tasks.** Every task is evaluated against the state committed by the
   *previous* tick. Writes are collected rather than issued inline, then
   flushed concurrently.
3. **Commit.** Every device promotes its staged values and computes this
   tick's change events.
4. **Tick hooks.** Extensions' `on_tick`, history sampling, sticky-value
   tracking — after the commit, so they see *this* tick.

The one-tick lag in pass 2 is deliberate: it makes every task's view of the
world identical within a tick, so task order can never decide which task
sees a change first.

Failures are isolated per device: a raising or timing-out fetch is
recorded and logged, never propagated, so one flaky device cannot take down
a tick. That isolation is why [device health](#device-health) has to exist.

## Two clocks

Every *interval* runs on `time.monotonic()`; only an absolute *time of day*
runs on the wall clock.

| Wall clock (`time.time()`) | Monotonic |
| --- | --- |
| a task's `time:` / `repeat:` | the heartbeat grid |
| endpoint `update_time` | a device's `update:` |
| | a task's `min_interval:` |
| | endpoint history sampling |
| | endpoint `last_read_time` / `age()` |
| | device health timestamps |

An NTP correction or a DST change moves the wall clock; if intervals rode
on it, a backwards step would stall every poll in the system for the size
of the step, and a forwards step would fire a burst of catch-up polls. A
task set for `22:00` genuinely should move with the clock, which is why the
split exists rather than one clock winning.

Both are passed explicitly into `tick()`, so a caller can drive ticks at
whatever times it likes; that is how the test suite runs a day of
scheduling in milliseconds.

The heartbeat itself is a **deadline grid**: the next tick is scheduled one
heartbeat after the previous tick's *start*. Sleeping a heartbeat after it
*finishes* would make the real period `heartbeat + tick duration` — every
interval in the system running proportionally slow, forever.

## Devices and endpoints

A `Device` holds endpoints and child devices; there is no separate
host/leaf class. A "room" is just a device with no endpoints.

Devices are built into a tree but driven from a **flat index** of qualified
ids. `fetch()` deliberately does *not* recurse into children — the
Scheduler visits every device directly, so recursing would fetch a device
twice whenever it and its parent were both due, and would silently override
a child's own `update:` interval. `update_state()` skips recursion for the
same reason.

An `Endpoint` stages via `set()`/`set_raw()` and commits via
`update_state()`. It carries its own declared metadata — type, unit,
`values` mapping, range, format, transforms — which is what lets
`to_text()`/`from_text()` be the single conversion point between raw values
and anything user-facing.

State a device module shares between its own instances (a connection, a
batched-request registry) belongs in `Device.context`, a dict scoped to one
loaded system — not at module scope, which would outlive the `System` and
leak between two loaded in one process.

## Device health

Because I/O failures are swallowed to protect the tick, a dead device would
otherwise be invisible: its endpoints keep their last-good values, and a
frozen reading looks exactly like a steady one.

So each device records whether its last attempt succeeded, and each
endpoint when it last produced an actual reading — distinct from
`update_time`, which moves only when the value *changes*.

Most modules never raise, though: they catch their own network errors and
report `None` values on purpose. `Device.report_failure()` is how they say
so, and without it health would never trip for exactly the network-backed
modules it matters most for.

## Tasks

A `Task` is a schedule and/or a condition plus a list of actions. Gating is
a fixed short-circuit chain — due time, then condition, then
`min_interval` — so a cooldown is only consumed by a firing that actually
happened.

Conditions come in two forms: a `{device, changed, value}` shorthand, and
`expr:`, a restricted-Python expression. Actions are registered classes
(`@register_task_kind`), which is how an extension adds a new `kind:`.

`expr:`/`script:` run in a small AST-whitelist sandbox
(`phc.core.scripting`). It targets *mistake containment* for a trusted,
locally-authored config — a typo failing loudly at load rather than doing
something surprising — not a hostile-author threat model. The sandbox
knows nothing about devices; what a script can call is decided in one
place, `task._build_rule_namespace`, so conditions and scripts can never
drift apart in what they expose.

## Plugins

Two plugin kinds, both discovered the same three ways: bundled, from a
`phc.devices`/`phc.extensions` entry point, or from a `plugin_paths:`
directory. A config cannot tell which.

A device module's descriptor is read from whichever package defined its
`Device` subclass, so it travels with the code rather than being assumed to
sit under `phc.devices`.

Discovery never swallows an `ImportError`: a plugin whose own imports fail
must say so, rather than silently not existing and surfacing later as
"unknown module".

See [writing a device module](writing-a-device-module.md) and [writing an
extension](writing-an-extension.md).

## Configuration

`load_system()` is the only way a `System` is built. It configures logging,
discovers plugins, builds devices, then extensions, then tasks — and
finally calls `on_bind` on every extension instance, which is the first
moment the whole system exists.

The loader's strong bias is to **fail at load time**. An unrecognized key,
a selector matching nothing, a reference to an extension instance that
doesn't exist, a `!placeholder` left unreplaced — all refuse to start. For
a system that runs unattended for months, a startup error is enormously
cheaper than a silent misbehaviour discovered later.
