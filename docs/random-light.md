# Random light control

[`extensions/random_light/`](../extensions/random_light/) randomizes a set of
"light" devices to make an empty house look occupied — each light gets one
or more on/off time-of-day windows (a fixed local `"HH:MM"`, or
`"sunrise"`/`"sunset"` plus/minus an offset, resolved against a
[`devices/sun/`](../devices/sun/) device's live sunrise/sunset), a minimum
switch interval, and a probability of being on. `windows`/`min_interval`/
`probability_on` cascade three ways: `extension.yaml`'s own default → this
instance's own `windows`/`min_interval`/`probability_on` (applies to every
light below that doesn't set its own) → each light's own override:

```yaml
extensions:
  random_light:
    house:
      lights:
        - device: "hallway_light.state"
          default: true   # forced on if, after a pass, no light ended up on
          # no windows/min_interval/probability_on of its own -- inherits
          # extension.yaml's own defaults (see below)
        - device: "porch_light.state"
          windows:
            - { start: "sunset+12m", end: "23:30" }
            - { start: "06:00", end: "sunrise-10m" }
          min_interval: 15m
          probability_on: 0.4

tasks:
  - tag: random_light_tick
    time: "+5s"
    repeat: 1m
    # Gate the periodic pass on other device state (e.g. "only while
    # armed and not alarmed") with the task's own condition: -- see
    # docs/configuration.md's Tasks section -- rather than a
    # random_light-specific parameter.
    condition: { device: "surveillance.armed", value: 1 }
    action: { kind: random_light, instance: "random_light.house" }
```

A `kind: random_light` action with `force: 0`/`force: 1` bypasses windows,
probability, and any `condition:` entirely, forcing every configured light
to that value immediately — for a surrounding system to drop into its own
tasks' `actions:` list (e.g. force everything off when arming/disarming,
force everything on as a deterrent during an alarm), as seen throughout
[`examples/virtual_surveillance-task_defs_1-nested.yaml`](../examples/virtual_surveillance-task_defs_1-nested.yaml)
(and its `-system_setup`/`-task_defs_2`/`-task_defs_3` companions).
