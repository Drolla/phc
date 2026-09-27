# Random Light Control

[`phc/extensions/random_light/`](../phc/extensions/random_light/) randomizes a set of
"light" devices to make an empty house look occupied — each light gets one
or more on/off time-of-day windows (a fixed local `"HH:MM"`, or
`"sunrise"`/`"sunset"` plus/minus an offset, resolved against a
[`phc/devices/sun/`](../phc/devices/sun/) device's live sunrise/sunset), a minimum
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
    condition: { device: "surveillance.armed", value: true }
    action: { kind: random_light, instance: "random_light.house" }
```

A `kind: random_light` action with `force: 0`/`force: 1` bypasses windows,
probability, and any `condition:` entirely, forcing every configured light
to that value immediately — for a surrounding system to drop into its own
tasks' `actions:` list (e.g. force everything off when arming/disarming,
force everything on as a deterrent during an alarm), as seen throughout
[`examples/emulated_surveillance-task_defs_1-nested.yaml`](../examples/emulated_surveillance-task_defs_1-nested.yaml)
(and its `-system_setup`/`-task_defs_2`/`-task_defs_3` companions).


## Simulating a Configuration

Since a light's actual on/off pattern depends on randomized probability
rolls, it's hard to tell from the YAML alone what a given `windows`/
`min_interval`/`probability_on` combination will really do over an evening
— or why one light seems to barely ever come on.
[`phc/extensions/random_light/simulate.py`](../phc/extensions/random_light/simulate.py)
answers that without a running system: it drives the same
`RandomLightController` decision code standalone, minute by minute over a
chosen time window, and prints each light's resulting on/off state plus
summary statistics (on-time, switch count, longest on-run, and how much of
the window — if any — had no light on at all).

It takes either a bare `random_light` instance file (a `lights:` list on
its own) or a full system YAML naming an instance with `--instance`:

```
python -m phc.extensions.random_light.simulate \
    --config examples/emulated_surveillance-system_setup.yaml --instance house \
    --from 19:00 --to 23:00 --seed 7
```

`--seed` makes a run reproducible (rerun the same command to get the exact
same pattern, e.g. to compare before/after a config edit); without it,
each run is genuinely random, like the real extension. See the script's
own `--help` for the rest (`--date`, `--latitude`/`--longitude`, `--step`
to thin out a long printed table).
