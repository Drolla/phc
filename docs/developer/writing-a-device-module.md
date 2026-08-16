# Writing a device module

A new device type is a new `phc/devices/<name>/` package containing:

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
