"""recovery extension wiring: resolves an extensions.recovery.<instance>'s
`selectors` list against the live device tree (post-filtered to writable
endpoints only, since restoring requires Device.set(), which requires
writable=True), persists their current value to a small YAML file
whenever one of them changes, and restores every persisted value once at
startup, before the scheduler's first tick -- see core.config._load_extensions
for how configure() is invoked, and core.scheduler.Scheduler for
start_hooks/tick_hooks/stop_hooks timing."""

import logging

from core.config import ConfigError
from core.device import Device
from core.selectors import resolve_selectors
from extensions.recovery.recovery import RecoveryStore

logger = logging.getLogger("phc.recovery")


class RecoveryInstance:
    """One configured recovery instance: a RecoveryStore plus the resolved,
    writable-only pairs it persists/restores. Looked up by nothing else --
    unlike extensions.logdb, recovery has no task action; it is entirely
    automatic (see on_start/on_tick/on_stop, auto-collected by
    core.config.load_system())."""

    def __init__(self, store: RecoveryStore, pairs: list[tuple[str, str]], instance_key: str):
        self.store = store
        self.pairs = pairs
        self.instance_key = instance_key

    async def on_start(self, devices: dict[str, Device]) -> None:
        """Restore every persisted pair's value, once, before the
        scheduler's first tick (see Scheduler.start_hooks). start_hooks
        run to completion before the tick loop -- and on_tick, the writer
        -- ever begins (core.scheduler.Scheduler._run_async), so unlike
        THC's RecoverAll no re-entrancy guard against self-triggered writes
        is needed here.

        Each persisted entry is restored independently: a device/endpoint
        that no longer exists, or is no longer writable (config may have
        changed since the file was written), is skipped with a warning;
        any exception from Device.set() itself (bad value, transmit
        failure, ...) is caught, logged, and does not block the rest of
        the restore or abort startup. Restoring is a normal write (routed
        through Device.set() -> transmit()), pushed to the device
        immediately -- whether it's later observable via get() depends on
        that device's own read path, same as any other write."""
        values = self.store.load()
        restored, skipped = 0, 0
        for pair_key, value in values.items():
            qualified_id, _, endpoint_key = pair_key.partition("/")
            device = devices.get(qualified_id)
            if device is None or endpoint_key not in device.endpoints:
                logger.warning("recovery %s: %r no longer exists; skipping",
                                self.instance_key, pair_key)
                skipped += 1
                continue
            if not device.endpoint(endpoint_key).writable:
                logger.warning("recovery %s: %r is no longer writable; skipping",
                                self.instance_key, pair_key)
                skipped += 1
                continue
            try:
                device.set(value, name=endpoint_key)
                restored += 1
            except Exception:
                logger.exception("recovery %s: failed to restore %r to %r",
                                  self.instance_key, pair_key, value)
                skipped += 1
        logger.info("recovery %s: restored %d, skipped %d of %d persisted value(s)",
                     self.instance_key, restored, skipped, len(values))

    def on_tick(self, devices: dict[str, Device]) -> None:
        """Rewrite the whole file if any subscribed pair changed this tick
        (see core.endpoint.Endpoint.get_event(), already deduped against a
        same-value re-write by Endpoint.update_state()). Auto-registered by
        core.config.load_system() as a Scheduler tick hook -- runs after
        this tick's state is fully committed (core.scheduler._tick_async,
        pass 4), so get_event() reflects it."""
        for qualified_id, endpoint_key in self.pairs:
            device = devices.get(qualified_id)
            if device is None:
                continue
            if device.endpoint(endpoint_key).get_event() is not None:
                self._persist(devices)
                return

    async def on_stop(self, devices: dict[str, Device]) -> None:
        """Do one final, unconditional persist on graceful shutdown (see
        Scheduler.stop_hooks) -- covers the very last tick's change even if
        on_tick's own write already handled it; cheap, since it's the same
        whole-file rewrite either way."""
        self._persist(devices)

    def _persist(self, devices: dict[str, Device]) -> None:
        """Build {pair_key: value} from each pair's current committed
        value and write it. A pair whose device has disappeared, or whose
        value is still None (never yet set -- nothing meaningful to
        persist), is omitted from the written file."""
        values = {}
        for qualified_id, endpoint_key in self.pairs:
            device = devices.get(qualified_id)
            if device is None:
                continue
            value = device.endpoint(endpoint_key).get()
            if value is None:
                continue
            values[f"{qualified_id}/{endpoint_key}"] = value
        self.store.write(values)


def configure(params: dict, flat: dict[str, Device], instance_key: str,
              extensions_registry: dict | None = None) -> RecoveryInstance:
    """Extension entry point (see core.config._load_extensions): resolve
    the selectors once against the static device tree, reject at
    configure-time (ConfigError) any selector that matches zero endpoints
    or matches a non-writable one -- both are almost certainly
    configuration mistakes, unlike a device/endpoint disappearing between
    persist and restore, which is tolerated (see RecoveryInstance.on_start).
    `extensions_registry` is unused -- recovery never references, nor is
    referenced by, another extension's instance."""
    pairs = resolve_selectors(params["selectors"], flat)
    if not pairs:
        raise ConfigError(
            f"recovery instance {instance_key!r}: selectors matched zero endpoints")
    non_writable = [(qid, key) for qid, key in pairs if not flat[qid].endpoint(key).writable]
    if non_writable:
        names = ", ".join(f"{qid}/{key}" for qid, key in non_writable)
        raise ConfigError(
            f"recovery instance {instance_key!r}: selector(s) matched non-writable "
            f"endpoint(s) {names} -- recovery only applies to writable endpoints; "
            f"narrow the selectors or drop the read-only ones")
    store = RecoveryStore(params["path"])
    return RecoveryInstance(store, pairs, instance_key)
