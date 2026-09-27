"""Try out a random_light configuration without running PHC.

Lights turn on/off by chance, so it's hard to tell from the YAML alone
what a config will actually do over an evening. This script plays it
forward minute by minute and prints the result: when each light was on,
and some overall statistics.

    python simulate.py --config surveillance_random_light.yaml \\
        --from 21:00 --to 22:00

    # A full system YAML: name the instance under extensions.random_light.
    python simulate.py --config examples/emulated_surveillance-system_setup.yaml \\
        --instance house --from 19:00 --to 23:00 --seed 7

--seed makes a run repeatable (same seed, same result) -- handy for
comparing two configs. --sunrise/--sunset default to a fixed 07:00/19:00;
override them with your own if the config uses sunrise/sunset-based
windows and you want a realistic time for the season.
"""

import argparse
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from phc.core.config.yamlio import _IncludeLoader
from phc.core.intervals import parse_duration
from phc.extensions.random_light.extension import _parse_probability, _parse_windows
from phc.extensions.random_light.random_light import Light, RandomLightController


def _load_lights(spec: dict, label: str) -> dict[str, Light]:
    """Build {device_ref: Light} from a random_light instance's own dict,
    using the same parsing the real extension does (see extension.py's
    configure()) -- so a config that loads for real simulates identically."""
    default_windows, _ = _parse_windows(spec.get("windows", [
        {"start": "sunset-1h", "end": "23:00"},
        {"start": "06:30", "end": "sunrise+30m"},
    ]), f"{label} default")
    default_min_interval = parse_duration(spec.get("min_interval", "15m"))
    default_probability_on = _parse_probability(spec.get("probability_on", 0.3), f"{label} default")

    lights_spec = spec.get("lights")
    if not isinstance(lights_spec, list) or not lights_spec:
        raise SystemExit(f"{label}: 'lights' must be a non-empty list")

    lights: dict[str, Light] = {}
    for entry in lights_spec:
        ref = entry.get("device")
        if not ref:
            raise SystemExit(f"{label}: light entry missing 'device'")
        if "windows" in entry:
            windows, _ = _parse_windows(entry["windows"], f"light {ref!r}")
        else:
            windows = default_windows
        min_interval = (parse_duration(entry["min_interval"]) if "min_interval" in entry
                         else default_min_interval)
        probability_on = (_parse_probability(entry["probability_on"], f"light {ref!r}")
                           if "probability_on" in entry else default_probability_on)
        lights[ref] = Light(id=ref, windows=windows, min_interval=min_interval,
                             probability_on=probability_on, is_default=bool(entry.get("default", False)))
    return lights


def _find_instance_spec(raw, instance: str | None) -> dict:
    """Resolve --config's content to one random_light instance's own dict.

    Accepts either a bare instance dict (has 'lights' directly -- the
    surveillance_random_light.yaml shape) or a full system YAML (has
    extensions.random_light.<instance>)."""
    if isinstance(raw, dict) and "lights" in raw:
        return raw
    if isinstance(raw, dict) and "extensions" in raw:
        instances = (raw.get("extensions") or {}).get("random_light") or {}
        if not instances:
            raise SystemExit("--config has no extensions.random_light instances")
        if instance is None:
            if len(instances) == 1:
                return next(iter(instances.values()))
            raise SystemExit(
                f"--config defines multiple random_light instances ({', '.join(sorted(instances))}); "
                "name one with --instance")
        if instance not in instances:
            raise SystemExit(
                f"no random_light instance {instance!r}; available: {', '.join(sorted(instances))}")
        return instances[instance]
    raise SystemExit("--config is neither a random_light instance (a 'lights:' list) "
                      "nor a system YAML with extensions.random_light")


def _parse_hhmm(spec: str, label: str) -> tuple[int, int]:
    try:
        hour_str, minute_str = spec.split(":")
        return int(hour_str), int(minute_str)
    except ValueError:
        raise SystemExit(f"{label}: expected \"HH:MM\", got {spec!r}") from None


