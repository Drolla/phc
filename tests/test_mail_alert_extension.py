"""Tests for phc.extensions.mail_alert.extension: configure()'s validation,
MailAlertInstance's to/from resolution and success/failure logging,
MailAlertAction, and an end-to-end test through phc.core.config.load_system().
The pure delivery logic itself is tested in tests/test_mail_alert.py."""

import logging

import pytest

from phc.core.config import ConfigError, load_system
from phc.core.registry import discover_extensions
from phc.core.scheduler import Scheduler
from phc.extensions.mail_alert.extension import MailAlertAction, configure
from tests.conftest import fetch_sync


@pytest.fixture
def mail_log(caplog):
    """caplog, but attached directly to the "phc.mail_alert" logger,
    bypassing propagate=False set by configure_logging() (invoked via
    load_system() in other tests in this session) -- same technique as
    tests/test_scheduler.py's task_log fixture."""
    mail_logger = logging.getLogger("phc.mail_alert")
    mail_logger.addHandler(caplog.handler)
    mail_logger.setLevel(logging.INFO)
    try:
        with caplog.at_level("INFO", logger="phc.mail_alert"):
            yield caplog
    finally:
        mail_logger.removeHandler(caplog.handler)


def _base_params(**overrides):
    """A complete params dict, as phc.core.config._merge_extension_params would
    hand to configure() -- every declared parameter present, instance-set
    or extension.yaml-defaulted. Direct configure() calls in these tests
    bypass _merge_extension_params, so (like tests/test_random_light_extension.py)
    they must supply the full dict themselves."""
    params = {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "security": "starttls",
        "username": None,
        "password": None,
        "from": "alerts@example.com",
        "to": ["a@example.com"],
        "timeout": "10s",
        "debug": False,
    }
    params.update(overrides)
    return params


# ---------- configure() ----------

def test_configure_builds_instance():
    instance = configure(_base_params(), {}, "mail_alert.house")
    assert instance.smtp_host == "smtp.example.com"
    assert instance.smtp_port == 587
    assert instance.security == "starttls"
    assert instance.default_from == "alerts@example.com"
    assert instance.default_to == ["a@example.com"]
    assert instance.timeout == 10.0


def test_configure_invalid_security_raises():
    with pytest.raises(ConfigError):
        configure(_base_params(security="carrier-pigeon"), {}, "mail_alert.house")


def test_configure_non_list_to_raises():
    with pytest.raises(ConfigError):
        configure(_base_params(to="a@example.com"), {}, "mail_alert.house")


def test_configure_missing_to_defaults_to_empty_list():
    instance = configure(_base_params(to=None), {}, "mail_alert.house")
    assert instance.default_to == []


# ---------- MailAlertInstance.send()/_deliver() ----------

def test_send_submits_to_shared_executor(monkeypatch):
    instance = configure(_base_params(), {}, "mail_alert.house")
    submitted = []
    monkeypatch.setattr("phc.extensions.mail_alert.extension._executor.submit",
                         lambda fn, *args: submitted.append((fn, args)))
    instance.send(to=["b@example.com"], title="Alarm", message="Sensor triggered")
    assert len(submitted) == 1
    fn, args = submitted[0]
    assert fn == instance._deliver
    assert args == (["b@example.com"], "Alarm", "Sensor triggered", "alerts@example.com")


def test_send_falls_back_to_instance_defaults(monkeypatch):
    instance = configure(_base_params(), {}, "mail_alert.house")
    submitted = []
    monkeypatch.setattr("phc.extensions.mail_alert.extension._executor.submit",
                         lambda fn, *args: submitted.append(args))
    instance.send(to=None, title="Alarm", message="Sensor triggered", from_addr=None)
    assert submitted[0] == (["a@example.com"], "Alarm", "Sensor triggered", "alerts@example.com")


def test_send_explicit_to_and_from_override_defaults(monkeypatch):
    instance = configure(_base_params(), {}, "mail_alert.house")
    submitted = []
    monkeypatch.setattr("phc.extensions.mail_alert.extension._executor.submit",
                         lambda fn, *args: submitted.append(args))
    instance.send(to=["c@example.com"], title="Alarm", message="msg", from_addr="other@example.com")
    assert submitted[0] == (["c@example.com"], "Alarm", "msg", "other@example.com")


