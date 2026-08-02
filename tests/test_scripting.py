"""Tests for core.scripting: the restricted-Python AST whitelist, compile/
eval/exec sandbox, and EndpointRef."""

import pytest

from core.endpoint import Endpoint
from core.scripting import (
    EndpointRef, ScriptError, compile_expression, compile_script,
    evaluate_expression, run_script,
)
from devices.virtual.device import VirtualDevice
from tests.conftest import fetch_sync


def _light(default="off"):
    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True)])
    light.set(default)
    fetch_sync(light)
    light.update_state()
    return light


# ---------- whitelist rejection ----------

@pytest.mark.parametrize("source", [
    "__import__('os')",
    "import os",
    "x.__class__",
    "().__class__",
    "_secret",
    "lambda: 1",
    "[x for x in (1, 2)]",
])
def test_compile_expression_rejects(source):
    with pytest.raises(ScriptError):
        compile_expression(source)


@pytest.mark.parametrize("source", [
    "import os",
    "def f(): pass",
    "class C: pass",
    "while True:\n    pass",
    "try:\n    pass\nexcept Exception:\n    pass",
    "with open('x') as f:\n    pass",
    "state('a.b').method()",
    "a.b = 1",
    "a, b = 1, 2",
    "for a, b in devices('*/*'):\n    pass",
    "log(*args)",
    "log(msg='hi')",
    "x[0] = 1",
    "for x[0] in y:\n    pass",
    "x[1:3]",
    "(y := 1)",
])
def test_compile_script_rejects(source):
    with pytest.raises(ScriptError):
        compile_script(source)


# ---------- whitelist acceptance ----------

def test_compile_expression_accepts_boolean_and_comparison():
    compiled = compile_expression("(1 == 1 and not False) or (2 > 3)")
    assert evaluate_expression(compiled, {}) is True


def test_compile_script_accepts_if_and_for():
    compiled = compile_script(
        "total = 0\n"
        "for name in items:\n"
        "    if name == 'a':\n"
        "        total = total + 1\n"
        "report(total)\n"
    )
    reported = []
    run_script(compiled, {"items": ["a", "b", "a"], "report": reported.append})
    assert reported == [2]


def test_compile_expression_accepts_fstring():
    compiled = compile_expression("f'{1 + 1}'")
    assert evaluate_expression(compiled, {}) == "2"


def test_run_script_calls_namespace_function():
    calls = []
    compiled = compile_script("log('hello')\nif changed('a.b'):\n    log('changed')")
    run_script(compiled, {"log": calls.append, "changed": lambda ref: True})
    assert calls == ["hello", "changed"]


# ---------- subscript ----------

@pytest.mark.parametrize("source, expected", [
    ("[1, 2, 3][1]", 2),
    ("[1, 2, 3][-1]", 3),
    ("{'a': 1}['a']", 1),
    ("[[1, 2], [3, 4]][0][1]", 2),
])
def test_compile_expression_accepts_subscript(source, expected):
    compiled = compile_expression(source)
    assert evaluate_expression(compiled, {}) == expected


def test_compile_expression_accepts_variable_index():
    compiled = compile_expression("event[2]")
    assert evaluate_expression(compiled, {"event": [1, "wrongcode", "1111"]}) == "1111"


def test_evaluate_expression_index_error_propagates_uncaught():
    compiled = compile_expression("event[5]")
    with pytest.raises(IndexError):
        evaluate_expression(compiled, {"event": [1, 2]})


# ---------- referenced_paths extraction ----------

def test_compile_expression_collects_referenced_paths_from_calls():
    compiled = compile_expression("state('house.motion1.state') == 1 and changed('house.motion2.state')")
    assert compiled.referenced_paths == {"house.motion1.state", "house.motion2.state"}


def test_compile_script_collects_referenced_paths_including_set_state():
    compiled = compile_script("set_state('living_light.state', 'on')\nreset_sticky('power.watts')")
    assert compiled.referenced_paths == {"living_light.state", "power.watts"}


def test_compile_expression_ignores_non_path_taking_calls():
    compiled = compile_expression("len('house.motion1.state') > 0")
    assert compiled.referenced_paths == frozenset()


def test_compile_expression_ignores_non_literal_call_args():
    compiled = compile_expression("state(name) == 1")
    assert compiled.referenced_paths == frozenset()


# ---------- Compiled reuse ----------

