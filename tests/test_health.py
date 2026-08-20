"""Device health and endpoint staleness.

A fetch failure is logged and swallowed so one flaky device cannot stall
the tick or take down its siblings. That made a dead device invisible:
its endpoints simply stopped changing, which from the outside looks
exactly like a sensor whose reading is genuinely steady. These tests pin
the distinction the health record exists to make.
"""

import logging
import time

import pytest

from phc.core.device import Device
from phc.core.endpoint import Endpoint
from phc.core.health import DeviceHealth
from phc.core.scheduler import Scheduler
from phc.core.task import _build_rule_namespace
from tests.conftest import fetch_sync


class FlakyDevice(Device):
    """Raises from receive() while `failing` is set."""

    def setup(self):
        self.failing = False

    def receive(self) -> dict:
        if self.failing:
            raise RuntimeError("controller unreachable")
        return {"value": 42}


class SilentlyFailingDevice(Device):
    """Catches its own I/O error and reports None, as the weather and zway
    modules do -- so the fetch itself never fails."""

    def setup(self):
        self.failing = False

    def receive(self) -> dict:
        return {"value": None if self.failing else 42}


def _device(cls=FlakyDevice, **kwargs):
    return cls("sensor", endpoints=[Endpoint("value")], update_interval=0.0, **kwargs)


@pytest.fixture
def health_log(caplog):
    """caplog attached directly to "phc.health".

    Propagation is turned off for the duration: whether this logger
    propagates depends on whether some earlier test in the session
    happened to run load_system() (which calls configure_logging(), and
    sets propagate=False on the phc tree). Without pinning it, a record
    would be captured once here and once again via root, and these tests
    count records."""
    logger = logging.getLogger("phc.health")
    previous_propagate, previous_level = logger.propagate, logger.level
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)
        logger.propagate, logger.level = previous_propagate, previous_level


# ---------- the record itself ----------

def test_health_counts_consecutive_failures_not_a_running_total():
    """"Is this device working now" is the useful question; a device that
    failed twice last week and has been fine since is healthy."""
    health = DeviceHealth()
    health.record_failure("boom")
    health.record_failure("boom")
    assert health.consecutive_failures == 2
    assert not health.healthy

    health.record_success()
    assert health.healthy
    assert health.consecutive_failures == 0
    assert health.total_failures == 2, "the total is still available for reporting"


def test_health_reports_only_the_transitions():
    """So a caller can log a state change rather than repeating itself on
    every tick for a device that has been down for hours."""
    health = DeviceHealth()
    assert health.record_failure("boom") is True     # healthy -> failing
    assert health.record_failure("boom") is False    # still failing
    assert health.record_success() is True           # failing -> healthy
    assert health.record_success() is False          # still healthy


def test_a_never_polled_device_is_healthy():
    """A device with no update: interval has not failed; reporting it as
    unhealthy forever would be actively misleading."""
    assert Device("d").health.healthy


# ---------- scheduler integration ----------

def test_scheduler_records_a_raising_fetch_as_a_failure():
    device = _device()
    device.failing = True
    scheduler = Scheduler({"sensor": device})

    scheduler.tick(now=0.0)

    assert not device.health.healthy
    assert device.health.consecutive_failures == 1
    assert "controller unreachable" in device.health.last_error
    scheduler.close()


def test_scheduler_records_recovery():
    device = _device()
    scheduler = Scheduler({"sensor": device})

    device.failing = True
    scheduler.tick(now=0.0)
    scheduler.tick(now=1.0)
    assert device.health.consecutive_failures == 2

    device.failing = False
    scheduler.tick(now=2.0)
    assert device.health.healthy
    assert device.get("value") == 42
    scheduler.close()


def test_one_devices_failure_does_not_mark_its_siblings_unhealthy():
    good, bad = _device(), _device()
    bad.failing = True
    scheduler = Scheduler({"good": good, "bad": bad})

    scheduler.tick(now=0.0)

    assert good.health.healthy
    assert not bad.health.healthy
    scheduler.close()


