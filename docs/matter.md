# Matter Integration

[`phc/devices/matter/`](../phc/devices/matter/) reads and controls Matter
devices (plugs, lights, sensors) through a **matterjs-server** running on
the same machine, over its WebSocket API. PHC does not pair devices
itself: the server does. Sections 1-3 set the server up on a Raspberry Pi 4
and pair a first device; section 4 configures PHC; section 5 describes the
command-line tool used throughout.

---

## 1. Recommended solution

Use the **Open Home Foundation Matter Server based on matter.js
(`matterjs-server`)**, run standalone on the Pi. Your own system talks to it
over its **WebSocket API**.

Why:

- **Current and maintained.** The old `python-matter-server` (8.1.2) was
  archived on 2026-06-23 and is no longer supported. Ignore tutorials that
  still use it.
- **Built for reuse.** It powers Home Assistant but is explicitly designed
  for use in other projects.
- **Mature.** `matterjs-server` 1.0.0 was released 2026-06-09 and became the
  Home Assistant default on 2026-06-23. The current release is 1.5.4
  (2026-10-23).
- **Easy to integrate.** It offers a JSON-over-WebSocket API (default port
  `5580`, path `/ws`) that is compatible with the old Python server API.
  Client libraries exist for JS (`@matter-server/ws-client`) and Python
  (`matter-python-client`), and any language can speak the protocol directly.

What the server needs from its environment:

- **Thread devices need a Thread Border Router on the LAN.** Either use an
  existing Apple TV, HomePod or Google/Nest hub, or run OpenThread Border
  Router (OTBR) with an RCP USB dongle. Wi-Fi and Ethernet devices don't need
  one. The choice decides how Thread devices are commissioned (see section 3):
  - Own OTBR: you have the Thread dataset, so the server can commission
    Thread devices itself over Bluetooth.
  - Apple/Google border router: their Thread dataset is not exposed, so pair
    the device in Apple Home or Google Home first, then share it to the
    server (multi-admin).
  - OTBR needs IPv6 forwarding enabled, while the Matter server's host should
    have it disabled (see section 2). Prefer running OTBR on separate
    hardware rather than on the same Pi.
- **New devices need Bluetooth LE for commissioning,** using either the Pi's
  own Bluetooth or a BLE proxy (`BLE_PROXY`). Two cases need no Bluetooth:
  devices already on the IP network (`commission_on_network`), and devices
  shared from another controller with a multi-admin pairing code.

Alternatives (not recommended here):

- **Embed matter.js directly in your own app.** Only worth it if your app is
  Node/TypeScript and you want full control. You lose the dashboard and the
  stable API.
- **Official C++ SDK (connectedhomeip / chip-tool).** Heavy to build and
  awkward to run as a service. Use it for testing and debugging only.

### Integration with PHC

PHC's `matter` module speaks the server's JSON protocol directly with
aiohttp, the same pattern as the [Z-Way module](zway.md), so it adds no
dependency. The Python client library `matter-python-client` is not used:
it requires Python 3.12+, while PHC supports 3.11+.

---

## 2. Installation

The server can be installed in two ways:

- **Docker:** image `ghcr.io/matter-js/matterjs-server` (bundles Node.js).
- **npm:** `npm install matter-server` (needs Node.js 22.13+, 24.x
  recommended).

Both use the same data directory, so paired devices carry over when
switching.

The steps below describe the Docker installation. Steps marked *(Docker)*
are specific to it; the others apply to both. With npm, replace them by:
install Node.js, `npm install matter-server`, and run the server as a
systemd service with `NOBLE_BINDINGS=dbus` (needs the optional `dbus-next`
package) and `--bluetooth-adapter 0`.

Exact commands are in the matterjs-server docs (see References). These steps
only point to the right sections.

1. **Prepare the OS.** Install Raspberry Pi OS Lite (64-bit) and connect via
   Ethernet. Check the host against `docs/os_requirements.md`.
   - Update the system first (`sudo apt update && sudo apt full-upgrade`,
     then reboot). The server expects a recent kernel, BlueZ and
     NetworkManager.
   - Use Ethernet because the Pi 4's Wi-Fi and Bluetooth share one radio, and
     Bluetooth commissioning is more reliable without Wi-Fi traffic on it.
   - Keep the Pi, border routers and devices on the same flat LAN (no VLAN
     separation, no mDNS reflectors).
   - On a 1-2 GB Pi 4, keep an eye on RAM with Docker, Node.js and PHC
     running together.
