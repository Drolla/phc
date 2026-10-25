# Matter Integration

PHC controls Matter devices (smart plugs, lights, sensors) through the
**Open Home Foundation Matter Server** (`matterjs-server`), which runs on the
same Raspberry Pi as PHC. The server pairs the devices and keeps the
connection to them; PHC talks to the server.

This guide sets up the server with Docker on a Raspberry Pi 4 (section 1),
pairs a first device (section 2) and adds it to PHC (section 3). Section 4
describes a command-line tool for advanced use.

---

## 1. Matter Server Installation (on a Raspberry Pi 4)

### Step 1 - Prepare the OS

Use Raspberry Pi OS Lite, 64-bit (Bookworm or newer), and update it:

```
sudo apt update && sudo apt full-upgrade
sudo reboot
```

- Connect the Pi via Ethernet: the Pi 4's Wi-Fi and Bluetooth share one
  radio, and pairing over Bluetooth is more reliable without Wi-Fi traffic.
- Keep the Pi and the Matter devices in the same home network (no separate
  guest network or VLAN).

### Step 2 - Check IPv6

Matter needs IPv6, which Raspberry Pi OS enables by default. Check it: the
first command must list an `inet6 fe80::` address, the second must print
`0`.

```
ip -6 addr show eth0
sysctl net.ipv6.conf.all.forwarding
```

If the second prints `1`, another program (e.g. a VPN) has enabled IPv6
forwarding. Find the line that sets it (lines starting with `#` are
inactive), remove it, then reload the settings:

```
grep -rn "ipv6.*forwarding" /etc/sysctl.conf /etc/sysctl.d/
sudo sysctl --system
```

Thread devices only: NetworkManager 1.42 or newer is needed (Bookworm has it):

```
nmcli --version
```

### Step 3 - Raise the conntrack UDP timeout (optional)

Only needed if a firewall filters incoming traffic (none by default on
Raspberry Pi OS); it keeps battery devices reachable if you add one later.

```
echo nf_conntrack | sudo tee /etc/modules-load.d/matter-conntrack.conf
echo "net.netfilter.nf_conntrack_udp_timeout_stream = 3600" | sudo tee /etc/sysctl.d/99-matter-conntrack.conf
sudo modprobe nf_conntrack
sudo sysctl --system
```

Check after a reboot (must print `3600`):

```
sysctl net.netfilter.nf_conntrack_udp_timeout_stream
```

### Step 4 - Enable Bluetooth

New devices are paired over Bluetooth. Check (expected: `enabled`, `active`,
`Powered: yes`):

```
systemctl is-enabled bluetooth
systemctl is-active bluetooth
bluetoothctl show | grep Powered
```

Fixes, depending on what fails:

```
sudo systemctl enable --now bluetooth   # not enabled / not active
bluetoothctl power on                   # Powered: no
rfkill list                             # "No default controller available" ...
sudo rfkill unblock bluetooth           # ... and Bluetooth is "Soft blocked: yes"
```

**With a UART shield (e.g. RaZberry/Z-Way).** The shield and the Pi's
Bluetooth both need the Pi's main serial port (UART), so shield setups often
disable Bluetooth (`dtoverlay=disable-bt`), which shows as "No default
controller available". To keep both, move Bluetooth to the secondary serial
port: in `/boot/firmware/config.txt`, remove `dtoverlay=disable-bt` if
present and add the lines below (`core_freq=250` keeps the secondary port's
speed, and so the Bluetooth link, stable).

```
dtoverlay=miniuart-bt
core_freq=250
```

If the serial port is used for data rather than a login console, also
remove `console=serial0,115200` from `/boot/firmware/cmdline.txt`. Reboot,
rerun the checks above, and check that the shield kept the main serial port
(expected: `/dev/serial0 -> ttyAMA0`):

```
ls -l /dev/serial0
```

### Step 5 - Create the data directory

The server stores its data, including the keys of your Matter network, in
`/opt/matter-server/data`. It runs as user ID 1000 (normally your first
user, e.g. `pi`), and the directory is readable by that user only:

```
sudo mkdir -p /opt/matter-server/data
sudo chown -R 1000:1000 /opt/matter-server/data
sudo chmod -R u+rwX,go-rwx /opt/matter-server/data
```

Check (expected: `drwx------` and `1000 1000`):

```
ls -ldn /opt/matter-server/data
```

### Step 6 - Install Docker

Check whether Docker is already installed, and that the OS is 64-bit
(expected: `arm64`):

```
docker --version
docker compose version
dpkg --print-architecture
```

If not installed, follow Docker's Debian instructions, section "Install
using the apt repository": https://docs.docker.com/engine/install/debian/
(not the "Raspberry Pi OS" page, which covers only 32-bit).

### Step 7 - Install and start the Matter server

Create `/opt/matter-server/docker-compose.yml`:

```yaml
services:
  matterjs-server:
    container_name: matterjs-server
    image: ghcr.io/matter-js/matterjs-server:1.5.4  # pinned version
    restart: unless-stopped
    network_mode: host            # required for Matter device discovery
    read_only: true
    # mem_limit: 700m             # ignored on Raspberry Pi OS, see below
    volumes:
      - ./data:/data
      - /run/dbus:/run/dbus:ro    # access to the Pi's Bluetooth
    environment:
      - NOBLE_BINDINGS=dbus
      - BLUETOOTH_ADAPTER=0
      - LISTEN_ADDRESS=127.0.0.1  # reachable from this Pi only
      - TZ=Europe/Zurich          # your time zone
      - NODE_OPTIONS=--max-old-space-size=512   # caps memory use at ~512 MB
```

Then download the server and start it (Docker downloads it on the first
start; `logs -f` shows its log, Ctrl+C to quit):

```
cd /opt/matter-server
sudo docker compose pull
sudo docker compose up -d
sudo docker compose logs -f
```

The server has no password: anyone who can reach it can control and pair
devices. `LISTEN_ADDRESS=127.0.0.1` therefore limits it to programs on this
Pi, such as PHC; from another computer, use the SSH tunnel of step 9.

- Without working Bluetooth (step 4), remove the `/run/dbus` line and the
  two Bluetooth lines, and pair devices via Apple Home or Google Home
  instead (see section 2).
- `mem_limit` would cap the whole container, but Raspberry Pi OS ignores it
  unless `cgroup_enable=memory` is added to the single line in
  `/boot/firmware/cmdline.txt` (then reboot). `NODE_OPTIONS` works without.
- To update later, change the version in `image:` (releases:
  https://github.com/matter-js/matterjs-server/releases), then:

```
cd /opt/matter-server
sudo docker compose pull
sudo docker compose up -d
```

### Step 8 - Back up the data directory

`/opt/matter-server/data` holds the keys of your Matter network. Losing it
means pairing every device again; anyone who has it can control your
devices. Back it up, and keep the backup private:

```
sudo tar czf ~/matter-data-$(date +%F).tar.gz -C /opt/matter-server data
```

### Step 9 - Verify

On the Pi (expected: `HTTP/1.1 200 OK`):

```
curl -sI http://127.0.0.1:5580/
```

To open the server's dashboard from another computer, start an SSH tunnel
there and keep it open, then browse to `http://localhost:5580/`:

```
ssh -L 5580:localhost:5580 pi@<pi-address>
```

---

## 2. Pairing devices (commissioning)

Devices are paired in the server's dashboard (`http://localhost:5580/`
through the SSH tunnel of section 1, step 9).

1. **Enter the Wi-Fi credentials first.** In **Settings**, enter the Wi-Fi
   name (2.4 GHz network) and password. Needed once, before the first
   Wi-Fi device.
2. **Add the device.** Select **Commission node**, choose the device type
   (e.g. Wi-Fi), and enter the pairing code printed on the device or its
   packaging: 11 digits, often shown as `1234-567-8901` (the dashes are
   optional), or less commonly 21 digits.
3. **Name the device.** Open the new node in the dashboard and give it a
   name (stored on the device). The node page also shows the serial number.
   PHC's `node:` setting (section 3) refers to the device by either.

Notes:

- The device must be in pairing mode: new or factory-reset, and within
  about 2 m of the Pi. A device already added to Apple Home, Google Home or
  Alexa has to be reset first.
- The Pi needs internet access during pairing, to verify the device.
- Thread devices (often battery sensors) need a Thread network. If yours
  comes from an Apple or Google hub, pair them in Apple Home or Google Home
  first, then share them to the Matter server (multi-admin).
- Devices already on the network (e.g. via Ethernet) are paired without
  Bluetooth.

If pairing fails, the usual causes are a missing internet connection, or
IPv6 not working between the Pi and the device (section 1, step 2). Battery
devices that go offline every 15-30 minutes point to a firewall (section 1,
step 3).

---

## 3. Configuring PHC

Paired devices are added to PHC with the `matter` module, for example:

```yaml
modules:
  matter:
    update: 1s
    # url: "ws://127.0.0.1:5580/ws"     # the default

devices:
  - id: plug_office
    module: matter
    name: Office Plug
    node: "My 1st Power Switch (Osram)"
    endpoints:
      - key: switch
        endpoint_profile: switch
      - key: power
        endpoint_profile: power
```

`node:` is the device's name or serial number, as shown in the dashboard,
or its node number, which changes when the device is paired again. It must
match exactly one device; otherwise PHC stops with an error listing the
candidates. Upper/lower case doesn't matter (unless the endpoint sets
`match_case: true`), and `*` matches any text, e.g. `"*Power Switch*"`. An
endpoint can set its own `node:` to combine several devices in one PHC
device.

More examples: [`examples/matter_system.yaml`](../examples/matter_system.yaml)
and [`examples/devices/matter_devices.yaml`](../examples/devices/matter_devices.yaml).

