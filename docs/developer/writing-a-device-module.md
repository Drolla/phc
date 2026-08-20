# Writing a device module

A new device type is a new package containing a `device.py` and a
`module.yaml`. It does **not** have to live inside PHC — see [Shipping a
module outside PHC](#shipping-a-module-outside-phc) below. Bundled
modules live in `phc/devices/<name>/`, and everything in this page applies
to all three cases equally.

A new bundled device type is a new `phc/devices/<name>/` package containing:

- `device.py` — a `Device` subclass decorated with `@register_module("<name>")`.
- `module.yaml` — its declared parameters, endpoints, and (if any endpoint
  needs a protocol field like zway's `command_group`/`address`) declared
  `endpoint_parameters:`.

See any existing module (e.g. [`phc/devices/virtual/`](../../phc/devices/virtual/))
for the minimal shape, [`phc/devices/meteoswiss/`](../../phc/devices/meteoswiss/) for
a fuller, network-backed example, or [`phc/devices/zway/`](../../phc/devices/zway/)
for one using `endpoint_parameters:` and a two-axis endpoint/device profile
library.

## `module.yaml` schema

`parameters:` declares the module's device-level params: a list of `{name,
description, default, override, scope}` entries. A declared name becomes an
ordinary top-level field on a device entry (and, for a `scope: module`
param, under `modules.<name>` too) — there is no `params:` nesting.
`override` is one of `allowed` (default), `required`, or `none`; `scope` is
`device` (default) or `module` (see [Modules and shared
configuration](../configuration.md) for how a system config supplies these
values). A parameter name is checked against every reserved device/modules
entry key (and the literal name `"params"`) so it can't collide.

`endpoint_parameters:` declares the module's own per-endpoint protocol
fields (e.g. zway's `command_group`/`address`) — a list of `{name,
description}` entries, mirroring `parameters:`'s schema but with no
`default`/`override`/`scope` (an endpoint has no equivalent of
`modules.<name>` to resolve against). A declared name becomes a legal
top-level key on any endpoint spec of this module, folded into
`Endpoint.params` once every profile/overlay/`{param}` step has resolved.

`endpoint_profiles`/`device_profiles` are an optional reusable-endpoint
library a module can ship: an **endpoint profile** is a full endpoint spec
(the same shape a user writes by hand) with `{param}` templates in any of
its string fields (typically a declared endpoint parameter like `address`,
but `description`/`unit`/`values` work too), and a **device profile** names
one product — optional `brand`/`type`/`product`/`description` metadata,
plus a named `endpoints:` list of `{key, endpoint_profile, ...}` entries
(typically supplying the product's addresses, which an endpoint profile
never hardcodes). A device opts in via `device_profile:` (whole device) or
an endpoint's own `endpoint_profile:` (single endpoint) — writing endpoints
out fully explicitly, with neither key anywhere, is unaffected either way.
`{param}` templating itself is not tied to profiles at all — it runs on
every endpoint of every device, whether or not a profile was used to
produce it.

`device_profiles` is mutually exclusive with a non-empty `endpoints:` on
the same module: `endpoints:` is unconditional (every device of the module
gets them, e.g. meteoswiss's six), while a `device_profiles` entry is
opt-in — mixing the two would make one of "module endpoints" or "profile
endpoints" the base and the other the overlay depending on call order,
which is not something a reader of the YAML could tell.

A system config can extend a module's profile library too, without
touching the module's own `module.yaml` — see [Endpoint and device
profiles](../profiles.md).

## Shipping a module outside PHC

A device module is discovered by the same mechanism wherever it lives, and
a system YAML cannot tell the difference — `module: <name>` either way.
Its `module.yaml` is always read from the package its `Device` subclass was
defined in, so it travels with the code.

**As part of a distribution** — the normal way to publish a plugin.
Advertise an entry point in the `phc.devices` group (or `phc.extensions`
for an extension), where the *name* is what configs write and the *value*
is the package holding `device.py` and `module.yaml`:

```toml
[project.entry-points."phc.devices"]
acme_sensor = "acme_phc.acme_sensor"
```

Installing that distribution alongside PHC is all that is needed; there is
nothing to register and no PHC file to edit.

**As a local directory** — for a private module not worth packaging, e.g.
one belonging to a single household's config. Point `plugin_paths:` at a
directory laid out like `phc/devices/`, one subdirectory per module:

```yaml
plugin_paths: ["./my_modules"]

devices:
  - id: shed
    module: acme_sensor      # from ./my_modules/acme_sensor/
```

Paths are resolved relative to the system YAML's own directory, so a
config that carries its private modules alongside itself stays
relocatable. Each directory goes on `sys.path`, so its subdirectories are
imported as ordinary top-level packages — give them distinctive names.

If a plugin's own `device.py` fails to import (a missing dependency, say),
that error is raised, naming what went wrong. It is not treated as "no
such module".

## Reporting I/O failures

If your `receive()`/`receive_async()` lets an exception propagate, PHC
records the failure for you: the device is marked unhealthy, shown as such
in the web UI and debug portal, and `available()` goes false for its
endpoints.

Most real modules don't do that, though — they catch their own network
errors and report every endpoint as `None`, so that one unreachable
controller can't disturb the tick. From PHC's side that is
indistinguishable from a completely successful fetch, so tell it:

```python
try:
    payload = await self._get()
except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
    self.report_failure(f"{type(exc).__name__}: {exc}")
    payload = None
```

Call it where you catch the error. One fetch counts as one attempt no
matter how many times you call it, and a fetch that reports nothing counts
as a success. Every bundled network module (`zway`, `meteoswiss`,
`open_meteo`, `waveplus_bridge`) does this.

## Sharing state between a module's devices

A module often needs state shared by several of its own device instances:
a connection or session, a cache, a registry that lets sibling devices
coalesce their reads into one request. Put it in `self.context`, a plain
dict shared by every device built from one system config, under a key
named for your module:

```python
def setup(self):
    state = self.context.get("mymodule")
    if state is None:
        state = self.context["mymodule"] = MyModuleState()
    self._state = state
```

`self.context` is assigned before `setup()` runs, so it is available from
there onwards. A directly-constructed `Device` (a test, a script) that
passes no context simply gets its own empty dict.

Do **not** keep this at module scope. A module-level dict is shared by
every system loaded in the process, which is the wrong lifetime: it
outlives the `System` it belongs to, leaks between two systems loaded
together, and forces tests to reach in and reset your module's internals
by hand. It is a particularly bad home for an `asyncio.Lock`, which binds
to the first event loop that contends for it and then fails against any
later one.

[`phc/devices/zway/`](../../phc/devices/zway/) shows the pattern at full
size (`_ZWayState`): a batched-fetch identifier registry, a response
cache, session cookies and several locks, all shared between the devices
of one system and isolated from any other.
