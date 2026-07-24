"""random_light extension wiring: parses an extensions.random_light.<instance>'s
`lights` list against the live device tree, builds a RandomLightController
(the pure algorithm, see random_light.py), and registers the "random_light"
task action kind so a hand-authored `tasks:` entry can run its periodic
randomize pass (or force every light on/off directly) on its own schedule --
see core.config._load_extensions for how configure() is invoked, and
core.registry.discover_extensions() for how this module's
@register_task_kind decorator gets imported at startup."""

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


def _parse_window_bound(spec: str, light_ref: str) -> WindowBound:
    """Parse one window "start"/"end" value: "HH:MM" (fixed local time) or
    "sunrise"/"sunset" optionally offset by a duration string (e.g.
    "sunset+12m", "sunrise-18m" -- core.intervals.parse_duration doesn't
    accept a leading sign, so the sign is split off here before parsing
    the magnitude)."""
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
        f"random_light: light {light_ref!r}: invalid window bound {spec!r}, expected "
        f'"HH:MM" or "sunrise"/"sunset" optionally offset (e.g. "sunset+12m")')


def _resolve_optional_ref(spec, flat: dict[str, Device], instance_key: str, label: str):
    """Resolve an optional "<device>.<endpoint>" ref param (enable_ref/
    pause_ref) to (device_id, endpoint_key), or None if unset."""
    if not spec:
        return None
    device_id, endpoint_key = resolve_endpoint_ref(spec)
    if device_id not in flat:
        raise ConfigError(f"random_light instance {instance_key!r}: {label} device {device_id!r} not found")
    return device_id, endpoint_key


def configure(params: dict, flat: dict[str, Device], instance_key: str) -> "RandomLightInstance":
    """Extension entry point (see core.config._load_extensions): parse and
    validate every configured light against `flat`, plus optional
    enable_ref/pause_ref gating and the sun device (only required to exist
    if some light's window actually references it -- see
    _parse_window_bound)."""
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

        window_specs = entry.get("windows")
        if not isinstance(window_specs, list) or not window_specs:
            raise ConfigError(
                f"random_light instance {instance_key!r}: light {ref!r}: 'windows' must be a non-empty list")
        windows = []
        for window_spec in window_specs:
            start = _parse_window_bound(str(window_spec["start"]), ref)
            end = _parse_window_bound(str(window_spec["end"]), ref)
            windows.append((start, end))
            if start.kind == "sun" or end.kind == "sun":
                needs_sun = True

        min_interval = parse_duration(entry.get("min_interval", "30m"))
        probability_on = float(entry.get("probability_on", 0.5))
        if not 0.0 <= probability_on <= 1.0:
            raise ConfigError(
                f"random_light instance {instance_key!r}: light {ref!r}: probability_on must be between 0 and 1")

        lights[ref] = Light(id=ref, windows=windows, min_interval=min_interval,
                             probability_on=probability_on, is_default=bool(entry.get("default", False)))
        targets[ref] = (device_id, endpoint_key)

    sun_device_id = params.get("sun_device") or "sun"
    if needs_sun and sun_device_id not in flat:
        raise ConfigError(f"random_light instance {instance_key!r}: sun device {sun_device_id!r} not found")

    enable_ref = _resolve_optional_ref(params.get("enable_ref"), flat, instance_key, "enable_ref")
    pause_ref = _resolve_optional_ref(params.get("pause_ref"), flat, instance_key, "pause_ref")

    controller = RandomLightController(lights)
    return RandomLightInstance(controller, targets, sun_device_id=sun_device_id if needs_sun else None,
                                enable_ref=enable_ref, pause_ref=pause_ref)


class RandomLightInstance:
    """One configured random_light instance: a RandomLightController (the
    pure algorithm) plus the resolved device targets and optional gating
    refs. apply() is called by RandomLightAction.perform() (see below),
    which runs in the Scheduler's task pass (pass 2), so writes issued
    here are automatically collected/batched via core.device's
    _write_collector, exactly like any other Action -- no special
    handling needed (contrast with an on_tick tick hook, which runs in
    pass 4, outside that context; this instance deliberately has no
    on_tick)."""

    def __init__(self, controller: RandomLightController, targets: dict[str, tuple[str, str]],
                 sun_device_id: str | None, enable_ref: tuple[str, str] | None,
                 pause_ref: tuple[str, str] | None):
        self._controller = controller
        self._targets = targets
        self._sun_device_id = sun_device_id
        self._enable_ref = enable_ref
        self._pause_ref = pause_ref

    @staticmethod
    def _read(devices: dict[str, Device], ref: tuple[str, str]):
        device_id, endpoint_key = ref
        return devices[device_id].get(endpoint_key)

    def apply(self, devices: dict[str, Device], force: int | None = None) -> None:
        """force=None: run the gated periodic randomize pass -- a no-op
        (no lights touched) if pause_ref currently reads 1, or enable_ref
        is set and doesn't currently read 1. force=0/1: bypass
        pause_ref/enable_ref/windows entirely, forcing every light to that
        value -- for the surrounding system's own explicit on/off calls
        (arm, disarm, alarm) to use directly. Reads current light states
        and sunrise/sunset fresh every call (never cached); writes only
        the lights whose decided target differs from their current state."""
        if force is None:
            if self._pause_ref is not None and self._read(devices, self._pause_ref) == 1:
                return
            if self._enable_ref is not None and self._read(devices, self._enable_ref) != 1:
                return

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
    or `force: 1`) when this task fires. Has no single target device, so
    it opts out of _build_action's device resolution."""

    requires_device = False

    def __init__(self, *, instance: str, extensions: dict, force: int | None = None, **params):
        super().__init__(device_id="", endpoint_key=None, **params)
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
