"""Tests for phc.extensions.random_light.random_light: the pure, framework-
agnostic random light control algorithm (in isolation from the extension/
task-wiring layer)."""

from datetime import datetime

from phc.extensions.random_light.random_light import Light, RandomLightController, WindowBound


def _ts(hour: int, minute: int = 0) -> float:
    """A timestamp for today at the given local hour:minute."""
    return datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()


# ---------- WindowBound.resolve ----------

def test_window_bound_fixed_resolves_to_todays_timestamp():
    bound = WindowBound.fixed(7, 30)
    assert bound.resolve(_ts(12), None, None) == _ts(7, 30)


def test_window_bound_sun_resolves_with_zero_offset():
    bound = WindowBound.sun("sunrise", 0.0)
    assert bound.resolve(_ts(12), sunrise=_ts(6, 45), sunset=_ts(20, 0)) == _ts(6, 45)


def test_window_bound_sun_resolves_with_positive_offset():
    bound = WindowBound.sun("sunset", 720.0)  # +12m
    assert bound.resolve(_ts(12), sunrise=_ts(6, 0), sunset=_ts(20, 0)) == _ts(20, 0) + 720.0


def test_window_bound_sun_resolves_with_negative_offset():
    bound = WindowBound.sun("sunrise", -1080.0)  # -18m
    assert bound.resolve(_ts(12), sunrise=_ts(6, 30), sunset=_ts(20, 0)) == _ts(6, 30) - 1080.0


def test_window_bound_sun_returns_none_when_anchor_is_none():
    # Polar day/night: the sun device reports no sunrise/sunset today.
    bound = WindowBound.sun("sunset", 0.0)
    assert bound.resolve(_ts(12), sunrise=None, sunset=None) is None


# ---------- Light.in_window ----------

def _light(windows, min_interval=60.0, probability_on=0.5, is_default=False):
    return Light(id="light", windows=windows, min_interval=min_interval,
                 probability_on=probability_on, is_default=is_default)


def test_in_window_true_inside_single_window():
    light = _light([(WindowBound.fixed(19, 0), WindowBound.fixed(23, 0))])
    assert light.in_window(_ts(20), None, None) is True


def test_in_window_false_outside_single_window():
    light = _light([(WindowBound.fixed(19, 0), WindowBound.fixed(23, 0))])
    assert light.in_window(_ts(12), None, None) is False


def test_in_window_true_via_second_pair():
    light = _light([(WindowBound.fixed(19, 0), WindowBound.fixed(23, 0)),
                     (WindowBound.fixed(6, 0), WindowBound.fixed(8, 0))])
    assert light.in_window(_ts(7), None, None) is True


def test_in_window_false_when_bound_unresolvable():
    # A sun-anchored bound with no sunrise/sunset today -> that pair is
    # skipped, not treated as always-matching.
    light = _light([(WindowBound.sun("sunrise", 0.0), WindowBound.sun("sunset", 0.0))])
    assert light.in_window(_ts(12), sunrise=None, sunset=None) is False


def test_in_window_false_when_end_before_start_no_wraparound():
    light = _light([(WindowBound.fixed(22, 0), WindowBound.fixed(2, 0))])
    assert light.in_window(_ts(23), None, None) is False
    assert light.in_window(_ts(1), None, None) is False


# ---------- RandomLightController.decide_single ----------

def test_decide_single_probability_zero_always_off_in_window():
    light = _light([(WindowBound.fixed(0, 0), WindowBound.fixed(23, 59))], probability_on=0.0)
    ctrl = RandomLightController({"light": light}, random_func=lambda: 0.9)
    assert ctrl.decide_single("light", _ts(12), current_state=1, sunrise=None, sunset=None) == 0


def test_decide_single_probability_one_always_on_in_window():
    light = _light([(WindowBound.fixed(0, 0), WindowBound.fixed(23, 59))], probability_on=1.0)
    ctrl = RandomLightController({"light": light}, random_func=lambda: 0.1)
    assert ctrl.decide_single("light", _ts(12), current_state=0, sunrise=None, sunset=None) == 1


def test_decide_single_force_bypasses_probability_inside_window():
    light = _light([(WindowBound.fixed(0, 0), WindowBound.fixed(23, 59))], probability_on=0.5)
    ctrl = RandomLightController({"light": light})
    assert ctrl.decide_single("light", _ts(12), current_state=0, sunrise=None, sunset=None, force=1) == 1
    assert ctrl.decide_single("light", _ts(12), current_state=1, sunrise=None, sunset=None, force=0) == 0


def test_decide_single_outside_window_always_off_and_clears_schedule():
    light = _light([(WindowBound.fixed(19, 0), WindowBound.fixed(23, 0))])
    ctrl = RandomLightController({"light": light}, random_func=lambda: 0.5)
    ctrl.decide_single("light", _ts(20), current_state=0, sunrise=None, sunset=None)
    assert "light" in ctrl._next_switch_time
    assert ctrl.decide_single("light", _ts(12), current_state=1, sunrise=None, sunset=None) == 0
    assert "light" not in ctrl._next_switch_time


