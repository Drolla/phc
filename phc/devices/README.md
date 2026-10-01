# Bundled Device Modules

One subdirectory per device module, each a `device.py` (a `Device`
subclass) plus a `module.yaml` describing its parameters and endpoints.
They are discovered automatically at startup — nothing here is registered
by hand.

## Adding a New One

**Do not start by copying one of the modules in this directory.** They are
real modules shaped by their own protocols, and each shows only part of the
pattern. Start from the template instead:

- [`examples/device-template/`](../../examples/device-template/) — a
  complete, working module written to be copied. Demonstrates the whole
  surface in one place, runs with no hardware or network, and has its own
  test so it cannot rot.
- [`docs/developer/writing-a-device-module.md`](../../docs/developer/writing-a-device-module.md)
  — the reference explaining every piece of it.
- [`.agentic_flowspace/skills/agentic-adding-a-device-module.md`](../../.agentic_flowspace/skills/agentic-adding-a-device-module.md)
  — the end-to-end workflow, for building a module with an AI assistant.

Once you know the pattern, these modules are useful as real-world points on
the spectrum: `virtual/` and `host/` for the minimal shape, `sun/` and
`system_monitor/` for synchronous computation with no network,
`meteoswiss/`, `open_meteo/` and `waveplus_bridge/` for cached async HTTP,
`zway/` for `endpoint_parameters:`, an endpoint profile library, and a
long-lived push-driven connection shared between a module's devices, and
`viessmann/` for a module built on a third-party vendor library -- the one
here whose I/O is synchronous, bridged onto a worker thread, with a
companion CLI for discovering what a given installation can report.

A module does not have to live here at all — see
[Shipping a Module Outside PHC](../../docs/developer/writing-a-device-module.md#shipping-a-module-outside-phc).
