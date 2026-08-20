"""logdb extension wiring: resolves an extensions.logdb.<instance>'s
`selectors` list against the live device tree, subscribes every matched
endpoint for sticky log tracking, builds a LogDb store, and registers the
"log_db" task action kind so a hand-authored `tasks:` entry can sample it
on its own schedule -- see phc.core.config._load_extensions for how
configure() is invoked, and phc.core.registry.discover_extensions() for how
this module's @register_task_kind decorator gets imported at startup."""

import time

from phc.core.device import Device
from phc.core.errors import ConfigError
from phc.core.intervals import parse_duration
from phc.core.registry import register_task_kind
from phc.core.selectors import resolve_selectors
from phc.core.task import Action
from phc.extensions.logdb.logdb import LogDb


class LogDbInstance:
    """One configured logdb instance: a LogDb store plus the resolved pairs
    it samples, each subscribed for sticky min/max tracking (see
    phc.core.endpoint.Endpoint.subscribe_log). Looked up by name from a log_db
    task action (see phc.core.config._load_extensions/_build_action)."""

    def __init__(self, store: LogDb, pairs: list[tuple[str, str]], subscriber_id: str):
        self.store = store
        self.pairs = pairs
        self.subscriber_id = subscriber_id

    def on_tick(self, devices: dict[str, Device]) -> None:
        """Called once per tick (see phc.core.scheduler's tick_hooks pass, after
        commit) to advance each subscribed endpoint's sticky value from this
        tick's freshly committed state. Auto-registered by
        phc.core.config.load_system() for every extension instance exposing
        this method -- no YAML wiring needed."""
        for qualified_id, endpoint_key in self.pairs:
            device = devices.get(qualified_id)
            if device is None:
                continue
            device.endpoint(endpoint_key).update_log_value()

    def sample(self, devices: dict[str, Device]) -> None:
        """Called when the log_db task fires: reads (and invalidates) each
        subscribed endpoint's sticky value -- not the live value -- so a
        brief event between two log samples is still captured (per
        log_aggregation) rather than only whatever the state happened to
        be exactly when the task fired. Only bool/int/float sticky values
        are stored (bool -> 0.0/1.0); other types are simply absent from
        this row (LogDb.log() records them as "no data"). A float value
        is rounded per the endpoint's declared `format` (e.g. ".1f") --
        the same precision to_text() would display -- so raw sensor
        noise (e.g. 23.834982187699923) doesn't bloat the CSV with
        digits beyond what the endpoint considers meaningful."""
        now = time.time()
        values: dict[str, float] = {}
        for qualified_id, endpoint_key in self.pairs:
            device = devices.get(qualified_id)
            if device is None:
                continue
            endpoint = device.endpoint(endpoint_key)
            raw = endpoint.get_log_value(self.subscriber_id)
            if isinstance(raw, bool):
                values[f"{qualified_id}/{endpoint_key}"] = 1.0 if raw else 0.0
            elif isinstance(raw, (int, float)):
                value = float(raw)
                if endpoint.format:
                    value = float(format(value, endpoint.format))
                values[f"{qualified_id}/{endpoint_key}"] = value
            endpoint.invalidate_log_value(self.subscriber_id)
        self.store.log(now, values)


def configure(params: dict, flat: dict[str, Device], instance_key: str,
              extensions_registry: dict | None = None) -> LogDbInstance:
    """Extension entry point (see phc.core.config._load_extensions): resolve the
    selectors once against the static device tree, subscribe every
    resolved endpoint for sticky log tracking under `instance_key`, and
    open/restore the CSV-backed store. `extensions_registry` (see
    phc.core.config._load_extensions) is unused here -- logdb never references
    another extension's instance; it's the one other extensions (e.g.
    phc.extensions.web_ui's graph panels) reference by name."""
    pairs = resolve_selectors(params["selectors"], flat)
    for qualified_id, endpoint_key in pairs:
        flat[qualified_id].endpoint(endpoint_key).subscribe_log(instance_key)
    labels = [f"{qid}/{key}" for qid, key in pairs]
    max_age = parse_duration(params["max_age"]) if params["max_age"] is not None else None
    store = LogDb(
        params["csv_path"], labels,
        full_vector_interval=int(params["full_vector_interval"]),
        max_records=params["max_records"], max_age=max_age,
        header_reserve_bytes=params["header_reserve_bytes"],
    )
    return LogDbInstance(store, pairs, subscriber_id=instance_key)


@register_task_kind("log_db")
class LogDbAction(Action):
    """Samples the named logdb instance when this task fires. Has no single
    target device -- its YAML spec never has a `device:` key, so
    device_id/endpoint_key stay None (see Action)."""

    def __init__(self, *, instance: str, extensions: dict, **params):
        super().__init__(**params)
        try:
            self._instance = extensions[instance]
        except KeyError:
            raise ConfigError(
                f"log_db action: unknown extensions instance {instance!r}; "
                f"available: {sorted(extensions)}") from None

    def perform(self, devices: dict[str, Device]) -> None:
        self._instance.sample(devices)
