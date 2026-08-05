"""random_light extension wiring: parses an extensions.random_light.<instance>'s
`lights` list against the live device tree, builds a RandomLightController
(the pure algorithm, see random_light.py), and registers the "random_light"
task action kind so a `tasks:` entry can run the periodic randomize pass
(or force every light on/off) on its own schedule -- see
core.config._load_extensions for configure()'s call site, and
core.registry.discover_extensions() for how @register_task_kind here gets
picked up at startup."""

import re
import time

from core.config import ConfigError
from core.device import Device
from core.intervals import parse_duration
from core.registry import register_task_kind
from core.task import Action, resolve_endpoint_ref
from extensions.random_light.random_light import Light, RandomLightController, WindowBound

_SUN_BOUND_RE = re.compile(r"^(sunrise|sunset)([+-].+)?$")
_FIXED_BOUND_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _parse_window_bound(spec: str, label: str) -> WindowBound:
    """Parse one window "start"/"end" value: "HH:MM" (fixed local time) or
    "sunrise"/"sunset" optionally offset by a duration string (e.g.
    "sunset+12m", "sunrise-18m" -- core.intervals.parse_duration doesn't
    accept a leading sign, so the sign is split off here before parsing
    the magnitude). `label` (e.g. "light 'a.b'" or "instance 'x.y'
    default") only flavors the error message."""
    sun_match = _SUN_BOUND_RE.match(spec)
    if sun_match:
        anchor, offset_spec = sun_match.groups()
        if not offset_spec:
            return WindowBound.sun(anchor, 0.0)
        sign, magnitude = offset_spec[0], offset_spec[1:]
        offset_seconds = parse_duration(magnitude)
        return WindowBound.sun(anchor, -offset_seconds if sign == "-" else offset_seconds)
    fixed_match = _FIXED_BOUND_RE.match(spec)
    if fixed_match:
        hour, minute = (int(g) for g in fixed_match.groups())
        return WindowBound.fixed(hour, minute)
    raise ConfigError(
        f"random_light: {label}: invalid window bound {spec!r}, expected "
        f'"HH:MM" or "sunrise"/"sunset" optionally offset (e.g. "sunset+12m")')


def _parse_windows(window_specs, label: str) -> tuple[list[tuple[WindowBound, WindowBound]], bool]:
    """Parse a `windows:` list (either a light's own, or the instance-level
    default) into resolved (start, end) WindowBound pairs, plus whether any
    bound in it is sun-anchored. `label` (e.g. "light 'a.b'" or "instance
    'x.y' default") only flavors error messages."""
    if not isinstance(window_specs, list) or not window_specs:
        raise ConfigError(f"random_light: {label}: 'windows' must be a non-empty list")
    windows = []
    needs_sun = False
    for window_spec in window_specs:
        start = _parse_window_bound(str(window_spec["start"]), label)
        end = _parse_window_bound(str(window_spec["end"]), label)
        windows.append((start, end))
        needs_sun = needs_sun or start.kind == "sun" or end.kind == "sun"
    return windows, needs_sun


def _parse_probability(value, label: str) -> float:
    probability_on = float(value)
    if not 0.0 <= probability_on <= 1.0:
        raise ConfigError(f"random_light: {label}: probability_on must be between 0 and 1")
    return probability_on


