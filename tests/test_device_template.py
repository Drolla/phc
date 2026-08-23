"""The device-module template in examples/device-template/ must keep working.

The template is code people copy, so a template that has quietly rotted
against a changed Device API is worse than none at all -- it teaches the
broken thing. Loading it through `plugin_paths:` and driving it with a real
Scheduler is what keeps it honest, and it exercises the out-of-tree plugin
path end to end at the same time.

This file is also the template's companion test template: the workflow in
.agentic_flowspace/skills/agentic-adding-a-device-module.md points at it as
the shape to copy for a new module's own tests.
"""

import asyncio
from pathlib import Path

from phc.core.config import load_system
from phc.core.scheduler import Scheduler
from tests.conftest import fetch_sync

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "examples" / "device-template"
EXAMPLE_SYSTEM = REPO_ROOT / "examples" / "device_template_system.yaml"


def _system(tmp_path, *, units=("hall",), simulate_failure=False, cache_time="10s"):
    """Build and load a system of device_template units behind one host.

    Every unit shares a host, so they exercise the shared cache; pass more
    than one to test coalescing.
    """
    devices = "\n".join(
        f"""
  - id: {unit}
    module: device_template
    host: 10.0.0.42
    unit: {unit}
    simulate_failure: {str(simulate_failure).lower()}"""
        for unit in units
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "system.yaml"
    path.write_text(f"""
heartbeat: 1s
plugin_paths: ["{TEMPLATE_DIR.as_posix()}"]
modules:
  device_template:
    cache_time: {cache_time}
devices:{devices}
""", encoding="utf-8")
    return load_system(path)


def _state(system):
    """The module's shared per-system state, as device.py stores it."""
    return next(iter(system.devices.values())).context["device_template"]


def test_template_reads_resolve_through_the_channel_endpoint_parameter(tmp_path):
    """The end-to-end read: each endpoint's declared `channel` selects a
    field of its own unit's readings, so two units behind one host report
    different values from a single payload."""
    system = _system(tmp_path, units=("hall", "cellar"))
    scheduler = Scheduler(system.devices)
    scheduler.tick(now=0.0)
    scheduler.close()

    assert system.devices["hall"].get("temperature") == 21.5
    assert system.devices["hall"].get("humidity") == 44.0
    assert system.devices["cellar"].get("temperature") == 12.0
    assert system.devices["cellar"].get("humidity") == 71.0


def test_template_endpoint_metadata_comes_from_module_yaml(tmp_path):
    """module.yaml really is being parsed -- unit and values mapping reach
    the endpoint, not just the raw numbers."""
    system = _system(tmp_path)
    device = system.devices["hall"]
    fetch_sync(device)
    device.update_state()

    assert device.get_text("temperature") == "21.5 °C"
    assert device.endpoint("relay").writable
    assert device.get_text("relay") == "off"      # values: {0: off, 1: on}


def test_template_write_round_trips_to_the_device(tmp_path):
    """A write reaches the hub and is read back on the next poll.

    set_text_async() rather than a bare set(): this template overrides only
    transmit_async(), and outside a scheduler tick a plain set() routes
    through the synchronous transmit() the template does not implement (see
    Device._emit). Inside a tick either is fine.
    """
    system = _system(tmp_path)
    device = system.devices["hall"]
    fetch_sync(device)
    device.update_state()
    assert device.get("relay") == 0

    asyncio.run(device.set_text_async("on", "relay"))
    fetch_sync(device)
    device.update_state()

    assert device.get("relay") == 1
    assert device.get_text("relay") == "on"


def test_template_reports_an_unreachable_host_as_unhealthy(tmp_path):
    """The failure path: the module catches its own I/O error, reports
    every endpoint as None, and still registers as unhealthy -- which only
    happens because it calls report_failure() explicitly."""
    system = _system(tmp_path, simulate_failure=True)
    device = system.devices["hall"]
    scheduler = Scheduler(system.devices)
    scheduler.tick(now=0.0)
    scheduler.close()

    assert device.get("temperature") is None
    assert not device.health.healthy
    assert "cannot reach Acme Hub" in device.health.last_error


def test_template_siblings_behind_one_host_coalesce_into_one_read(tmp_path):
    """Two devices due in the same tick fetch concurrently, so this
    exercises the shared cache's double-checked locking rather than plain
    sequential reuse."""
    system = _system(tmp_path, units=("hall", "cellar"))
    scheduler = Scheduler(system.devices)
    scheduler.tick(now=0.0)
    scheduler.close()

    assert _state(system).reads == 1, "both units should share one hub read"


def test_template_cache_expires(tmp_path):
    """cache_time: 0s always re-fetches -- and, because it makes every poll
    take the shared lock, it is also the configuration that would expose a
    lock bound to an already-closed event loop."""
    system = _system(tmp_path, units=("hall", "cellar"), cache_time="0s")
    scheduler = Scheduler(system.devices)
    scheduler.tick(now=0.0)
    scheduler.tick(now=1.0)
    scheduler.close()

    assert _state(system).reads == 2


def test_each_system_gets_its_own_shared_state(tmp_path):
    """The property module-scope state gets wrong.

    Two systems loaded in one process must not share a cache: the second
    does its own read rather than serving the first's payload, and gets its
    own asyncio.Lock -- which is what lets each bind to its own event loop
    instead of failing against a closed one.
    """
    first = _system(tmp_path / "a", cache_time="60s")
    second = _system(tmp_path / "b", cache_time="60s")

    for system in (first, second):
        scheduler = Scheduler(system.devices)
        scheduler.tick(now=0.0)
        scheduler.close()

    assert _state(first) is not _state(second)
    assert _state(first).reads == 1
    assert _state(second).reads == 1, "the second system must not reuse the first's cache"


def test_the_shipped_example_system_runs(tmp_path, monkeypatch):
    """examples/device_template_system.yaml is what a reader runs first, so
    it has to do more than load: one tick must produce real readings."""
    monkeypatch.chdir(tmp_path)
    system = load_system(EXAMPLE_SYSTEM)
    scheduler = Scheduler(system.devices)
    scheduler.tick(now=0.0)
    scheduler.close()

    assert system.devices["hall"].get("temperature") == 21.5
    assert system.devices["cellar"].get("temperature") == 12.0


def test_the_template_is_not_a_bundled_module():
    """It must be invisible to a normal installation: discoverable only
    from a plugin_paths: directory, never shipped inside the phc package."""
    assert not (REPO_ROOT / "phc" / "devices" / "device_template").exists()
    assert (TEMPLATE_DIR / "device_template" / "module.yaml").is_file()
