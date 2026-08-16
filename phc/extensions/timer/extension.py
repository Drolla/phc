"""timer extension wiring: resolves an extensions.timer.<instance>'s
`selectors` list against the live device tree (post-filtered to writable
endpoints only, same rule as phc.extensions.recovery, since a timer's whole
point is writing to its target), restores every persisted timer as a real
phc.core.task.Task once at startup (see on_bind), and exposes the CRUD API
`phc/extensions/web_ui`'s `kind: timers` panel drives (see
phc/extensions/web_ui/panels.py's TimersPanel and phc/extensions/web_ui/server.py's
timer routes) -- see phc.core.config._load_extensions for how configure() is
invoked, and phc.core.scheduler.Scheduler for on_tick/on_bind timing.

Timers are deliberately NOT a parallel scheduler: each becomes an ordinary
phc.core.task.Task (via phc.core.task.register_task), tagged "<instance_key>.<id>",
so firing, one-shot retirement, and repeat catch-up are all handled by
phc.core.task.Task.run()/phc.core.scheduler.Scheduler exactly like a hand-authored
`tasks:` entry -- see _task_spec()."""

import logging
import time

from phc.core.errors import ConfigError
from phc.core.device import Device
from phc.core.intervals import parse_duration, parse_time
from phc.core.selectors import resolve_selectors
from phc.core.task import kill_tasks, register_task
from phc.extensions.timer.timer import TimerDef, TimerStore, expired_one_shot

logger = logging.getLogger("phc.timer")


def _task_spec(timer: TimerDef, instance_key: str) -> dict:
    """Build one timer's phc.core.task Task spec (the same shape as a `tasks:`
    YAML entry -- see phc.core.config._build_task), consumed by
    phc.core.task.register_task. `time:` is always the timer's own literal
    Unix timestamp (parse_time's digit-string branch); `repeat:` (if set)
    then drives parse_time's own "already past -> advance by whole
    multiples" catch-up logic -- no separate rolling-forward code is
    needed here for repeating timers."""
    action_spec = {"kind": timer.action, "device": f"{timer.device}.{timer.endpoint}"}
    if timer.action == "set":
        action_spec["value"] = timer.value
    return {
        "tag": timer.tag(instance_key),
        "description": timer.description,
        "time": str(int(timer.time)),
        "repeat": timer.repeat,
        "action": action_spec,
    }


def configure(params: dict, flat: dict[str, Device], instance_key: str,
              extensions_registry: dict | None = None) -> "TimerInstance":
    """Extension entry point (see phc.core.config._load_extensions): resolve
    `selectors` once against the static device tree, reject at
    configure-time (ConfigError) any selector that matches zero endpoints
    or a non-writable one -- both are almost certainly configuration
    mistakes, mirroring phc.extensions.recovery.extension.configure. Loading
    persisted timers and registering their Tasks is deferred to on_bind
    (see TimerInstance.on_bind), since `flat`/`extensions_registry` here
    don't yet include the fully-built System.tasks list a Task needs to be
    registered into -- see phc.core.config.load_system's on_bind docstring."""
    pairs = resolve_selectors(params["selectors"], flat)
    if not pairs:
        raise ConfigError(
            f"timer instance {instance_key!r}: selectors matched zero endpoints")
    non_writable = [(qid, key) for qid, key in pairs if not flat[qid].endpoint(key).writable]
    if non_writable:
        names = ", ".join(f"{qid}/{key}" for qid, key in non_writable)
        raise ConfigError(
            f"timer instance {instance_key!r}: selector(s) matched non-writable "
            f"endpoint(s) {names} -- a timer can only target a writable endpoint; "
            f"narrow the selectors or drop the read-only ones")
    catch_up = parse_duration(params["catch_up"])
    store = TimerStore(params["path"])
    return TimerInstance(store=store, target_pairs=pairs, instance_key=instance_key, catch_up=catch_up)


