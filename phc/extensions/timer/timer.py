"""Timer data model and persistence: pure definitions and file I/O for
user-programmable timers, decoupled from phc.core.device/phc.core.task -- a
TimerDef is a plain record of what to do and when. phc/extensions/timer/
extension.py turns a set of these into live phc.core.task.Task objects (via
phc.core.task.register_task) and exposes the CRUD API a web UI panel drives.

TimerStore mirrors phc.extensions.recovery.recovery.RecoveryStore's atomic
whole-file YAML rewrite (temp file + os.replace, tolerate a missing/corrupt
file on load) -- duplicated rather than imported, since phc.extensions.timer
must not depend on phc.extensions.recovery being configured, and the payload
here is a list of records (each with its own id/repeat/enabled), not a flat
value map."""

import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

logger = logging.getLogger("phc.timer")

_ACTIONS = ("set", "toggle")


@dataclass
class TimerDef:
    """One user-defined timer: fire `action` against `device`/`endpoint` at
    `time` (a Unix timestamp -- the next/last-computed trigger), optionally
    repeating every `repeat` (a phc.core.intervals.parse_duration string, e.g.
    "1D"/"1h30m"; None means one-shot). `value` is only meaningful for
    action "set" (the raw or display-text value to write, e.g. 1 or "on").
    `enabled=False` keeps the definition persisted but not scheduled -- see
    extension.py's TimerInstance."""

    id: int
    time: float
    device: str
    endpoint: str
    action: str
    value: object | None
    repeat: str | None
    description: str
    enabled: bool = True

    def __post_init__(self):
        if self.action not in _ACTIONS:
            raise ValueError(f"timer: invalid action {self.action!r}, expected one of {_ACTIONS}")
        if self.action == "set" and self.value is None:
            raise ValueError("timer: action 'set' requires a value")

    def tag(self, instance_key: str) -> str:
        """This timer's phc.core.task.Task tag, e.g. "timer.house.4" -- unique
        within one timer instance, stable across edits (only deleting and
        re-adding changes it), used to replace/kill its Task by tag (see
        phc.core.task.register_task/kill_tasks)."""
        return f"{instance_key}.{self.id}"


def expired_one_shot(timer: TimerDef, now: float, catch_up: float) -> bool:
    """True if `timer` is a one-shot (repeat is None) whose trigger time is
    more than `catch_up` seconds in the past relative to `now` -- i.e. it
    was missed by more than the configured grace window and should be
    dropped at startup rather than fired immediately. A repeating timer is
    never "expired" this way: phc.core.intervals.parse_time's own repeat
    catch-up logic (invoked when the timer's Task is built) rolls its
    due_time forward to the next future slot instead of ever asking this
    question."""
    return timer.repeat is None and (now - timer.time) > catch_up


class TimerStore:
    """A YAML file at `path` holding {"next_id": int, "timers": [...]}."""

    def __init__(self, path):
        self.path = Path(path)

    def write(self, next_id: int, timers: list[TimerDef]) -> None:
        """Atomically rewrite the whole file (see
        phc.extensions.recovery.recovery.RecoveryStore.write for the same
        temp-file + os.replace() pattern and its crash-safety rationale)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"next_id": next_id, "timers": [asdict(t) for t in timers]}
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        os.replace(tmp_path, self.path)

    def load(self) -> tuple[int, list[TimerDef]]:
        """Read back (next_id, timers). Returns (1, []) if the file doesn't
        exist yet, is empty, or can't be parsed as a YAML mapping with the
        expected shape -- logged as a warning, never raised, mirroring
        RecoveryStore.load()'s tolerate-and-start-fresh behaviour. Any
        single malformed timer entry is skipped (logged) rather than
        aborting the whole load."""
        if not self.path.exists():
            return 1, []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("timer: %s is missing or corrupt (%s); starting with no timers",
                            self.path, exc)
            return 1, []
        if not isinstance(data, dict):
            if data is not None:
                logger.warning("timer: %s does not contain a YAML mapping; ignoring", self.path)
            return 1, []

        next_id = data.get("next_id", 1)
        timers = []
        for entry in data.get("timers") or []:
            try:
                timers.append(TimerDef(**entry))
            except (TypeError, ValueError) as exc:
                logger.warning("timer: %s: skipping malformed timer entry %r (%s)",
                                self.path, entry, exc)
        return next_id, timers
