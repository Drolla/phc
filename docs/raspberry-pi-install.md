# Installing PHC on a Raspberry Pi

This guide sets up PHC on a Raspberry Pi running Raspberry Pi OS (Bookworm
or later), running under a dedicated system user, and optionally as a
`systemd` service that starts on boot and restarts on failure.

## 1. Prerequisites

PHC needs Python 3.11+. Raspberry Pi OS Bookworm ships Python 3.11 by
default; check what you have:

```
python3 --version
```

If it's older than 3.11, either upgrade to a current Raspberry Pi OS image
or install a newer Python via [pyenv](https://github.com/pyenv/pyenv) —
covering that is out of scope here.

Install the OS packages PHC's virtual environment will need to build/run:

```
sudo apt update
sudo apt install -y python3-venv python3-pip git
```

## 2. Create a dedicated user (recommended)

Running PHC as its own unprivileged user (rather than `pi`) limits what a
bug or a compromised dependency could touch on the rest of the system:

```
sudo useradd --system --create-home --home-dir /opt/phc --shell /usr/sbin/nologin phc
```

Everything below assumes PHC lives at `/opt/phc` and runs as the `phc`
user. If you'd rather run it under your own account (e.g. `pi`), skip this
step and substitute your own home directory/user throughout.

## 3. Fetch PHC

```
sudo git clone https://github.com/Drolla/phc.git /opt/phc
sudo chown -R phc:phc /opt/phc
```

(No public repository yet? `sudo -u phc git clone <your-fork-url> /opt/phc`,
or copy the project tree over with `rsync`/`scp` and `chown` it to `phc`
afterwards.)

## 4. Install PHC into a virtual environment

```
sudo -u phc -H bash -c '
  cd /opt/phc
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -e .
'
```

This installs PHC's dependencies (`PyYAML`, `aiohttp`, `astral`, `Jinja2`,
`psutil`) and generates a `phc` console command at
`/opt/phc/.venv/bin/phc` — this is the executable the systemd unit below
calls directly, so there's no need to activate the virtual environment or
put it on `PATH`.

Confirm it works:

```
sudo -u phc /opt/phc/.venv/bin/phc --config examples/virtual_system.yaml
```

You should see startup log lines on stdout and a live tick countdown; stop
it with Ctrl+C.

## 5. Write your system configuration

Copy one of the `examples/` files as a starting point for your own house,
rather than editing an example in place:

```
sudo -u phc cp /opt/phc/examples/virtual_system.yaml /opt/phc/system.yaml
```

Edit `/opt/phc/system.yaml` to describe your actual devices — see
[`docs/concepts.md`](concepts.md) and
[`docs/configuration.md`](configuration.md) for the config format, and
each `phc/devices/<name>/module.yaml` for what a given module supports. If
your Pi drives real hardware (e.g. a Razberry/Z-Wave controller), see
[`docs/zway.md`](zway.md).

A Pi is also a natural target for `system_monitor` — a device module that
reports the Pi's own CPU load, memory, disk usage, throughput, and
temperature, with no extra dependencies beyond `psutil` (already
installed). Add it to `system.yaml`:

```yaml
devices:
  - id: pi
    module: system_monitor
    name: Raspberry Pi
```

The directory referenced by extensions such as `timer`, `recovery`, and
`logdb` (log file paths) is resolved relative to the current working
directory PHC is started from, not the YAML file's location — the systemd
unit below sets that explicitly with `WorkingDirectory=`, so a relative
`path: "logs/timers.yaml"` (timer), `path: "logs/recovery.yaml"`
(recovery), or `csv_path: "logs/house_log.csv"` (logdb) in your config
lands under `/opt/phc/`:

```
sudo -u phc mkdir -p /opt/phc/logs
```

Validate the config actually loads before wiring up the service:

```
sudo -u phc /opt/phc/.venv/bin/phc --config /opt/phc/system.yaml
```

Ctrl+C to stop once you're satisfied it starts cleanly.

## 6. Run as a systemd service

Create `/etc/systemd/system/phc.service`:

```ini
[Unit]
Description=Pylon Home Control
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=phc
Group=phc
WorkingDirectory=/opt/phc
ExecStart=/opt/phc/.venv/bin/phc --config /opt/phc/system.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`After=`/`Wants=network-online.target` delays startup until the network is
up — worth having if your config includes devices that fetch over the
network (`meteoswiss`, `open_meteo`, `zway`). `Restart=on-failure` brings
PHC back up if it crashes, without masking a config error as a boot loop
(a bad config exits immediately and non-zero, `systemctl status` will show
it, and `RestartSec=5` keeps retries from spinning tight).

Enable and start it:

```
sudo systemctl daemon-reload
sudo systemctl enable --now phc.service
```

Check status and logs:

```
sudo systemctl status phc.service
sudo journalctl -u phc.service -f
```

PHC logs to stdout by default (see [Logging](configuration.md#logging)),
which systemd captures into the journal automatically — `journalctl` above
is the log viewer, no separate log file needed. To also keep a persistent
file log (e.g. for [`phc/extensions/web_ui`](web-ui.md) or `logdb` history
independent of journal rotation), add a file `dest:` to `system.yaml`'s
`log:` list — see [Logging](configuration.md#logging).

Stop or restart after a config change:

```
sudo systemctl restart phc.service
```

## 7. Optional: web UI dashboard

If your config includes [`phc/extensions/web_ui`](web-ui.md), it binds to
`127.0.0.1` by default (loopback-only, no authentication) — safe to leave
as-is if you'll only ever browse it from the Pi itself, or if you reach it
through an SSH tunnel:

```
ssh -L 8080:localhost:8080 pi@<raspberry-pi-address>
```

then open `http://localhost:8080` locally. If you want LAN access instead,
change `host:` to the Pi's LAN address or `0.0.0.0` — but since there's no
built-in authentication, only do this on a network you trust, or put a
reverse proxy with auth (e.g. `nginx`) in front of it.

## 8. Updating PHC

```
sudo systemctl stop phc.service
sudo -u phc -H bash -c '
  cd /opt/phc
  git pull
  .venv/bin/pip install -e .
'
sudo systemctl start phc.service
```

Re-run `pip install -e .` after every update in case dependencies changed
— it's a no-op if they haven't.

## Troubleshooting

- **Service won't start / exits immediately** — run the same `ExecStart`
  command manually as the `phc` user (step 5's validation command); a
  config error prints a clear message there, whereas `systemctl status`
  only shows the exit code.
- **`ModuleNotFoundError` under systemd but not when run manually** — the
  unit's `ExecStart` must point at `/opt/phc/.venv/bin/phc` (the
  virtual environment's own console script), not a bare `phc` or the
  system `python3`.
- **Permission denied on a log/state file path** — paths under `path:`/
  `csv_path:` in `system.yaml` are resolved relative to
  `WorkingDirectory:`; confirm the directory exists and is owned by the
  `phc` user (step 5).