def test_compiled_can_be_evaluated_against_different_namespaces():
    compiled = compile_expression("x > 0")
    assert evaluate_expression(compiled, {"x": 1}) is True
    assert evaluate_expression(compiled, {"x": -1}) is False


# ---------- EndpointRef ----------

def test_endpoint_ref_state_and_changed():
    light = _light("off")
    fetch_sync(light)
    light.update_state()  # settle: clear the event from _light()'s initial set()
    devices = {"living_light": light}
    ref = EndpointRef("living_light", "state", devices)
    assert ref.state == "off"
    assert ref.changed is False

    light.set("on")
    fetch_sync(light)
    light.update_state()
    assert ref.state == "on"
    assert ref.changed is True


def test_endpoint_ref_text_and_event():
    light = VirtualDevice("living_light", endpoints=[
        Endpoint("state", writable=True, value_type="int", values={0: "off", 1: "on"}),
    ])
    light.set(1)
    fetch_sync(light)
    light.update_state()
    devices = {"living_light": light}
    ref = EndpointRef("living_light", "state", devices)
    assert ref.text == "on"
    assert ref.event == 1


def test_endpoint_ref_sticky_reads_log_value_for_subscriber():
    light = _light("off")
    devices = {"living_light": light}
    endpoint = light.endpoint("state")
    endpoint.subscribe_log("task:report")
    endpoint.update_log_value()
    ref = EndpointRef("living_light", "state", devices, subscriber_id="task:report")
    assert ref.sticky == "off"


def test_endpoint_ref_sticky_none_for_unsubscribed_id():
    light = _light("off")
    devices = {"living_light": light}
    ref = EndpointRef("living_light", "state", devices, subscriber_id="nobody")
    assert ref.sticky is None


# ---------- end-to-end: expr using bound EndpointRef names ----------

def test_expression_with_bound_endpoint_ref_names():
    light = _light("off")
    fetch_sync(light)
    light.update_state()  # settle: clear the event from _light()'s initial set()
    devices = {"living_light": light}
    compiled = compile_expression("living_light.state == 'off' and not living_light.changed")
    namespace = {"living_light": EndpointRef("living_light", "state", devices)}
    assert evaluate_expression(compiled, namespace) is True


# ---------- EndpointRef.history ----------

def _sensor(history=4):
    sensor = VirtualDevice("temp_sensor", endpoints=[
        Endpoint("temp", writable=True, value_type="float", history=history),
    ])
    return sensor


def _record(sensor, *values):
    for value in values:
        sensor.set(value)
        fetch_sync(sensor)
        sensor.update_state()
        sensor.endpoint("temp").record_history()


def test_endpoint_ref_history_returns_samples_oldest_first():
    sensor = _sensor()
    _record(sensor, 1.0, 2.0, 3.0)
    ref = EndpointRef("temp_sensor", "temp", {"temp_sensor": sensor})
    assert ref.history == [1.0, 2.0, 3.0]


def test_endpoint_ref_history_raises_without_declaration():
    sensor = _sensor(history=0)
    ref = EndpointRef("temp_sensor", "temp", {"temp_sensor": sensor})
    with pytest.raises(ValueError):
        ref.history


# ---------- history()/fractile()/median()/average() sandbox wiring ----------

def test_history_attribute_is_allowed():
    compiled = compile_expression("t.history")
    sensor = _sensor()
    _record(sensor, 1.0)
    namespace = {"t": EndpointRef("temp_sensor", "temp", {"temp_sensor": sensor})}
    assert evaluate_expression(compiled, namespace) == [1.0]


def test_bogus_endpoint_ref_attribute_still_rejected():
    with pytest.raises(ScriptError):
        compile_expression("t.bogus_attr")


@pytest.mark.parametrize("func", ["history", "fractile", "median", "average"])
def test_history_functions_are_path_taking(func):
    source = f"{func}('a.b', 0.5)" if func == "fractile" else f"{func}('a.b')"
    compiled = compile_expression(source)
    assert compiled.referenced_paths == {"a.b"}


# ---------- sandbox extensions: sum, is/is not ----------

def test_sum_is_available_in_the_sandbox():
    compiled = compile_expression("sum([1, 2, 3])")
    assert evaluate_expression(compiled, {}) == 6


def test_is_and_is_not_comparisons_are_allowed():
    compiled = compile_expression("x is None")
    assert evaluate_expression(compiled, {"x": None}) is True
    compiled = compile_expression("x is not None")
    assert evaluate_expression(compiled, {"x": 1}) is True
