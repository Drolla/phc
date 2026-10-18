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


def _main(argv: list[str] | None = None) -> int:
    """CLI: send one test message using an instance from a system YAML.

    `python -m phc.extensions.mail_alert.mail_alert <system.yaml> <instance>`.
    Imports phc lazily so send_mail() itself stays free of phc imports."""
    import argparse
    from datetime import datetime

    from phc.core.config.yamlio import load_yaml
    from phc.core.errors import ConfigError
    from phc.core.extension.config import _load_extension_descriptor, _merge_extension_params
    from phc.core.registry import discover_extensions
    from phc.extensions.mail_alert.extension import configure

    parser = argparse.ArgumentParser(
        description="Send a test message through an extensions.mail_alert.<instance> of a system YAML.")
    parser.add_argument("config", help="system YAML (its !include lines are resolved)")
    parser.add_argument("instance", help="instance name under extensions.mail_alert")
    parser.add_argument("--to", nargs="+", help="recipients (default: the instance's 'to')")
    parser.add_argument("--from", dest="from_addr", help="sender (default: the instance's 'from')")
    parser.add_argument("--title", default="phc mail_alert test")
    parser.add_argument("--message", default=None)
    args = parser.parse_args(argv)

    try:
        discover_extensions()
        instances = ((load_yaml(args.config) or {}).get("extensions") or {}).get("mail_alert") or {}
        if args.instance not in instances:
            raise ConfigError(f"no extensions.mail_alert.{args.instance} in {args.config}; "
                              f"available: {sorted(instances)}")
        key = f"mail_alert.{args.instance}"
        params = _merge_extension_params(_load_extension_descriptor("mail_alert"),
                                         instances[args.instance], key)
        inst = configure(params, {}, key)
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 2

    to = args.to or inst.default_to
    sender = args.from_addr or inst.default_from
    print(f"{inst.smtp_host}:{inst.smtp_port} ({inst.security}), user={inst.username}, "
          f"from={sender}, to={to}")
    try:
        send_mail(smtp_host=inst.smtp_host, smtp_port=inst.smtp_port, security=inst.security,
                  username=inst.username, password=inst.password, timeout=inst.timeout,
                  from_addr=sender, to=to, title=args.title,
                  message=args.message or f"Test message sent {datetime.now():%Y-%m-%d %H:%M:%S}")
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1
    print("sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