2. **Check IPv6.** Matter always needs IPv6 (link-local addresses and mDNS).
   Both settings below are the default, so this is normally a check only:
   - IPv6 enabled: `ip -6 addr show eth0` must list an `fe80::` address.
   - Forwarding off: `sysctl net.ipv6.conf.all.forwarding` must print `0`.
     Only if it prints `1`: find the uncommented line that sets it
     (`grep -rn "ipv6.*forwarding" /etc/sysctl.conf /etc/sysctl.d/`; lines
     starting with `#` are inactive) and remove it, or override it in
     `/etc/sysctl.d/99-matter.conf`, then run `sudo sysctl --system`.
   - Thread only: the host must also accept router advertisements with route
     information from the border router, or it cannot reach Thread devices.
     NetworkManager (Raspberry Pi OS default) does this itself from version
     1.42: check with `nmcli --version`, and keep `ipv6.method` at `auto`.
     Without NetworkManager, set `accept_ra=1` and
     `accept_ra_rt_info_max_plen=64` on the interface.
3. **Raise the conntrack UDP timeout.** Docker (and firewalls such as ufw)
   load the `nf_conntrack` kernel module, whose default UDP timeout (120 s)
   silently drops reports from sleepy battery devices. The module is loaded
   at boot so the setting can be applied before Docker starts.

   Check first:

```
sysctl net.netfilter.nf_conntrack_udp_timeout_stream
grep -rn nf_conntrack /etc/modules-load.d/ /etc/sysctl.conf /etc/sysctl.d/
```

   Done if the first prints `3600` and the second shows both an
   `nf_conntrack` line in `/etc/modules-load.d/` and an uncommented
   `nf_conntrack_udp_timeout_stream = 3600` line. Otherwise run:

```
echo nf_conntrack | sudo tee /etc/modules-load.d/matter-conntrack.conf
echo "net.netfilter.nf_conntrack_udp_timeout_stream = 3600" | \
  sudo tee /etc/sysctl.d/99-matter-conntrack.conf
sudo modprobe nf_conntrack
sudo sysctl --system
```

   Check (also after a reboot):
   `sysctl net.netfilter.nf_conntrack_udp_timeout_stream` must print `3600`.
4. *(Docker)* **Install Docker** following Docker's Debian instructions,
   section "Install using the apt repository":
   https://docs.docker.com/engine/install/debian/
   - Use the Debian page, not the "Raspberry Pi OS" page, which covers only
     32-bit. Check with `dpkg --print-architecture` (must print `arm64`).
   - Check first: `docker --version` and `docker compose version` show
     whether Docker and the Compose plugin are already installed.
   - Optional post-install steps (e.g. running `docker` without `sudo`):
     https://docs.docker.com/engine/install/linux-postinstall/
5. **Enable Bluetooth on the host.** Check:

```
systemctl is-enabled bluetooth
systemctl is-active bluetooth
bluetoothctl show | grep Powered
```

   Done if these print `enabled`, `active` and `Powered: yes`. Otherwise:
   - Service not enabled/active: `sudo systemctl enable --now bluetooth`
   - `Powered: no`: `bluetoothctl power on`
   - "No default controller available": `rfkill list`; if Bluetooth is
     "Soft blocked: yes", run `sudo rfkill unblock bluetooth`.
6. *(Docker)* **Create the data directory, owned by UID 1000.** The
   container runs unprivileged as UID 1000 (on Raspberry Pi OS usually the
   first user, e.g. `pi`; check with `id -nu 1000`). The directory holds the
   fabric keys, so it is readable by its owner only. Layout used here:
   `/opt/matter-server/` for the compose file, `data/` below it.

   Check first: `ls -ldn /opt/matter-server/data` must show `drwx------`
   and `1000 1000`. On a first install it reports "No such file or
   directory"; in that case, or if the values differ, run:

```
sudo mkdir -p /opt/matter-server/data
sudo chown -R 1000:1000 /opt/matter-server/data
sudo chmod -R u+rwX,go-rwx /opt/matter-server/data
```

   See also "Permission Issues" in `docs/docker.md`.
7. *(Docker)* **Install and start the server.** There is no separate
   install: Docker downloads the server image on the first start.

   Check first: `sudo docker ps --filter name=matterjs-server` lists the
   container if it is already running.

   Create `/opt/matter-server/docker-compose.yml`:

```yaml
services:
  matterjs-server:
    container_name: matterjs-server
    image: ghcr.io/matter-js/matterjs-server:1.5.4  # pinned, not "stable"
    restart: unless-stopped
    network_mode: host            # required for mDNS and IPv6
    read_only: true
    volumes:
      - ./data:/data
      - /run/dbus:/run/dbus:ro    # Bluetooth via BlueZ (D-Bus backend)
    environment:
      - NOBLE_BINDINGS=dbus
      - BLUETOOTH_ADAPTER=0
      - LISTEN_ADDRESS=127.0.0.1  # see step 8
      - TZ=Europe/Zurich          # your time zone
```

   Then download the image and start it:

```
cd /opt/matter-server
sudo docker compose pull
sudo docker compose up -d
sudo docker compose logs -f
```

   - Without working Bluetooth (step 5), remove the `/run/dbus` line and the
     two Bluetooth variables; commission via multi-admin instead.
   - Don't use the raw HCI backend (`NOBLE_BINDINGS=hci`): it needs root and
     disables container isolation.
   - Update later: change the tag, then `sudo docker compose pull` and
     `sudo docker compose up -d`. Releases:
     https://github.com/matter-js/matterjs-server/releases
8. **Secure access.** The WebSocket API has no authentication: anyone who can
   reach it can control and commission devices. The server binds to all
   interfaces by default.
   - If PHC runs on the same Pi, set `LISTEN_ADDRESS=127.0.0.1` and reach the
     dashboard via an SSH tunnel.
   - *(Docker)* Put an IP literal first in `LISTEN_ADDRESS`; the health
     check cannot resolve interface names.
   - See the security note in `docs/docker.md` (`LISTEN_ADDRESS`,
     `PRIMARY_INTERFACE`).
9. **Back up the data directory** (`/data` in Docker). It holds the
   fabric's root CA keys. Losing it means re-commissioning every device;
   leaking it gives others control of them. Keep backups protected.
10. **Verify.**
    - On the Pi: `curl -sI http://127.0.0.1:5580/` must return
      `HTTP/1.1 200 OK`.
    - Dashboard from another computer: `ssh -L 5580:localhost:5580 pi@<pi>`,
      then open `http://localhost:5580/`.
    - WebSocket API (for PHC): `ws://127.0.0.1:5580/ws`. Once the command
      line tool of section 5 is set up, `matter list` connects the same
      way PHC does and prints the paired nodes; a failure prints the reason.
      To watch a full exchange, use the dashboard with the browser's
      developer tools (Network -> WS -> `/ws` -> Messages).

Optional settings documented in `docs/docker.md` (environment variables;
the npm CLI options are in the README): `LOG_LEVEL`, `LOG_FILE`,
`NODE_OPTIONS` memory limit (only if the host runs low on RAM), and `TZ`.

---

## 3. Commissioning - steps

Commands are sent via the dashboard or with the command-line tool of
section 5 (`matter raw <command> '<json>'`).

1. **Provide network credentials once, before the first Bluetooth pairing.**
   - Wi-Fi devices: `set_wifi_credentials`, using a **2.4 GHz** SSID.
   - Thread devices: `set_thread_dataset`, with the operational dataset from
     your own OTBR. Not possible with Apple/Google border routers; use
     multi-admin sharing instead (step 6).
2. **Put the device in pairing mode.** Use a new or factory-reset device and
   keep it within about 2 m of the Pi.
3. **Start commissioning.** Send `commission_with_code` with the QR payload
   (`MT:...`) or the 11- or 21-digit manual code, or use the dashboard. Allow
   a few minutes for it to complete. Devices already on the IP network can use
   `commission_on_network` instead.
4. **Wait for confirmation.** After `start_listening`, your client receives a
   `node_added` event. The node then appears in `get_nodes`, and you control
   it with `device_command`.
5. **Keep internet access available during pairing.** It is used for
   attestation checks against the CSA DCL; the bundled seed data may be
   outdated (offline operation is possible via `DCL_OFFLINE`).
6. **Multi-admin.**
   - Into the server: pair the device in Apple Home or Google Home, create a
     pairing code there, and send `commission_with_code` with
     `network_only: true`.
   - Out of the server: `open_commissioning_window` produces a code for
     adding a server-paired device to Apple Home or Google Home.

Command and argument names: see the Python client in the server repository
(`python_client/matter_server/client/client.py`).

---

Troubleshooting hints:

- Most commissioning failures are a missing internet connection (device
  attestation) or IPv6/mDNS not working between the Pi and the device once
  it has joined the network. For Thread devices check the
  route-information settings in section 2.
- Battery devices dropping their subscription every 15-30 minutes: usually the
  conntrack UDP timeout (section 2).

---

## 4. Configuring PHC

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

See [`examples/matter_system.yaml`](../examples/matter_system.yaml) and
[`examples/devices/matter_devices.yaml`](../examples/devices/matter_devices.yaml).

### Naming a node