def test_deliver_logs_success(monkeypatch, mail_log):
    instance = configure(_base_params(), {}, "mail_alert.house")
    monkeypatch.setattr("phc.extensions.mail_alert.extension.send_mail", lambda **kwargs: None)
    instance._deliver(["a@example.com"], "Alarm", "Sensor triggered", "alerts@example.com")
    assert any("sent" in r.message for r in mail_log.records)


def test_deliver_logs_warning_on_failure(monkeypatch, mail_log):
    instance = configure(_base_params(), {}, "mail_alert.house")

    def _raise(**kwargs):
        raise ConnectionRefusedError("no route to host")

    monkeypatch.setattr("phc.extensions.mail_alert.extension.send_mail", _raise)
    instance._deliver(["a@example.com"], "Alarm", "Sensor triggered", "alerts@example.com")
    assert any(r.levelno == logging.WARNING and "failed" in r.message for r in mail_log.records)


def test_deliver_logs_traceback_when_debug(monkeypatch, mail_log):
    instance = configure(_base_params(debug=True), {}, "mail_alert.house")

    def _raise(**kwargs):
        raise ConnectionRefusedError("no route to host")

    monkeypatch.setattr("phc.extensions.mail_alert.extension.send_mail", _raise)
    instance._deliver(["a@example.com"], "Alarm", "Sensor triggered", "alerts@example.com")
    record = next(r for r in mail_log.records if "failed" in r.message)
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None


# ---------- MailAlertAction ----------

def test_mail_alert_action_unknown_instance_raises():
    with pytest.raises(ConfigError):
        MailAlertAction(instance="mail_alert.nonexistent", extensions={}, title="t", message="m")


def test_mail_alert_action_perform_delegates_to_instance_send():
    instance = configure(_base_params(), {}, "mail_alert.house")
    calls = []
    instance.send = lambda **kwargs: calls.append(kwargs)
    action = MailAlertAction(instance="mail_alert.house", extensions={"mail_alert.house": instance},
                              title="Alarm", message="Sensor triggered", to=["c@example.com"],
                              **{"from": "other@example.com"})
    action.perform({})
    assert calls == [{"to": ["c@example.com"], "title": "Alarm", "message": "Sensor triggered",
                       "from_addr": "other@example.com"}]


def test_mail_alert_action_omitted_to_and_from_pass_through_as_none():
    instance = configure(_base_params(), {}, "mail_alert.house")
    calls = []
    instance.send = lambda **kwargs: calls.append(kwargs)
    action = MailAlertAction(instance="mail_alert.house", extensions={"mail_alert.house": instance},
                              title="Alarm", message="Sensor triggered")
    action.perform({})
    assert calls == [{"to": None, "title": "Alarm", "message": "Sensor triggered", "from_addr": None}]


# ---------- end-to-end via load_system() ----------

def test_end_to_end_load_system_sends_alert(tmp_path, monkeypatch):
    discover_extensions()
    sent = []
    monkeypatch.setattr("phc.extensions.mail_alert.extension.send_mail",
                         lambda **kwargs: sent.append(kwargs))
    # send() dispatches delivery to the shared background executor -- run it
    # inline instead, so the assertion below doesn't race the worker thread
    # (and so this test doesn't tear down the executor other tests share).
    monkeypatch.setattr("phc.extensions.mail_alert.extension._executor.submit",
                         lambda fn, *args: fn(*args))

    system_yaml = tmp_path / "system.yaml"
    system_yaml.write_text("""
heartbeat: 1s
devices:
  - id: siren
    module: virtual
    update: 1s
    endpoints:
      - key: state
        writable: true
        type: int
        default: 0

extensions:
  mail_alert:
    house:
      smtp_host: "smtp.example.com"
      from: "alerts@example.com"
      to: ["a@example.com"]

tasks:
  - tag: alert
    time: "+1s"
    actions:
      - kind: set
        device: "siren.state"
        value: 1
      - kind: mail_alert
        instance: "mail_alert.house"
        title: "Alarm"
        message: "Sensor triggered"
""")
    system = load_system(system_yaml)
    task = next(t for t in system.tasks if t.tag == "alert")
    task.due_time = 0.0

    scheduler = Scheduler(system.devices, tasks=system.tasks, heartbeat=system.heartbeat,
                          tick_hooks=system.tick_hooks)
    scheduler.tick(now=0.0)

    siren = system.devices["siren"]
    fetch_sync(siren)
    siren.update_state()
    assert siren.get() == 1

    assert sent and sent[0]["title"] == "Alarm"
