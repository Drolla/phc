# Writing an extension

An extension is anything PHC does that isn't a device: logging history to
disk, serving a web UI, sending mail, persisting values across a restart.
It follows the same package-plus-descriptor shape as a [device
module](writing-a-device-module.md) — a package containing:

- `extension.py` — a `configure()` entry point, plus any task action kinds
  it registers.
- `extension.yaml` — its declared parameters, in user-facing language.

Bundled extensions live in `phc/extensions/<name>/`, but an extension can
live anywhere: see [shipping a module outside
PHC](writing-a-device-module.md#shipping-a-module-outside-phc), which
applies identically here (entry point group `phc.extensions`, or a
`plugin_paths:` directory).

## Instances

Unlike a device module, an extension is configured as **named instances**:

```yaml
extensions:
  logdb:
    house_log:        # instance name -- "logdb.house_log" is its key
      csv_path: "logs/house.csv"
      selectors: ["*"]
    garden_log:       # a second, independently configured instance
      csv_path: "logs/garden.csv"
      selectors: ["garden.*/*"]
```

Each instance's params are merged against `extension.yaml` and passed to
`configure()` separately, so one extension can serve several unrelated
purposes in one system. An extension with no entry in `extensions:` is
never instantiated at all.

## `configure()`

```python
def configure(params: dict, flat: dict[str, Device], instance_key: str,
              extensions_registry: dict | None = None) -> "MyInstance":
    ...
```

- `params` — this instance's values, already merged against
  `extension.yaml`'s declared parameters and defaults. A parameter the
  descriptor doesn't declare is rejected before you see it.
- `flat` — every device, by qualified id. Resolve
  [selectors](../configuration.md) against it here, once, rather than on
  every tick.
- `instance_key` — `"<extension>.<instance>"`, e.g. `"logdb.house_log"`.
  Use it in log messages and as a subscriber id (see
  `Endpoint.subscribe_log`).
- `extensions_registry` — the live registry of other instances. It is
  still being filled while `configure()` runs, so **do not** resolve
  another instance here; do it in `on_bind` (below).

Raise `ConfigError` for anything wrong with the configuration — a selector
matching nothing, a mutually exclusive pair of options. Failing at load is
much better than failing at 3am on the first tick that needs the value.

Return an instance object. Whatever it is, PHC only looks for the
lifecycle hooks below.

## Lifecycle hooks

All four are optional. An extension that only registers a task action kind
(`mail_alert`) implements none. They run in this order:

| Hook | When | Notes |
| --- | --- | --- |
| `on_bind(system)` | once, at the end of `load_system()` | Synchronous. The `System` is complete here — devices, tasks, and every other extension instance. |
| `on_start(devices)` | once, before the first tick | `async`. Runs on the Scheduler's own event loop. |
| `on_tick(devices)` | every tick | Synchronous, after that tick's state is committed. |
| `on_stop(devices)` | once, after the last tick | `async`, same loop as `on_start`. |

Two things follow from the ordering:

**Resolve cross-extension references in `on_bind`, not `configure()`.** The
instance you want may be declared later in the file. `on_bind` is the first
moment everything exists — and raising there aborts the load, so a typo'd
reference becomes a startup error rather than a runtime surprise. This is
what `extensions/web_ui` does for its graph and timer panels.

**`on_tick` sees this tick's state, tasks see the previous tick's.** Hooks
run after the commit pass, so `get_event()` reflects the tick you are in
(see [Tasks and the heartbeat](../configuration.md#tasks-and-the-heartbeat-a-one-tick-lag)).

`on_start`, `on_tick` and `on_stop` are each isolated: one instance raising
is logged and does not stop the tick or the other instances. `on_bind` is
deliberately not — a configuration problem should stop startup.

Hooks are found **by name**. A method called `on_tik` is not a broken hook,
it is not a hook at all — so PHC checks for near-misses at load time and
refuses to start rather than let an extension silently never run.

## Registering a task action kind

To give configs a new `kind:` for `action:`, register an `Action`
subclass. The decorator runs on import, and PHC imports every discovered
`extension.py` at startup:

```python
from phc.core.registry import register_task_kind
from phc.core.task import Action

@register_task_kind("mail_alert")
class MailAlertAction(Action):
    def __init__(self, *, instance: str, extensions: dict, title: str, **params):
        super().__init__(**params)
        try:
            self._instance = extensions[instance]
        except KeyError:
            raise ConfigError(f"mail_alert action: unknown instance {instance!r}") from None
        self._title = title

    def perform(self, devices):
        self._instance.send(title=self._title, ...)
```

Every action kind receives the same context keywords (`flat`, `tasks`,
`extensions`, `task_tag`, `sticky_endpoints`, `task_specs`); take the ones
you need and let `**params` absorb the rest. An action that targets a
device also gets `device_id`/`endpoint_key` resolved from its `device:`
key.

Validate in `__init__` — it runs at config-load time, so an unknown
instance name or missing argument is reported before the system starts.

## Persisting state

If your extension writes a file, write it atomically: to a sibling
temp file, then `os.replace()`. A crash mid-write must never leave a
half-written file where a good one was.
`extensions/recovery/recovery.py` and `extensions/timer/timer.py` both show
the pattern, including tolerating a missing or corrupt file on load rather
than refusing to start.

Declare a file parameter with `path: true` in `extension.yaml`, and PHC
resolves a relative value against the system YAML's own directory before
your `configure()` sees it — so a config directory stays self-contained,
and you never handle the resolution (or the working directory) yourself.

## `extension.yaml`

```yaml
description: >
  What this extension does, in plain English -- shown by
  `phc list-extensions` and rendered in the web UI.

parameters:
  - name: path
    override: required
    description: Where to store the data.
  - name: interval
    override: allowed
    default: "10m"
    description: How often to write a sample.
```

`override` is `allowed` (default), `required`, or `none`. There is no
`scope` — an extension has no equivalent of a device module's
`modules.<name>` section, since every instance is configured
independently.

These descriptions are user-facing documentation, not implementation
notes: they are parsed at runtime and displayed. Implementation detail
belongs in the Python docstrings.