`node:` identifies the Matter node, on the device or, to group several
nodes in one PHC device, on each endpoint (an endpoint's own `node:` wins).
It is one of:

- the node's **label**, a text stored on the device itself. Set it once
  with `matter label 3 "Living lamp"`; unlike the node id it survives
  re-pairing the device.
- its **serial number**, as `matter get <node> 0/40/15` shows it;
- its numeric **node id**, as `matter list` shows it (changes whenever
  the device is paired again).

A label or serial number is a glob and must match exactly one node:
matching none, or several, is a config error naming the candidates.
Matching ignores case unless the endpoint sets `match_case: true`.

### Endpoints

`endpoint_profile` selects what an endpoint reads and writes. `endpoint:`
names the Matter endpoint on the node (default 1; `matter list` shows
them).

| Profile | Attribute | Value | Writable |
|---|---|---|---|
| `switch` | On/Off `6/0` | bool | yes |
| `dimmer` | Level Control `8/0` | 0-100 % | yes |
| `power` | Electrical Power `144/8` | W | no |
| `voltage` | Electrical Power `144/4` | V | no |
| `current` | Electrical Power `144/5` | A | no |
| `energy` | Electrical Energy `145/1` | kWh, imported | no |
| `temperature` | `1026/0` | °C | no |
| `humidity` | `1029/0` | % | no |
| `motion` | Occupancy `1030/0` | bool | no |
| `contact` | Boolean State `69/0` | bool, true = closed | no |
| `battery` | Power Source `47/12` | % | no |

An unavailable node, or a value the node doesn't report, reads `None`.
The server pushes every change, so a value is typically fresh within a
tick; the module also re-reads everything every `resync_interval` (5m) and
reconnects by itself after the server restarts.

The read side of the `switch`, `power`, `voltage` and `energy` profiles
is checked against a real plug (LEDVANCE Matter plug). Writing, `dimmer`
and the sensor profiles follow the Matter specification and haven't been
tried on hardware yet; a device that reports another unit or cluster needs
its own attribute, as below.

### Any other attribute

An endpoint can name an attribute directly, without a profile:

```yaml
- key: tvoc
  attribute: "1070/0"        # "cluster/attribute", decimal
  type: float
  scale: 0.001               # optional: wire unit -> endpoint unit
```

`field: <tag>` reads one member of a structured value (e.g. `field: 0`
for the energy in `145/1`), and `mask: <bits>` reads a bitmap as true
when any of those bits is set. A writable endpoint declares
`commands: {true: "On", false: "Off"}`, or `level_command:` for a
number (divided by `scale`); one that declares neither is read-only.

---

## 5. Command-line tool

[`demo.py`](../phc/devices/matter/demo.py) drives the same client PHC
uses, on its own. It is the quickest way to see what the server has
paired, to pair a first device and to find the right `node:`. Define a
shortcut once (add the line to `~/.bashrc` to keep it):

```
alias matter='/opt/phc/.venv/bin/python /opt/phc/phc/devices/matter/demo.py'
```

Nodes and attributes:

```
matter list
matter get "Living lamp"
matter get "Living lamp" 1/6/0
matter on "Living lamp"
matter off "Living lamp"
matter label 3 "Living lamp"
matter listen
```

- `list`: every node with label, product, availability, and each
  endpoint's clusters.
- `get NODE`: all cached attributes of a node; with a path
  (`endpoint/cluster/attribute`) just that value.
- `listen`: prints server events until Ctrl+C; operate a device by hand
  or from the dashboard to see its `attribute_updated` events.

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

- `code`: the number printed next to the QR code (without dashes), or the
  `MT:...` text obtained by scanning the QR code with a QR scanner app.
- `parse_onboarding_payload`: decodes a pairing code (vendor/product ID,
  discriminator) without commissioning; checks the code before use.
- `discover`: lists devices currently in pairing mode on the IP network.
- `read_attribute`: asks the device itself; `get` reads the server's cache.
- `ping_node`: checks the device is reachable over IP, per address.
- `interview_node`: re-reads all endpoints and attributes, e.g. after a
  firmware update.
- `open_commissioning_window`: returns a pairing code for adding the
  device to Apple Home or Google Home as well (multi-admin).
- `remove_node`: removes the device from the fabric; it has to be
  commissioned again afterwards.
- `diagnostics`: server info plus all nodes and recent events, for bug
  reports.

Command and argument names: the Python client in the server repository,
`python_client/matter_server/client/client.py`.

---

## References

- matterjs-server (official): https://github.com/matter-js/matterjs-server
  - Docker guide: `docs/docker.md`
  - OS requirements: `docs/os_requirements.md`
  - CLI options: `README.md`; all environment variables: `docs/docker.md`
  - Python client: `python_client/` (`matter-python-client`)
- Docker image: `ghcr.io/matter-js/matterjs-server`
- Archived python-matter-server: https://github.com/matter-js/python-matter-server
- Home Assistant Matter integration: https://www.home-assistant.io/integrations/matter/
- Home Assistant Thread integration: https://www.home-assistant.io/integrations/thread/
