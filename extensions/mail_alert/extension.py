"""mail_alert extension wiring: resolves an extensions.mail_alert.<instance>'s
SMTP connection details and default sender/recipients into a MailAlertInstance,
and registers the "mail_alert" task action kind so a hand-authored `tasks:`
entry can send a message through it -- see core.config._load_extensions for
how configure() is invoked, and core.registry.discover_extensions() for how
this module's @register_task_kind decorator gets picked up at startup."""

import logging
from concurrent.futures import ThreadPoolExecutor

from core.config import ConfigError
from core.device import Device
from core.intervals import parse_duration
from core.registry import register_task_kind
from core.task import Action
from extensions.mail_alert.mail_alert import send_mail

logger = logging.getLogger("phc.mail_alert")

_SECURITY_MODES = ("starttls", "ssl", "none")

# Shared by every configured mail_alert instance: sends are infrequent
# alerts, not high-throughput, so one small pool suffices and keeps
# configure() simple -- no per-instance thread lifecycle to manage.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="phc-mail")


def configure(params: dict, flat: dict[str, Device], instance_key: str,
              extensions_registry: dict | None = None) -> "MailAlertInstance":
    """Extension entry point (see core.config._load_extensions): validate
    this instance's SMTP settings and build a MailAlertInstance. Unlike
    logdb/random_light, nothing here is resolved against `flat` -- a mail
    alert has no device target. `extensions_registry` (see
    core.config._load_extensions) is unused here -- mail_alert never
    references another extension's instance."""
    security = params["security"]
    if security not in _SECURITY_MODES:
        raise ConfigError(
            f"mail_alert instance {instance_key!r}: security must be one of "
            f"{_SECURITY_MODES}, got {security!r}")

    to = params.get("to") or []
    if not isinstance(to, list):
        raise ConfigError(f"mail_alert instance {instance_key!r}: 'to' must be a list")

    timeout = parse_duration(params["timeout"])

    return MailAlertInstance(
        smtp_host=params["smtp_host"],
        smtp_port=int(params["smtp_port"]),
        security=security,
        username=params.get("username"),
        password=params.get("password"),
        timeout=timeout,
        default_from=params["from"],
        default_to=to,
        debug=bool(params.get("debug", False)),
    )


class MailAlertInstance:
    """One configured mail_alert instance: resolved SMTP connection settings
    plus default sender/recipients. Looked up by name from a mail_alert
    task action (see core.config._load_extensions/_build_action)."""

    def __init__(self, *, smtp_host: str, smtp_port: int, security: str,
                 username: str | None, password: str | None, timeout: float,
                 default_from: str, default_to: list[str], debug: bool):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.security = security
        self.username = username
        self.password = password
        self.timeout = timeout
        self.default_from = default_from
        self.default_to = default_to
        self.debug = debug

    def send(self, *, to: list[str] | None, title: str, message: str,
              from_addr: str | None = None) -> None:
        """Resolve `to`/`from_addr` against this instance's defaults and
        submit the actual delivery to the shared background pool -- returns
        immediately, so a slow/unreachable mail server never blocks the
        calling Action.perform() (see extension.yaml's description)."""
        recipients = to or self.default_to
        sender = from_addr or self.default_from
        _executor.submit(self._deliver, recipients, title, message, sender)

    def _deliver(self, to: list[str], title: str, message: str, from_addr: str) -> None:
        """Runs on a worker thread: perform the actual SMTP send and log the
        outcome (success/failure is never surfaced back to the firing
        task -- mirrors the old Tcl system's SendDirect success/failure log
        line and its -debug flag)."""
        try:
            send_mail(smtp_host=self.smtp_host, smtp_port=self.smtp_port, security=self.security,
                      username=self.username, password=self.password, timeout=self.timeout,
                      from_addr=from_addr, to=to, title=title, message=message)
            logger.info("mail alert %r sent to %s", title, to)
        except Exception:
            if self.debug:
                logger.exception("mail alert %r failed to send to %s", title, to)
            else:
                logger.warning("mail alert %r failed to send to %s", title, to)


@register_task_kind("mail_alert")
class MailAlertAction(Action):
    """Sends one message through the named mail_alert instance when this
    task fires. Has no single target device, so it opts out of
    _build_action's device resolution."""

    requires_device = False

    def __init__(self, *, instance: str, extensions: dict, title: str, message: str,
                 to: list[str] | None = None, **params):
        # "from" is a reserved word, so it can't be a named parameter --
        # popped out of the catch-all **params instead (still an ordinary
        # YAML action key, e.g. `from: "alerts@example.com"`).
        from_addr = params.pop("from", None)
        super().__init__(device_id="", endpoint_key=None, **params)
        try:
            self._instance: MailAlertInstance = extensions[instance]
        except KeyError:
            raise ConfigError(
                f"mail_alert action: unknown extensions instance {instance!r}; "
                f"available: {sorted(extensions)}") from None
        self._title = title
        self._message = message
        self._to = to
        self._from = from_addr

    def perform(self, devices: dict[str, Device]) -> None:
        self._instance.send(to=self._to, title=self._title, message=self._message, from_addr=self._from)