def _short_label(ref: str, width: int) -> str:
    """Shorten a device ref for a column header, keeping the endpoint name
    intact so two lights on the same device don't end up with the same
    truncated label."""
    short = ref.split(".", 1)[1] if "." in ref else ref
    if len(short) <= width:
        return short
    if "." not in short:
        return short[:width]
    device_part, _, endpoint_part = short.rpartition(".")
    suffix = "." + endpoint_part
    keep = max(width - len(suffix), 1)
    return device_part[:keep] + suffix


def simulate(lights: dict[str, Light], sunrise: float, sunset: float,
             start: datetime, end: datetime, seed: int | None) -> dict:
    """Step decide_all() once per minute from one hour before `start`
    (warm-up, not recorded) through `end` inclusive. Returns the recorded
    rows plus house-wide and per-light statistics."""
    if seed is not None:
        random.seed(seed)
    controller = RandomLightController(lights)
    order = list(lights)

    warm_start = start - timedelta(hours=1)
    current_states: dict[str, int | None] = {ref: 0 for ref in order}
    t = warm_start
    while t < start:
        current_states = dict(controller.decide_all(t.timestamp(), current_states, sunrise, sunset))
        t += timedelta(minutes=1)

    rows: list[tuple[datetime, dict[str, int]]] = []
    on_minutes = {ref: 0 for ref in order}
    switches = {ref: 0 for ref in order}
    longest_on_run = {ref: 0 for ref in order}
    current_on_run = {ref: 0 for ref in order}
    prev_states = dict(current_states)

    no_light_minutes = 0
    longest_dark_run = 0
    longest_dark_start: datetime | None = None
    current_dark_run = 0
    current_dark_start: datetime | None = None

    t = start
    while t <= end:
        targets = controller.decide_all(t.timestamp(), current_states, sunrise, sunset)
        current_states = dict(targets)

        if any(v == 1 for v in targets.values()):
            longest_dark_run = max(longest_dark_run, current_dark_run)
            current_dark_run = 0
            current_dark_start = None
        else:
            no_light_minutes += 1
            current_dark_run += 1
            if current_dark_start is None:
                current_dark_start = t

        for ref in order:
            v = targets[ref]
            if v == 1:
                on_minutes[ref] += 1
                current_on_run[ref] += 1
            else:
                longest_on_run[ref] = max(longest_on_run[ref], current_on_run[ref])
                current_on_run[ref] = 0
            if prev_states.get(ref) != v:
                switches[ref] += 1
        prev_states = dict(targets)

        rows.append((t, dict(targets)))
        t += timedelta(minutes=1)

    longest_dark_run = max(longest_dark_run, current_dark_run)
    for ref in order:
        longest_on_run[ref] = max(longest_on_run[ref], current_on_run[ref])

    return {
        "rows": rows,
        "no_light_minutes": no_light_minutes,
        "longest_dark_run": longest_dark_run,
        "longest_dark_start": longest_dark_start,
        "on_minutes": on_minutes,
        "switches": switches,
        "longest_on_run": longest_on_run,
    }


