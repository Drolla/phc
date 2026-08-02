# Mail alerts

[`extensions/mail_alert/`](../extensions/mail_alert/) sends a message through
one configured SMTP server — an `extensions.mail_alert.<instance>` entry
holds the server's connection details plus a default sender/recipient
list, and a `kind: mail_alert` task action sends one message through it,
alongside a task's other actions (`set`, `random_light`, ...). A recipient
can be an ordinary mailbox or an email-to-SMS gateway address — both are
just SMTP recipients as far as this extension is concerned. Delivery
success/failure is logged, not surfaced back to the firing task, so a slow
or unreachable mail server can't stall the whole system:

```yaml
extensions:
  mail_alert:
    house:
      smtp_host: "smtp.example.com"
      username: "alerts@example.com"
      password: "..."           # plain text -- phc has no secrets mechanism; guard this file accordingly
      from: "alerts@example.com"
      to:
        - "someone@example.com"
        - "15555550123@sms.example.com"   # an email-to-SMS gateway, same as any other recipient

tasks:
  - tag: intrusion_alert
    condition: { device: "hallway_motion.state", changed: true }
    min_interval: 5m   # don't refire more than once every 5 minutes
    actions:
      - kind: set
        device: "siren.state"
        value: 1
      - kind: mail_alert
        instance: "mail_alert.house"
        title: "Home Security - Alarm Alert"
        message: "Sensor triggered"
```

`to`/`from` on the action itself override the instance's defaults when
given. See
[`examples/virtual_surveillance_system.yaml`](../examples/virtual_surveillance_system.yaml)
for a fuller worked example, wired into its intrusion-detection task.
