"""Tests for extensions.logdb.extension: sticky-value subscription/read/
invalidate wiring, configure(), and LogDbAction, plus an end-to-end test
through core.config.load_system(). Selector pattern resolution itself is
tested in tests/test_selectors.py (core.selectors.resolve_selectors)."""

from pathlib import Path

import pytest

from core.config import ConfigError, load_system
from core.endpoint import Endpoint
from core.registry import discover_extensions
from core.scheduler import Scheduler
from devices.virtual.device import VirtualDevice
from extensions.logdb.extension import LogDbAction, configure
from tests.conftest import fetch_sync


def _commit(device, endpoint_key, value):
    device.set(value, name=endpoint_key)
    fetch_sync(device)
    device.update_state()


def _house(**endpoint_kwargs):
    lamp = VirtualDevice("desk_lamp", endpoints=[
        Endpoint("power", writable=True, value_type="int", **endpoint_kwargs),
        Endpoint("brightness", writable=True, value_type="int"),
    ], parent_qualified_id="house")
    house = VirtualDevice("house", children=[lamp])
    light = VirtualDevice("living_light", endpoints=[Endpoint("state", writable=True, value_type="int")])
    flat = {"house": house, "house.desk_lamp": lamp, "living_light": light}
    return flat


# ---------- configure() ----------

def test_configure_subscribes_resolved_endpoints_and_builds_store(tmp_path):
    flat = _house()
    params = {
        "selectors": ["house.desk_lamp/*"],
        "csv_path": str(tmp_path / "log.csv"),
        "full_vector_interval": 100,
        "max_records": None,
        "max_age": None,
        "header_reserve_bytes": None,
    }
    instance = configure(params, flat, "logdb.house_log")

    assert instance.pairs == [("house.desk_lamp", "brightness"), ("house.desk_lamp", "power")]
    lamp = flat["house.desk_lamp"]
    assert lamp.endpoint("power").get_log_value("logdb.house_log") is None
    _commit(lamp, "power", 1)
    instance.on_tick({"house.desk_lamp": lamp})
    assert lamp.endpoint("power").get_log_value("logdb.house_log") == 1
    instance.store.close()


# ---------- LogDbInstance.on_tick / sample ----------

def test_on_tick_advances_sticky_value_per_log_aggregation(tmp_path):
    flat = _house()
    params = {
        "selectors": ["house.desk_lamp/power"],
        "csv_path": str(tmp_path / "log.csv"),
        "full_vector_interval": 100,
        "max_records": None,
        "max_age": None,
        "header_reserve_bytes": None,
    }
    instance = configure(params, flat, "logdb.house_log")
    lamp = flat["house.desk_lamp"]

    _commit(lamp, "power", 3)
    instance.on_tick(flat)
    _commit(lamp, "power", 7)
    instance.on_tick(flat)
    _commit(lamp, "power", 2)
    instance.on_tick(flat)

    assert lamp.endpoint("power").get_log_value("logdb.house_log") == 7
    instance.store.close()


def test_sample_reads_sticky_value_and_invalidates(tmp_path):
    flat = _house()
    params = {
        "selectors": ["house.desk_lamp/power"],
        "csv_path": str(tmp_path / "log.csv"),
        "full_vector_interval": 100,
        "max_records": None,
        "max_age": None,
        "header_reserve_bytes": None,
    }
    instance = configure(params, flat, "logdb.house_log")
    lamp = flat["house.desk_lamp"]

    _commit(lamp, "power", 3)
    instance.on_tick(flat)
    _commit(lamp, "power", 9)
    instance.on_tick(flat)

    instance.sample(flat)
    assert len(instance.store) == 1
    row = instance.store.get_range(0, float("inf"))[0]
    assert row["house.desk_lamp/power"] == 9.0
    assert lamp.endpoint("power").get_log_value("logdb.house_log") is None
    instance.store.close()


def test_sample_reports_no_data_when_sticky_value_still_none(tmp_path):
    flat = _house()
    params = {
        "selectors": ["house.desk_lamp/power"],
        "csv_path": str(tmp_path / "log.csv"),
        "full_vector_interval": 100,
        "max_records": None,
        "max_age": None,
        "header_reserve_bytes": None,
    }
    instance = configure(params, flat, "logdb.house_log")

    instance.sample(flat)  # no on_tick() ever ran -- sticky value is still None
    row = instance.store.get_range(0, float("inf"))[0]
    import math
    assert math.isnan(row["house.desk_lamp/power"])
    instance.store.close()