def configure(params: dict, flat: dict[str, Device], instance_key: str,
              extensions_registry: dict | None = None) -> "RandomLightInstance":
    """Extension entry point (see core.config._load_extensions and
    docs/random-light.md). `extensions_registry` is unused -- random_light
    never references another extension's instance. `windows`/`min_interval`/
    `probability_on` are declared, defaulted extension parameters
    (extension.yaml), so `params[...]` already holds the instance's value
    or extension.yaml's default by the time this runs (read directly, no
    `.get()`, matching extensions/logdb)."""
    default_label = f"instance {instance_key!r} default"
    default_windows, default_needs_sun = _parse_windows(params["windows"], default_label)
    default_min_interval = parse_duration(params["min_interval"])
    default_probability_on = _parse_probability(params["probability_on"], default_label)

    lights_spec = params.get("lights")
    if not isinstance(lights_spec, list) or not lights_spec:
        raise ConfigError(f"random_light instance {instance_key!r}: 'lights' must be a non-empty list")

    lights: dict[str, Light] = {}
    targets: dict[str, tuple[str, str]] = {}
    needs_sun = False

    for entry in lights_spec:
        ref = entry.get("device")
        if not ref:
            raise ConfigError(f"random_light instance {instance_key!r}: light entry missing 'device'")
        if ref in lights:
            raise ConfigError(f"random_light instance {instance_key!r}: duplicate light {ref!r}")
        device_id, endpoint_key = resolve_endpoint_ref(ref)
        if device_id not in flat:
            raise ConfigError(f"random_light instance {instance_key!r}: light device {device_id!r} not found")
        endpoint = flat[device_id].endpoint(endpoint_key)
        if not endpoint.writable:
            raise ConfigError(f"random_light instance {instance_key!r}: light endpoint {ref!r} is not writable")

        if "windows" in entry:
            windows, light_needs_sun = _parse_windows(entry["windows"], f"light {ref!r}")
        else:
            windows, light_needs_sun = default_windows, default_needs_sun
        needs_sun = needs_sun or light_needs_sun

        min_interval = parse_duration(entry["min_interval"]) if "min_interval" in entry else default_min_interval
        probability_on = (_parse_probability(entry["probability_on"], f"light {ref!r}")
                           if "probability_on" in entry else default_probability_on)

        lights[ref] = Light(id=ref, windows=windows, min_interval=min_interval,
                             probability_on=probability_on, is_default=bool(entry.get("default", False)))
        targets[ref] = (device_id, endpoint_key)

    sun_device_id = params.get("sun_device") or "sun"
    if needs_sun and sun_device_id not in flat:
        raise ConfigError(f"random_light instance {instance_key!r}: sun device {sun_device_id!r} not found")

    controller = RandomLightController(lights)
    return RandomLightInstance(controller, targets, sun_device_id=sun_device_id if needs_sun else None)


class RandomLightInstance:
    """One configured random_light instance: a RandomLightController (the
    pure algorithm) plus the resolved device targets. apply() is called by
    RandomLightAction.perform() (below), which runs in the Scheduler's task
    pass (pass 2) -- so writes here are automatically batched via
    core.device's _write_collector, like any other Action (unlike an
    on_tick hook, which runs in pass 4, outside that context; this instance
    has no on_tick)."""

    def __init__(self, controller: RandomLightController, targets: dict[str, tuple[str, str]],
                 sun_device_id: str | None):
        self._controller = controller
        self._targets = targets
        self._sun_device_id = sun_device_id

    @staticmethod
    def _read(devices: dict[str, Device], ref: tuple[str, str]):
        device_id, endpoint_key = ref
        return devices[device_id].get(endpoint_key)

    def apply(self, devices: dict[str, Device], force: int | None = None) -> None:
        """force=None: run the periodic randomize pass. force=0/1: bypass
        windows entirely, forcing every light to that value -- for a
        surrounding system's own on/off calls (arm, disarm, alarm). Reads
        current states and sunrise/sunset fresh each call (never cached);
        writes only lights whose target differs from their current state."""
        now = time.time()
        current_states = {light_id: self._read(devices, ref) for light_id, ref in self._targets.items()}
        sunrise = sunset = None
        if self._sun_device_id is not None:
            sun_device = devices[self._sun_device_id]
            sunrise, sunset = sun_device.get("sunrise"), sun_device.get("sunset")

        targets = self._controller.decide_all(now, current_states, sunrise, sunset, force=force)
        for light_id, target in targets.items():
            if target != current_states[light_id]:
                device_id, endpoint_key = self._targets[light_id]
                devices[device_id].set_text(target, name=endpoint_key)


@register_task_kind("random_light")
class RandomLightAction(Action):
    """Runs one random_light instance's periodic randomize pass (`force`
    omitted) or forces every configured light to a fixed value (`force: 0`
    or `force: 1`) when this task fires. Has no single target device --
    its YAML spec never has a `device:` key, so device_id/endpoint_key
    stay None (see Action)."""

    def __init__(self, *, instance: str, extensions: dict, force: int | None = None, **params):
        super().__init__(**params)
        if force not in (None, 0, 1):
            raise ConfigError(f"random_light action: force must be 0, 1, or omitted, got {force!r}")
        try:
            self._instance = extensions[instance]
        except KeyError:
            raise ConfigError(
                f"random_light action: unknown extensions instance {instance!r}; "
                f"available: {sorted(extensions)}") from None
        self._force = force

    def perform(self, devices: dict[str, Device]) -> None:
        self._instance.apply(devices, force=self._force)
