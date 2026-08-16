"""Tests for phc.extensions.mail_alert.mail_alert.send_mail: the pure delivery
logic (message construction, security-mode branching), independent of the
extension wiring/threading in phc/extensions/mail_alert/extension.py (see
tests/test_mail_alert_extension.py)."""

import smtplib

import pytest

from phc.extensions.mail_alert.mail_alert import send_mail


class _FakeSMTP:
    """Records constructor args and method calls instead of opening a real
    socket. Used as both the plaintext (smtplib.SMTP) and SSL
    (smtplib.SMTP_SSL) stand-in via monkeypatch."""

    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls = []
        type(self).instances.append(self)

    def starttls(self):
        self.calls.append("starttls")

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def send_message(self, msg):
        self.calls.append(("send_message", msg))

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FailingSMTP(_FakeSMTP):
    def send_message(self, msg):
        raise smtplib.SMTPException("connection refused")


@pytest.fixture(autouse=True)
def _reset_instances():
    _FakeSMTP.instances = []
    yield
    _FakeSMTP.instances = []


def _send(monkeypatch, smtp_cls=_FakeSMTP, **overrides):
    monkeypatch.setattr("phc.extensions.mail_alert.mail_alert.smtplib.SMTP", smtp_cls)
    monkeypatch.setattr("phc.extensions.mail_alert.mail_alert.smtplib.SMTP_SSL", smtp_cls)
    params = dict(
        smtp_host="smtp.example.com", smtp_port=587, security="starttls",
        username=None, password=None, timeout=10.0,
        from_addr="alerts@example.com", to=["a@example.com", "b@example.com"],
        title="Alarm", message="Sensor triggered",
    )
    params.update(overrides)
    send_mail(**params)


def test_message_headers_and_body(monkeypatch):
    _send(monkeypatch)
    smtp = _FakeSMTP.instances[0]
    _, msg = next(call for call in smtp.calls if call[0] == "send_message")
    assert msg["Subject"] == "Alarm"
    assert msg["From"] == "alerts@example.com"
    assert msg["To"] == "a@example.com, b@example.com"
    assert msg.get_content().strip() == "Sensor triggered"


def test_connects_with_host_port_timeout(monkeypatch):
    _send(monkeypatch, smtp_port=2525, timeout=5.0)
    smtp = _FakeSMTP.instances[0]
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.example.com", 2525, 5.0)


def test_starttls_called_for_starttls_security(monkeypatch):
    _send(monkeypatch, security="starttls")
    assert "starttls" in _FakeSMTP.instances[0].calls


def test_starttls_not_called_for_ssl_security(monkeypatch):
    _send(monkeypatch, security="ssl")
    assert "starttls" not in _FakeSMTP.instances[0].calls


def test_starttls_not_called_for_no_security(monkeypatch):
    _send(monkeypatch, security="none")
    assert "starttls" not in _FakeSMTP.instances[0].calls


def test_login_called_when_username_set(monkeypatch):
    _send(monkeypatch, username="user", password="secret")
    assert ("login", "user", "secret") in _FakeSMTP.instances[0].calls


def test_login_not_called_when_username_unset(monkeypatch):
    _send(monkeypatch, username=None)
    assert not any(call[0] == "login" for call in _FakeSMTP.instances[0].calls if isinstance(call, tuple))


def test_send_failure_propagates(monkeypatch):
    with pytest.raises(smtplib.SMTPException):
        _send(monkeypatch, smtp_cls=_FailingSMTP)


def test_ssl_security_uses_smtp_ssl_not_plain_smtp(monkeypatch):
    class _PlainSMTP(_FakeSMTP):
        instances = []

    class _SslSMTP(_FakeSMTP):
        instances = []

    monkeypatch.setattr("phc.extensions.mail_alert.mail_alert.smtplib.SMTP", _PlainSMTP)
    monkeypatch.setattr("phc.extensions.mail_alert.mail_alert.smtplib.SMTP_SSL", _SslSMTP)
    send_mail(smtp_host="smtp.example.com", smtp_port=465, security="ssl",
              username=None, password=None, timeout=10.0, from_addr="alerts@example.com",
              to=["a@example.com"], title="Alarm", message="Sensor triggered")
    assert _PlainSMTP.instances == []
    assert len(_SslSMTP.instances) == 1


def test_non_ssl_security_uses_plain_smtp_not_smtp_ssl(monkeypatch):
    class _PlainSMTP(_FakeSMTP):
        instances = []

    class _SslSMTP(_FakeSMTP):
        instances = []

    monkeypatch.setattr("phc.extensions.mail_alert.mail_alert.smtplib.SMTP", _PlainSMTP)
    monkeypatch.setattr("phc.extensions.mail_alert.mail_alert.smtplib.SMTP_SSL", _SslSMTP)
    send_mail(smtp_host="smtp.example.com", smtp_port=587, security="starttls",
              username=None, password=None, timeout=10.0, from_addr="alerts@example.com",
              to=["a@example.com"], title="Alarm", message="Sensor triggered")
    assert _SslSMTP.instances == []
    assert len(_PlainSMTP.instances) == 1