def test_sample_converts_bool_to_zero_one(tmp_path):
    flat = {"living_light": VirtualDevice("living_light", endpoints=[
        Endpoint("state", writable=True, value_type="bool"),
    ])}
    params = {
        "selectors": ["living_light/state"],
        "csv_path": str(tmp_path / "log.csv"),
        "full_vector_interval": 100,
        "max_records": None,
        "max_age": None,
        "header_reserve_bytes": None,
    }
    instance = configure(params, flat, "logdb.house_log")
    light = flat["living_light"]

    _commit(light, "state", True)
    instance.on_tick(flat)
    instance.sample(flat)

    row = instance.store.get_range(0, float("inf"))[0]
    assert row["living_light/state"] == 1.0
    instance.store.close()


def test_two_instances_sampling_same_endpoint_track_independent_sticky_values(tmp_path):
    flat = _house()
    lamp = flat["house.desk_lamp"]
    fast_params = {
        "selectors": ["house.desk_lamp/power"], "csv_path": str(tmp_path / "fast.csv"),
        "full_vector_interval": 100, "max_records": None, "max_age": None, "header_reserve_bytes": None,
    }
    slow_params = {
        "selectors": ["house.desk_lamp/power"], "csv_path": str(tmp_path / "slow.csv"),
        "full_vector_interval": 100, "max_records": None, "max_age": None, "header_reserve_bytes": None,
    }
    fast = configure(fast_params, flat, "logdb.fast")
    slow = configure(slow_params, flat, "logdb.slow")

    _commit(lamp, "power", 3)
    fast.on_tick(flat)
    slow.on_tick(flat)
    fast.sample(flat)  # fast_logger samples every tick, invalidating its own sticky value

    _commit(lamp, "power", 9)
    fast.on_tick(flat)
    slow.on_tick(flat)
    fast.sample(flat)

    slow.sample(flat)
    slow_row = slow.store.get_range(0, float("inf"))[0]
    assert slow_row["house.desk_lamp/power"] == 9.0  # running max across both ticks

    fast.store.close()
    slow.store.close()


# ---------- LogDbAction ----------

def test_log_db_action_delegates_to_instance_sample(tmp_path):
    flat = _house()
    params = {
        "selectors": ["house.desk_lamp/power"],
        "csv_path": str(tmp_path / "log.csv"),
        "full_vector_interval": 100,
        "max_records": None,
        "max_age": None,
        "header_reserve_bytes": None,
    }
    instance = configure(params, flat, "logdb.house_log")
    action = LogDbAction(instance="logdb.house_log", extensions={"logdb.house_log": instance})

    lamp = flat["house.desk_lamp"]
    _commit(lamp, "power", 5)
    instance.on_tick(flat)
    action.perform(flat)

    assert len(instance.store) == 1
    instance.store.close()


def test_log_db_action_unknown_instance_raises_config_error():
    with pytest.raises(ConfigError):
        LogDbAction(instance="logdb.nonexistent", extensions={})


def test_log_db_action_requires_device_is_false():
    assert LogDbAction.requires_device is False


# ---------- end-to-end via load_system() ----------

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples"


def test_end_to_end_load_system_logs_sticky_max_between_samples(tmp_path):
    discover_extensions()
    csv_path = tmp_path / "house_log.csv"
    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text(f"""
heartbeat: 1s
devices:
  - id: alarm
    module: virtual
    update: 1s
    endpoints:
      - key: state
        writable: true
        type: int
        default: 0

extensions:
  logdb:
    house_log:
      selectors: ["alarm/state"]
      csv_path: "{csv_path.as_posix()}"
      max_records: 1000

tasks:
  - tag: log_house
    time: "+1s"
    repeat: 30s
    action: {{ kind: log_db, instance: "logdb.house_log" }}
""")
    system = load_system(system_yaml)
    # The task's "time: +1s"/"repeat: 30s" are wall-clock-relative (see
    # core.intervals.parse_time), which would make this test's small fake
    # tick(now=...) values wall-clock-dependent -- pin due_time directly to
    # decouple task scheduling from real time for this test.
    task = next(t for t in system.tasks if t.tag == "log_house")
    task.due_time = 30.0
    task.repeat = 0.0
    scheduler = Scheduler(system.devices, tasks=system.tasks, heartbeat=system.heartbeat,
                          tick_hooks=system.tick_hooks)

    alarm = system.devices["alarm"]

    scheduler.tick(now=0.0)  # tick 0: state 0, on_tick seeds sticky = 0

    alarm.set(1, name="state")  # a brief alarm pulse between two log samples
    scheduler.tick(now=1.0)     # tick 1: fetch commits state=1, on_tick advances sticky max -> 1

    alarm.set(0, name="state")
    scheduler.tick(now=2.0)     # tick 2: state reverts to 0, sticky max stays 1

    scheduler.tick(now=31.0)    # log_house task fires (due at +1s, repeat 30s): samples sticky max

    rows = [r for r in csv_path.read_text().splitlines() if r and not r.startswith(("phc-logdb", "type,"))]
    assert len(rows) == 1
    logged_row = rows[0]
    assert logged_row.split(",")[-1] == "1"  # sticky max captured the pulse, not the reverted value
