# Viessmann Heating Integration

[`phc/devices/viessmann/`](../phc/devices/viessmann/) reads and controls a
Viessmann heating installation — a heat pump, a boiler, the gateway, a room
control — through Viessmann's ViCare cloud API. There is nothing to install
on the heating system: the gateway (a Vitoconnect) already reports to
Viessmann's cloud, and PHC reads the same data the ViCare phone app does.

```yaml
modules:
  viessmann:
    update: 5m
    email: !placeholder <Viessmann account e-mail>
    password: !placeholder <Viessmann account password>
    client_id: !placeholder <Client ID>
    cache_time: 5m

devices:
  - id: heatpump
    module: viessmann
    name: Heat Pump
    device_id: "0"
    endpoints:
      - key: outside_temperature
        endpoint_profile: outside_temperature
      - key: dhw_target_temperature
        endpoint_profile: dhw_target_temperature
```

A runnable example is in
[`examples/viessmann_heatpump.yaml`](../examples/viessmann_heatpump.yaml),
with its device list in
[`examples/devices/viessmann_heatpump.yaml`](../examples/devices/viessmann_heatpump.yaml).


## What You Need

- **A Viessmann account** — the one the installation is registered to, the
  same credentials the ViCare app uses.
- **A client id**, created once at
  [app.developer.viessmann-climatesolutions.com](https://app.developer.viessmann-climatesolutions.com):
  sign in with the same account, add a client, and give it

  - any name,
  - **Google reCAPTCHA disabled** — with it enabled the login below cannot
    complete,
  - redirect URI `vicare://oauth-callback/everest`.

  Copy the client id it issues. There is no client secret.
- **The device id** of whatever you want to read, from `discover.py
  topology` below. The heating system itself is usually `"0"`.

PHC talks to the cloud, not to the heating system directly, so it needs
internet access and inherits whatever the cloud knows. Readings are
typically a few minutes old even at the source.


## The API Quota Shapes Everything

The free tier allows **120 calls per 10 minutes and 1450 per day**, as a
sliding window. Exceeding either **blocks the account for 24 hours** —
affecting the ViCare app too, not just PHC.

One poll reads every feature of a gateway in a single call, so reads are
cheap: at the default `update: 5m` a gateway costs under 300 calls a day.
Two things spend the rest:

- **Every write is one more call.** A task that writes on a condition that
  flickers can spend hundreds a day. Give any writing task a `min_interval`,
  or fire it on a clock instead — see the example config's hot-water tasks.
- **Each gateway polls separately.** Devices on one gateway share a response
  (see `cache_time` below); a second gateway doubles the cost.

Polling harder than 5 minutes buys little: a heat pump is a slow system,
and the cloud's own data is not fresher than that. `update: 1m` would spend
1440 calls a day — essentially the entire budget, leaving nothing for
writes and no margin for a restart.


## Finding Out What to Put There

**Which features exist depends entirely on your hardware.** A heat pump
reports things a boiler does not; heating circuits are numbered per
installation; a room control with no paired thermostats reports almost
nothing. So rather than copying an endpoint list, list your own:

```console
$ python -m phc.devices.viessmann.discover --config house.yaml topology
INSTALLATION   GATEWAY            DEVICE             MODEL                TYPE             STATUS
----------------------------------------------------------------------------------------------
2800335        7637415046982246   gateway            Heatbox2_SRC         vitoconnect      Online
2800335        7637415046982246   RoomControl-1      Smart_RoomControl    roomControl      Online
2800335        7637415046982246   0                  CU401B_S             heating          Online
```

Then list one device's features:

```console
$ python -m phc.devices.viessmann.discover --config house.yaml \
      features --device 0 --filter "heating.dhw.*"

device 0  (CU401B_S, 194 features, 120 enabled)

FEATURE                                      PROPERTY  VALUE   UNIT     RW  COMMANDS
heating.dhw.sensors.temperature.dhwCylinder   value     44.2    celsius  r
heating.dhw.temperature.main                  value     52      celsius  rw  setTargetTemperature(temperature: 10..60 step 1)
heating.dhw.oneTimeCharge                     active    False            rw  activate(); deactivate(); setActive(active: boolean)
```

And have it write the YAML for you, to paste in and edit down:

```console
$ python -m phc.devices.viessmann.discover --config house.yaml \
      yaml --device 0 --filter "heating.dhw.*"
```

Useful flags: `--writable-only` for just the settable features,
`--include-disabled` to see what the installation reports but has switched
off, `--raw` for the API's own JSON, and `--calls` is always printed so you
can see what a run cost. `login` makes **no** feature calls, so it is the
safe one to repeat while sorting out credentials.

Credentials come from `--config <your config>.yaml` (read out of its
`modules.viessmann:` section) or from `--email`/`--password`/`--client-id`.


## Naming a Feature

An endpoint names a **feature** and one of its **properties**:

```yaml
- key: outside_temperature
  type: float
  unit: "°C"
  feature: heating.sensors.temperature.outside
  property: value
```

They are two separate fields because the API's own feature names are
already dotted, and the property is not part of that name — gluing them
would make `heating.compressors.0.statistics.hours` ambiguous.

`property` defaults to `value`, which covers most readings. The others are
worth knowing because they are easy to miss — a feature can carry several
useful values at once:

| Property | Example feature | Holds |
|---|---|---|
| `value` | `heating.sensors.temperature.outside` | the reading |
| `status` | `heating.circuits.2.circulation.pump` | `"on"` / `"off"`, or `"connected"` for a sensor |
| `active` | `heating.compressors.0` | whether it is running |
| `phase` | `heating.compressors.0` | `"heating"`, `"defrost"`, … |
| `hours`, `starts` | `heating.compressors.0.statistics` | lifetime counters |
| `slope`, `shift` | `heating.circuits.2.heating.curve` | the two heating-curve values |
| `temperature` | `heating.circuits.2.operating.programs.normal` | a target, not a measurement |

A mistyped feature or property simply reads **unavailable** — it does not
mark the device unhealthy, because nothing is wrong with the connection. So
check a blank value against `discover.py` before assuming an outage.

Features the installation reports but has switched off (`isEnabled: false`)
also read unavailable, deliberately: their values are stale and mean
nothing.

For the readings most installations share, `module.yaml` ships
**endpoint profiles** that carry the type, unit and feature name, so a
config only opts in:

```yaml
- key: outside_temperature
  endpoint_profile: outside_temperature
```

Available profiles: `outside_temperature`, `dhw_temperature`,
`dhw_target_temperature`, `supply_temperature`, `return_temperature`,
`compressor_active`, `compressor_phase`, `compressor_hours`,
`compressor_starts`, `cop`, `wifi_strength`. Anything else is written out
in full, which is what `discover.py yaml` generates.


## Types

Mostly ordinary, with one trap:

**A `status` property is a string, not a boolean.** The API reports the word
`"off"`, and `bool("off")` is `true` — so a `type: bool` endpoint on a
pump's `status` would read "on" while the pump is off. Use `type: str`:

```yaml
- key: circulation_pump
  type: str
  feature: heating.circuits.2.circulation.pump
  property: status
```

Genuine booleans (`active` on a compressor or a one-time charge) really are
`true`/`false`, so `type: bool` with a `values:` mapping is right for those
and the web UI renders a toggle.

`stepping` is not enforced locally — PHC endpoints have `min`/`max` but no
step. A value that misses the step (a heating-curve slope of 1.15 where the
step is 0.1) is rejected by the API, and the device reports the refusal.


## Writes

A writable endpoint names the **command** that applies it:

```yaml
- key: dhw_target_temperature
  type: int
  unit: "°C"
  writable: true
  min: 10
  max: 60
  on_invalid: reject
  feature: heating.dhw.temperature.main
  property: value
  command: setTargetTemperature
  param: temperature
```

`param` says which of the command's parameters carries the value. It can be
left out when the command takes exactly one — the module reads that from
the installation's own response rather than guessing. When a command sets
several values at once, `command_extras` supplies the rest:

```yaml
- key: heating_curve_slope
  writable: true
  feature: heating.circuits.2.heating.curve
  property: slope
  command: setCurve
  param: slope
  command_extras: { shift: 0 }       # setCurve writes both at once
```

Keep such a fixed value in step with reality: a stale `shift` here would
quietly undo a shift set elsewhere every time the slope is written.

Setting `min`/`max` with `on_invalid: reject` is worth doing on every
writable endpoint — it stops an out-of-range value locally instead of
spending an API call to be told no.

Some commands are reported but refused (`isExecutable: false`) — a
compressor's `setActive`, for instance, or a hysteresis command while the
system is mid-cycle. The module checks before sending and reports the
refusal rather than wasting a call.

After a successful write the cached reading is dropped, so the next poll
reads back what the installation actually accepted. A value it clamped or
ignored shows up rather than appearing applied.

For a feature with `activate()`/`deactivate()` and a `setActive(active:
boolean)` — hot-water one-time charge is the common one — use `setActive`
with `type: bool`. The zero-argument pair would need a "pick the command
from the value" mechanism that nothing else needs.

Schedules (`heating.dhw.schedule` and friends) are **not** exposed: a week
of time slots is not one scalar, so it cannot be a PHC endpoint. Set those
in the ViCare app.


## Authentication

The module logs in with the account e-mail and password and holds the
resulting token, renewing it when it expires (roughly hourly). This is the
same headless mechanism the PyViCare library and Home Assistant's ViCare
integration use; no browser is involved.

Two consequences worth knowing:

- **The password is sent on each renewal**, not only at startup — the
  library renews by logging in again rather than by using a refresh token.
  It costs an auth call about once an hour, which is immaterial against the
  quota.
- **Credentials sit in the config file in plain text**, and they are for
  the whole Viessmann account, not just this API. See *Security* below.

`token_file` optionally caches the token so a restart does not need a fresh
login. Give it an **absolute** path: a relative one resolves against the
directory PHC was started from, so a service-started run would not find a
token written by a hand-started one. The file is as sensitive as the
password.

When a login fails for good — a wrong password, a revoked account — the
device reports itself unhealthy with a message naming the fix, and backs
off (5m, 10m, 20m, …, hourly) rather than retrying on every poll. It never
stops PHC from starting: one misconfigured heating system should not take
down the rest of the installation. Fix the credentials and restart, or
verify them first with `discover.py login`.


## Freshness, Quota and Failure

`cache_time` (default 5m, module-scoped) is how long one gateway's response
may be reused. Devices on the same gateway share it, so a heat pump and its
gateway cost **one** call per poll between them rather than two. Set it to
`0s` to always re-fetch, at proportionally more calls.

A failed fetch is never cached, so the next poll retries instead of serving
an error for a whole cache window. Each endpoint reads unavailable and the
device shows as unhealthy in the web UI and debug portal.

If the quota does get exceeded, the module reports the reset time the API
gives it and stops calling until then — every further call while blocked
extends the block. The same backoff covers credential failures. In both
cases the device keeps reporting unhealthy on every poll, so the problem
stays visible; it just stops spending calls to rediscover it.


## Security

The config file holds credentials for your entire Viessmann account. Treat
it accordingly:

- Make it readable only by the user running PHC
  (`chmod 600`, or Windows ACLs).
- Consider keeping the credentials in their own file and pulling them in
  with `<<: !include secrets/viessmann.yaml`, so the main config can be
  shared or version-controlled while the secrets are not.
- A `token_file` is equally sensitive — it grants API access without the
  password.
- `!placeholder` (see
  [`docs/configuration.md`](configuration.md#placeholder-values)) keeps a
  config that has never been filled in from starting at all, which is why
  the shipped example uses it.

If the account is ever compromised, changing the Viessmann password
invalidates both the stored credentials and any cached token.