class TimerInstance:
    """One configured timer instance: a TimerStore plus the resolved,
    writable-only targets it may schedule against, and the live set of
    user-defined TimerDefs (keyed by id) each mirrored into a
    phc.core.task.Task. Looked up by name from phc/extensions/web_ui's
    `kind: timers` panel (see phc/extensions/web_ui/server.py)."""

    def __init__(self, store: TimerStore, target_pairs: list[tuple[str, str]],
                 instance_key: str, catch_up: float):
        self.store = store
        self.target_pairs = target_pairs
        self._target_set = set(target_pairs)
        self.instance_key = instance_key
        self.catch_up = catch_up
        self._timers: dict[int, TimerDef] = {}
        self._next_id = 1
        # Bound by on_bind(), once the System (incl. its fully-built
        # `tasks` list) exists -- see class/module docstring.
        self._flat: dict[str, Device] | None = None
        self._tasks: list | None = None
        self._extensions: dict[str, object] | None = None

    # ---------- lifecycle hooks (auto-collected, see phc.core.config.load_system) ----------

    def on_bind(self, system) -> None:
        """Capture the fully-built System, then load every persisted timer:
        a one-shot missed by more than `catch_up` is dropped (INFO log,
        never fired); everything else is registered as a Task. Runs
        synchronously at load time, before the Scheduler's first tick --
        see phc.core.config.load_system's on_bind docstring."""
        self._flat = system.devices
        self._tasks = system.tasks
        self._extensions = system.extensions

        next_id, timers = self.store.load()
        self._next_id = next_id
        now = time.time()
        kept, dropped = [], 0
        for t in timers:
            if expired_one_shot(t, now, self.catch_up):
                logger.info(
                    "timer %s: dropping expired one-shot timer %d (%r, due %.0fs ago)",
                    self.instance_key, t.id, t.description, now - t.time)
                dropped += 1
                continue
            kept.append(t)

        self._timers = {t.id: t for t in kept}
        for t in kept:
            if t.enabled:
                self._register_task(t)
        if dropped:
            self._persist()
        logger.info("%s: restored %d timer(s), dropped %d expired one-shot(s)",
                     self.instance_key, len(kept), dropped)

    def on_tick(self, devices: dict[str, Device]) -> None:
        """Reconcile the persisted timers against this tick's live task
        list: a one-shot whose Task the Scheduler already retired (see
        phc.core.scheduler.Scheduler pass 2, task.finished) is removed here
        too; a repeating timer whose Task.due_time advanced (it fired and
        rearmed) has that new time mirrored back. Persists only if
        something actually changed."""
        if self._tasks is None:
            return
        tasks_by_tag = {t.tag: t for t in self._tasks}
        changed = False
        for timer_id in list(self._timers):
            t = self._timers[timer_id]
            if not t.enabled:
                continue
            task = tasks_by_tag.get(t.tag(self.instance_key))
            if task is None:
                del self._timers[timer_id]
                changed = True
                continue
            if task.due_time is not None and task.due_time != t.time:
                t.time = task.due_time
                changed = True
        if changed:
            self._persist()

    # ---------- CRUD API (used by phc/extensions/web_ui's timers panel) ----------

    def list_timers(self) -> list[TimerDef]:
        """Every currently-held timer, soonest first."""
        return sorted(self._timers.values(), key=lambda t: t.time)

    def get_timer(self, timer_id: int) -> TimerDef:
        try:
            return self._timers[timer_id]
        except KeyError:
            raise ValueError(f"timer {timer_id}: not found") from None

    def add_timer(self, *, time_spec: str, device: str, endpoint: str, action: str,
                   value=None, repeat_spec: str | None = None, description: str = "",
                   enabled: bool = True) -> TimerDef:
        """Validate and persist a new timer, registering its Task if
        `enabled`. Raises ValueError (surfaced by the web UI as a 400) for
        a bad time/repeat/value or a target outside this instance's
        selectors."""
        t = self._build_timer(self._next_id, time_spec, device, endpoint, action,
                               value, repeat_spec, description, enabled)
        self._timers[t.id] = t
        self._next_id += 1
        if t.enabled:
            self._register_task(t)
        self._persist()
        return t

    def update_timer(self, timer_id: int, *, time_spec: str, device: str, endpoint: str,
                      action: str, value=None, repeat_spec: str | None = None,
                      description: str = "", enabled: bool = True) -> TimerDef:
        """Replace timer `timer_id`'s definition, re-registering its Task
        (register_task replaces by tag -- see phc.core.task.register_task) so
        an in-flight edit takes effect on the very next tick."""
        if timer_id not in self._timers:
            raise ValueError(f"timer {timer_id}: not found")
        t = self._build_timer(timer_id, time_spec, device, endpoint, action,
                               value, repeat_spec, description, enabled)
        kill_tasks([t.tag(self.instance_key)], self._tasks)
        self._timers[timer_id] = t
        if t.enabled:
            self._register_task(t)
        self._persist()
        return t

    def delete_timer(self, timer_id: int) -> None:
        t = self.get_timer(timer_id)
        kill_tasks([t.tag(self.instance_key)], self._tasks)
        del self._timers[timer_id]
        self._persist()

    def set_enabled(self, timer_id: int, enabled: bool) -> TimerDef:
        """Enable/disable timer `timer_id` without deleting it -- a
        disabled timer stays persisted (and keeps its own `time`), but has
        no live Task, so it never fires until re-enabled."""
        t = self.get_timer(timer_id)
        if t.enabled == enabled:
            return t
        t.enabled = enabled
        if enabled:
            self._register_task(t)
        else:
            kill_tasks([t.tag(self.instance_key)], self._tasks)
        self._persist()
        return t

    # ---------- internals ----------

    def _build_timer(self, timer_id: int, time_spec: str, device: str, endpoint: str,
                      action: str, value, repeat_spec: str | None, description: str,
                      enabled: bool) -> TimerDef:
        """Validate `device`/`endpoint` against this instance's configured
        targets, `time_spec`/`repeat_spec` against phc.core.intervals, and (for
        action "set") `value` against the target endpoint's own
        Endpoint.from_text() -- the same conversion the Task will run at
        fire time (see phc.core.device.Device.set_text), so a bad value is
        rejected immediately rather than only when the timer next fires.
        Raises ValueError on any failure."""
        if (device, endpoint) not in self._target_set:
            raise ValueError(
                f"timer: {device}.{endpoint} is not a configured timer target for "
                f"instance {self.instance_key!r}")
        due_time = parse_time(str(time_spec))
        repeat = str(repeat_spec) if repeat_spec else None
        if repeat is not None:
            parse_duration(repeat)
        if action == "set":
            self._flat[device].endpoint(endpoint).from_text(value)
        return TimerDef(id=timer_id, time=due_time, device=device, endpoint=endpoint,
                         action=action, value=value if action == "set" else None,
                         repeat=repeat, description=description, enabled=bool(enabled))

    def _register_task(self, t: TimerDef) -> None:
        register_task(_task_spec(t, self.instance_key), self._flat, self._tasks,
                       self._extensions, set(), None)

    def _persist(self) -> None:
        self.store.write(self._next_id, sorted(self._timers.values(), key=lambda t: t.id))