def print_report(lights: dict[str, Light], sunrise_dt: datetime, sunset_dt: datetime,
                  start: datetime, end: datetime, result: dict, step: int) -> None:
    order = list(lights)
    total = len(result["rows"])
    labels = {ref: _short_label(ref, 18) + ("*" if lights[ref].is_default else "") for ref in order}
    col_w = max(10, max(len(v) for v in labels.values()) + 1)

    print(f"random_light simulation -- {start.strftime('%Y-%m-%d %H:%M')} to {end.strftime('%H:%M')} "
          f"(sunrise {sunrise_dt.strftime('%H:%M')}, sunset {sunset_dt.strftime('%H:%M')})")
    print("* = default: true (the always-on-house fallback light)\n")

    print(f"{'time':6s} " + " ".join(f"{labels[r]:{col_w}s}" for r in order))
    for i, (t, states) in enumerate(result["rows"]):
        if i % step:
            continue
        row = " ".join(f"{'ON' if states[r] else '-':{col_w}s}" for r in order)
        print(f"{t.strftime('%H:%M'):6s} {row}")

    dark_start = result["longest_dark_start"]
    print(f"\n--- house-wide ({start.strftime('%H:%M')}-{end.strftime('%H:%M')}, {total} samples) ---")
    print(f"Minutes with NO light on: {result['no_light_minutes']} / {total} "
          f"({result['no_light_minutes'] / total * 100:.0f}%)")
    dark_str = f", starting {dark_start.strftime('%H:%M')}" if dark_start else ""
    print(f"Longest unbroken dark stretch: {result['longest_dark_run']} min{dark_str}")

    print(f"\n--- per-light ({start.strftime('%H:%M')}-{end.strftime('%H:%M')}, {total} samples) ---")
    print(f"{'light':{col_w}s} {'on-min':>7s} {'on-%':>6s} {'switches':>9s} {'longest-on-run':>15s}")
    for ref in order:
        on = result["on_minutes"][ref]
        print(f"{labels[ref]:{col_w}s} {on:7d} {on / total * 100:5.0f}% "
              f"{result['switches'][ref]:9d} {result['longest_on_run'][ref]:15d}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, type=Path,
                         help="Path to a random_light instance YAML (a 'lights:' list) "
                              "or a full system YAML")
    parser.add_argument("--instance",
                         help="Which extensions.random_light.<instance> to simulate, "
                              "if --config is a full system YAML defining more than one")
    parser.add_argument("--from", dest="time_from", default="18:00", metavar="HH:MM",
                         help="Start of the printed window, local time (default: 18:00)")
    parser.add_argument("--to", dest="time_to", default="23:00", metavar="HH:MM",
                         help="End of the printed window, local time, inclusive (default: 23:00)")
    parser.add_argument("--date", type=lambda s: date.fromisoformat(s),
                         help="Calendar date, for the printed report only (default: today)")
    parser.add_argument("--sunrise", default="07:00", metavar="HH:MM",
                         help="Sunrise time, for sunrise-relative windows (default: 07:00)")
    parser.add_argument("--sunset", default="19:00", metavar="HH:MM",
                         help="Sunset time, for sunset-relative windows (default: 19:00)")
    parser.add_argument("--seed", type=int, default=None,
                         help="Seed random.random() for a reproducible run (default: unseeded)")
    parser.add_argument("--step", type=int, default=1, metavar="MIN",
                         help="Print every Nth simulated minute, to shorten the table "
                              "(default: 1, i.e. every minute; statistics always use every minute)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    # PHC's own include-aware loader, not yaml.safe_load: a full system
    # YAML can use !include/!placeholder, and !include resolves relative
    # to the including file's own path (read off the open file's .name),
    # so the file must be handed to yaml.load as-is, not pre-read to text.
    with open(args.config, encoding="utf-8") as f:
        raw = yaml.load(f, Loader=_IncludeLoader)
    spec = _find_instance_spec(raw, args.instance)
    lights = _load_lights(spec, f"{args.config}")

    the_date = args.date or date.today()
    sunrise_h, sunrise_m = _parse_hhmm(args.sunrise, "--sunrise")
    sunset_h, sunset_m = _parse_hhmm(args.sunset, "--sunset")
    sunrise_dt = datetime.combine(the_date, datetime.min.time()).replace(hour=sunrise_h, minute=sunrise_m)
    sunset_dt = datetime.combine(the_date, datetime.min.time()).replace(hour=sunset_h, minute=sunset_m)

    from_h, from_m = _parse_hhmm(args.time_from, "--from")
    to_h, to_m = _parse_hhmm(args.time_to, "--to")
    start = sunrise_dt.replace(hour=from_h, minute=from_m, second=0, microsecond=0)
    end = start.replace(hour=to_h, minute=to_m)
    if end < start:
        raise SystemExit("--to is before --from (overnight windows aren't supported by this script)")

    result = simulate(lights, sunrise_dt.timestamp(), sunset_dt.timestamp(), start, end, args.seed)
    print_report(lights, sunrise_dt, sunset_dt, start, end, result, max(1, args.step))
    return 0


if __name__ == "__main__":
    sys.exit(main())
