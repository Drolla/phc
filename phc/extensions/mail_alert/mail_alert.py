"""Pure mail-delivery logic for the mail_alert extension.

Builds one plain-text message and sends it via stdlib smtplib -- no phc
imports, so this is testable (and swappable) independently of the
extension wiring in extension.py, which resolves instance/action
config and offloads the call here onto a background thread."""

import smtplib
from email.message import EmailMessage


def send_mail(*, smtp_host: str, smtp_port: int, security: str, username: str | None,
              password: str | None, timeout: float, from_addr: str, to: list[str],
              title: str, message: str) -> None:
    """Send one plain-text message.

    `security` is "starttls" (upgrade a plaintext connection), "ssl"
    (implicit TLS from connection start), or "none" (unencrypted) --
    validated by extension.py's configure(), not here. Raises on any
    connection/auth/send failure; the caller is responsible for
    catching and logging it."""
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    msg.set_content(message)

    smtp_cls = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    with smtp_cls(smtp_host, smtp_port, timeout=timeout) as smtp:
        if security == "starttls":
            smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(msg)