### Endpoints

`endpoint_profile` selects which value an endpoint reads or switches.
`endpoint:` selects the part of the device it belongs to, for devices with
several (default 1). The profiles are defined under `endpoint_profiles:` in
the module's [`module.yaml`](../phc/devices/matter/module.yaml#L94); see
[`profiles.md`](profiles.md) for profiles in general.

| Profile | Value | Writable | Matter attribute |
|---|---|---|---|
| `switch` | on/off | yes | On/Off `6/0` |
| `dimmer` | 0-100 % | yes | Level Control `8/0` |
| `power` | W | no | Electrical Power `144/8` |
| `voltage` | V | no | Electrical Power `144/4` |
| `current` | A | no | Electrical Power `144/5` |
| `energy` | kWh consumed | no | Electrical Energy `145/1` |
| `temperature` | °C | no | `1026/0` |
| `humidity` | % | no | `1029/0` |
| `motion` | detected or not | no | Occupancy `1030/0` |
| `contact` | open/closed | no | Boolean State `69/0` |
| `battery` | % | no | Power Source `47/12` |

With `update: 1s`, values follow a change within about a second. A device
that is offline,
or doesn't report a value, shows as empty (`None`). PHC reconnects by itself
after the server restarts.

Tested on a real device (LEDVANCE Matter plug): reading `switch`, `power`,
`voltage` and `energy`. Switching, `dimmer` and the sensor profiles follow
the Matter specification but haven't been tried on hardware yet.

### Any other attribute

For a value without a profile, an endpoint can name the Matter attribute
directly (`cluster/attribute`, decimal numbers):

```yaml
- key: tvoc
  attribute: "1070/0"
  type: float
  scale: 0.001               # optional: converts to the endpoint's unit
```

Further options: `field: <n>` reads one part of a structured value (e.g.
`field: 0` for the energy in `145/1`), `mask: <bits>` reads a set of flags
as true/false. To make the endpoint writable, add
`commands: {true: "On", false: "Off"}`, or `level_command:` for a number.

---

## 4. Command-line tool (advanced)

[`demo.py`](../phc/devices/matter/demo.py) talks to the server the same way
PHC does. It shows what the server has paired and what each device reports,
and gives access to every server command. Define a shortcut once (add the
line to `~/.bashrc` to keep it):

```
alias matter='/opt/phc/.venv/bin/python /opt/phc/phc/devices/matter/demo.py'
```

Devices and their values:

```
matter list
matter get "Living lamp"
matter get "Living lamp" 1/6/0
matter on "Living lamp"
matter off "Living lamp"
matter label 3 "Living lamp"
matter listen
```

- `list`: every device with its name, product, status and parts.
- `get`: all values of a device, or one value by its
  `endpoint/cluster/attribute` path.
- `label`: names a device, like the dashboard does.
- `listen`: prints every change the server reports, until Ctrl+C.

Any server command, with its arguments as JSON:

```
matter raw set_wifi_credentials '{"ssid": "MyWifi", "credentials": "secret"}'
matter raw parse_onboarding_payload '{"code": "34970112332"}'
matter raw discover
matter raw commission_with_code '{"code": "34970112332"}'
matter raw device_command '{"node_id": 3, "endpoint_id": 1, "cluster_id": 6, "command_name": "Toggle", "payload": {}}'
matter raw read_attribute '{"node_id": 3, "attribute_path": "1/6/0"}'
matter raw ping_node '{"node_id": 3}'
matter raw interview_node '{"node_id": 3}'
matter raw open_commissioning_window '{"node_id": 3}'
matter raw remove_node '{"node_id": 3}'
matter raw diagnostics
```

- `code`: the pairing code without dashes, or the `MT:...` text obtained by
  scanning the QR code with a QR scanner app.
- `parse_onboarding_payload`: checks a pairing code without pairing.
- `discover`: lists devices in pairing mode on the network.
- `read_attribute`: asks the device itself (`get` uses the server's copy).
- `ping_node`: checks the device is reachable.
- `interview_node`: re-reads everything from the device, e.g. after a
  firmware update.
- `open_commissioning_window`: returns a pairing code for adding the device
  to Apple Home or Google Home as well.
- `remove_node`: removes the device; it has to be paired again afterwards.
- `diagnostics`: server status, devices and recent events, for bug reports.

---

## References

- Matter server: https://github.com/matter-js/matterjs-server
  - Docker guide:
    https://github.com/matter-js/matterjs-server/blob/main/docs/docker.md
  - Network and OS requirements:
    https://github.com/matter-js/matterjs-server/blob/main/docs/os_requirements.md
  - Command and argument names for `matter raw`:
    `python_client/matter_server/client/client.py` in that repository
- Home Assistant's Matter documentation (network troubleshooting):
  https://www.home-assistant.io/integrations/matter/