def test_decide_single_none_current_state_treated_as_off():
    light = _light([(WindowBound.fixed(0, 0), WindowBound.fixed(23, 59))], probability_on=1.0)
    ctrl = RandomLightController({"light": light})
    assert ctrl.decide_single("light", _ts(12), current_state=None, sunrise=None, sunset=None) == 1


def test_decide_single_first_switch_computes_next_switch_time_without_3600_factor():
    light = _light([(WindowBound.fixed(0, 0), WindowBound.fixed(23, 59))],
                    min_interval=100.0, probability_on=0.5)
    ctrl = RandomLightController({"light": light}, random_func=lambda: 0.5)
    now = _ts(12)
    target = ctrl.decide_single("light", now, current_state=0, sunrise=None, sunset=None)
    assert target == 1  # was off -> turning on
    p_component = 0.5  # probability_on, since turning on
    jitter = 0.3 + 1.4 * 0.5
    expected_next = now + p_component * jitter * 100.0
    assert ctrl._next_switch_time["light"] == expected_next


def test_decide_single_not_due_returns_current_state_unchanged():
    light = _light([(WindowBound.fixed(0, 0), WindowBound.fixed(23, 59))],
                    min_interval=1000.0, probability_on=0.5)
    ctrl = RandomLightController({"light": light}, random_func=lambda: 0.5)
    now = _ts(12)
    ctrl.decide_single("light", now, current_state=0, sunrise=None, sunset=None)  # schedules a future switch
    # Called again immediately, current_state now reflects the light having
    # actually been switched on by the caller after the first decision.
    assert ctrl.decide_single("light", now, current_state=1, sunrise=None, sunset=None) == 1


def test_decide_single_due_refires_after_elapsed():
    light = _light([(WindowBound.fixed(0, 0), WindowBound.fixed(23, 59))],
                    min_interval=10.0, probability_on=0.5)
    ctrl = RandomLightController({"light": light}, random_func=lambda: 0.0)
    now = _ts(12)
    first = ctrl.decide_single("light", now, current_state=0, sunrise=None, sunset=None)
    assert first == 1
    later = now + 10000.0  # well past any possible next_switch_time
    second = ctrl.decide_single("light", later, current_state=1, sunrise=None, sunset=None)
    assert second == 0  # was on -> turning off


# ---------- RandomLightController.decide_all ----------

def test_decide_all_no_fallback_when_something_already_on():
    a = _light([(WindowBound.fixed(0, 0), WindowBound.fixed(23, 59))], probability_on=1.0, is_default=True)
    b = _light([(WindowBound.fixed(0, 0), WindowBound.fixed(23, 59))], probability_on=0.0)
    ctrl = RandomLightController({"a": a, "b": b}, random_func=lambda: 0.5)
    targets = ctrl.decide_all(_ts(12), {"a": 0, "b": 0}, None, None)
    assert targets == {"a": 1, "b": 0}


def test_decide_all_fallback_picks_default_light_when_nothing_on():
    a = _light([(WindowBound.fixed(0, 0), WindowBound.fixed(23, 59))], probability_on=0.0, is_default=True)
    b = _light([(WindowBound.fixed(0, 0), WindowBound.fixed(23, 59))], probability_on=0.0)
    ctrl = RandomLightController({"a": a, "b": b}, random_func=lambda: 0.0)
    targets = ctrl.decide_all(_ts(12), {"a": 0, "b": 0}, None, None)
    assert targets == {"a": 1, "b": 0}


def test_decide_all_fallback_respects_chosen_lights_own_window():
    # "a" is default-flagged but currently OUTSIDE its window -- the
    # fallback must still leave it off, matching the Tcl original.
    a = _light([(WindowBound.fixed(19, 0), WindowBound.fixed(23, 0))], probability_on=0.0, is_default=True)
    b = _light([(WindowBound.fixed(0, 0), WindowBound.fixed(23, 59))], probability_on=0.0)
    ctrl = RandomLightController({"a": a, "b": b}, random_func=lambda: 0.0)
    targets = ctrl.decide_all(_ts(12), {"a": 0, "b": 0}, None, None)  # noon: "a" is out of window
    assert targets == {"a": 0, "b": 0}


def test_decide_all_no_default_lights_no_crash_stays_all_off():
    a = _light([(WindowBound.fixed(0, 0), WindowBound.fixed(23, 59))], probability_on=0.0)
    ctrl = RandomLightController({"a": a}, random_func=lambda: 0.0)
    assert ctrl.decide_all(_ts(12), {"a": 0}, None, None) == {"a": 0}


def test_decide_all_force_bypasses_everything_including_windows():
    a = _light([(WindowBound.fixed(19, 0), WindowBound.fixed(23, 0))])  # out of window at noon
    b = _light([(WindowBound.fixed(19, 0), WindowBound.fixed(23, 0))])
    ctrl = RandomLightController({"a": a, "b": b})
    assert ctrl.decide_all(_ts(12), {"a": 0, "b": 1}, None, None, force=1) == {"a": 1, "b": 1}
    assert ctrl.decide_all(_ts(12), {"a": 1, "b": 1}, None, None, force=0) == {"a": 0, "b": 0}
