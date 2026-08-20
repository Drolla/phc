"""Random light control: the pure, framework-agnostic decision algorithm.
Decoupled from phc.core.device/phc.core.task -- callers pass plain
{light_id: 0|1|None} state dicts and sunrise/sunset timestamps, and get
back a plain {light_id: 0|1} target dict. See phc/extensions/random_light/
extension.py for the phc.core.config/phc.core.task wiring."""

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class WindowBound:
    """One edge of a light's on/off window: either a fixed local
    time-of-day ("HH:MM") or a sun-relative anchor ("sunrise"/"sunset")
    plus/minus an offset. Built via the fixed()/sun() constructors;
    resolve() turns it into a concrete Unix timestamp for "today"."""

    kind: str  # "fixed" | "sun"
    hour: int | None = None
    minute: int | None = None
    anchor: str | None = None  # "sunrise" | "sunset", when kind == "sun"
    offset_seconds: float = 0.0

    @classmethod
    def fixed(cls, hour: int, minute: int) -> "WindowBound":
        return cls(kind="fixed", hour=hour, minute=minute)

    @classmethod
    def sun(cls, anchor: str, offset_seconds: float = 0.0) -> "WindowBound":
        return cls(kind="sun", anchor=anchor, offset_seconds=offset_seconds)

    def resolve(self, now: float, sunrise: float | None, sunset: float | None) -> float | None:
        """This bound's timestamp for `now`'s local calendar day, or None
        if it can't be resolved today (kind="sun" and the matching
        sunrise/sunset value is currently None -- polar day/night)."""
        if self.kind == "fixed":
            today = datetime.fromtimestamp(now).replace(
                hour=self.hour, minute=self.minute, second=0, microsecond=0)
            return today.timestamp()
        anchor_value = sunrise if self.anchor == "sunrise" else sunset
        if anchor_value is None:
            return None
        return anchor_value + self.offset_seconds


@dataclass(frozen=True)
class Light:
    """One configured light's static settings (see RandomLightController
    for the mutable per-light "next switch time" state)."""

    id: str
    windows: list[tuple[WindowBound, WindowBound]]
    min_interval: float  # seconds
    probability_on: float
    is_default: bool = False

    def in_window(self, now: float, sunrise: float | None, sunset: float | None) -> bool:
        """True if `now` falls inside any of this light's [start, end]
        window pairs. A pair that can't be resolved today, or whose
        resolved end is before its start (would need midnight wraparound,
        not supported -- express an overnight window as two same-day
        pairs instead), is simply skipped rather than failing the whole
        light."""
        for start, end in self.windows:
            start_ts = start.resolve(now, sunrise, sunset)
            end_ts = end.resolve(now, sunrise, sunset)
            if start_ts is None or end_ts is None or end_ts < start_ts:
                continue
            if start_ts <= now <= end_ts:
                return True
        return False


class RandomLightController:
    """Owns every configured light's static Light config plus the mutable
    per-light "next switch time" bookkeeping, for one extension instance."""

    def __init__(self, lights: dict[str, Light], random_func: Callable[[], float] = random.random):
        self._lights = lights
        self._random_func = random_func
        self._next_switch_time: dict[str, float] = {}

    def decide_single(self, light_id: str, now: float, current_state: int | None,
                       sunrise: float | None, sunset: float | None,
                       force: int | None = None) -> int:
        """Decide one light's target state. `force` here is the internal,
        window-respecting override used only by decide_all()'s
        default-light fallback (a light forced on this way can still end
        up off if it's outside its own window) -- NOT the same as
        decide_all()'s own top-level `force`, which bypasses windows
        entirely. `current_state=None` (never observed) is treated as
        "off" for branch selection. A forced call never touches
        next_switch_time -- the schedule it left behind still applies
        once the light is no longer forced."""
        light = self._lights[light_id]
        if not light.in_window(now, sunrise, sunset):
            self._next_switch_time.pop(light_id, None)
            return 0
        if force is not None:
            return force
        if light.probability_on in (0.0, 1.0):
            return int(round(light.probability_on))
        due = light_id not in self._next_switch_time or now > self._next_switch_time[light_id]
        if not due:
            return 0 if current_state is None else current_state
        turning_on = current_state != 1
        p_component = light.probability_on if turning_on else (1.0 - light.probability_on)
        jitter = 0.3 + 1.4 * self._random_func()
        self._next_switch_time[light_id] = now + p_component * jitter * light.min_interval
        return 1 if turning_on else 0

    def decide_all(self, now: float, current_states: dict[str, int | None],
                    sunrise: float | None, sunset: float | None,
                    force: int | None = None) -> dict[str, int]:
        """Decide every configured light's target state for this call.
        `force` (0 or 1) bypasses everything -- windows, probability,
        min_interval -- forcing every light to that value directly.
        Otherwise runs the normal per-light decision, then -- only if no
        light ended up on -- picks one random `is_default`-flagged light
        and re-decides it with force=1 (still window-respecting: it can
        still end up off)."""
        if force is not None:
            return {light_id: force for light_id in self._lights}
        targets = {light_id: self.decide_single(light_id, now, current_states.get(light_id),
                                                  sunrise, sunset)
                   for light_id in self._lights}
        if not any(target == 1 for target in targets.values()):
            defaults = [light_id for light_id, light in self._lights.items() if light.is_default]
            if defaults:
                chosen = defaults[int(self._random_func() * len(defaults)) % len(defaults)]
                targets[chosen] = self.decide_single(chosen, now, current_states.get(chosen),
                                                       sunrise, sunset, force=1)
        return targets