def test_failure_is_logged_on_transition_then_quietly(health_log):
    """A device unreachable for hours would otherwise repeat the same
    traceback every tick and bury everything else -- but dropping the
    repeats entirely would leave no evidence it is still failing."""
    device = _device()
    device.failing = True
    scheduler = Scheduler({"sensor": device})

    scheduler.tick(now=0.0)
    warnings = [r for r in health_log.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "failed" in warnings[0].getMessage()

    scheduler.tick(now=1.0)
    scheduler.tick(now=2.0)
    warnings = [r for r in health_log.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "continued failure must not repeat the warning"
    debugs = [r for r in health_log.records if r.levelno == logging.DEBUG]
    assert any("still failing" in r.getMessage() for r in debugs), "but must leave evidence"

    device.failing = False
    scheduler.tick(now=3.0)
    assert any("recovered" in r.getMessage()
               for r in health_log.records if r.levelno == logging.WARNING)
    scheduler.close()


# ---------- endpoint staleness ----------

def test_last_read_time_tracks_reads_not_changes():
    """The distinction the whole feature turns on: update_time moves only
    when the value CHANGES, so a sensor reporting a steady 20.0 for an
    hour is indistinguishable from one that stopped answering an hour
    ago."""
    device = _device()
    endpoint = device.endpoint("value")

    fetch_sync(device)
    device.update_state()
    first_update = endpoint.get_update_time()
    first_read = endpoint.get_last_read_time()
    assert first_read is not None

    time.sleep(0.01)
    fetch_sync(device)            # same value 42 again -- no change
    device.update_state()

    assert endpoint.get_update_time() == first_update, "unchanged value: update_time frozen"
    assert endpoint.get_last_read_time() > first_read, "but it WAS read again"


def test_a_none_reading_does_not_refresh_the_read_stamp():
    """A device that catches its own error and reports None must not look
    freshly read -- that is precisely the silent-failure case."""
    device = _device(SilentlyFailingDevice)
    endpoint = device.endpoint("value")

    fetch_sync(device)
    device.update_state()
    read_at = endpoint.get_last_read_time()

    device.failing = True
    time.sleep(0.01)
    fetch_sync(device)
    device.update_state()

    assert endpoint.get_last_read_time() == read_at
    assert endpoint.get_age() >= 0.01


def test_age_is_none_before_the_first_reading():
    assert Endpoint("value").get_age() is None


# ---------- the scripting surface ----------

def _namespace(devices):
    return _build_rule_namespace(devices=devices, flat=devices, tasks=None,
                                  task_tag="t", writable=False)


def test_available_is_false_while_the_device_is_failing():
    device = _device()
    devices = {"sensor": device}
    scheduler = Scheduler(devices)
    scheduler.tick(now=0.0)
    assert _namespace(devices)["available"]("sensor.value") is True

    device.failing = True
    scheduler.tick(now=1.0)
    assert _namespace(devices)["available"]("sensor.value") is False, \
        "a stale last-good value on a device that stopped answering looks live"
    scheduler.close()


def test_available_is_false_before_anything_has_been_read():
    device = _device()
    assert _namespace({"sensor": device})["available"]("sensor.value") is False


def test_age_is_available_to_a_condition():
    device = _device()
    devices = {"sensor": device}
    fetch_sync(device)
    device.update_state()
    age = _namespace(devices)["age"]("sensor.value")
    assert age is not None and age >= 0.0


def test_available_and_age_work_as_expressions():
    """They must survive the sandbox whitelist, both as calls and via the
    refs: attribute form."""
    from phc.core import scripting

    device = _device()
    devices = {"sensor": device}
    fetch_sync(device)
    device.update_state()

    namespace = _namespace(devices)
    compiled = scripting.compile_expression(
        'available("sensor.value") and age("sensor.value") < 3600')
    assert scripting.evaluate_expression(compiled, namespace) is True
    # and the paths are harvested for validation/subscription like any other
    assert "sensor.value" in compiled.referenced_paths

    namespace["s"] = scripting.EndpointRef("sensor", "value", devices, "t")
    compiled = scripting.compile_expression("s.available and s.age >= 0")
    assert scripting.evaluate_expression(compiled, namespace) is True
