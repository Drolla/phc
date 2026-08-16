"""Pylon Home Control (PHC): a YAML-configured home automation framework.

The package is deliberately thin at this level -- importing `phc` pulls in
nothing heavyweight, so a plugin (or a test) can `import phc` without
dragging in aiohttp, the device registry, or a configured logging tree.
The pieces live in the three subpackages:

- `phc.core` -- the framework itself: config loading, the device/endpoint
  model, tasks/actions, the scheduler.
- `phc.devices` -- the bundled device modules, discovered at startup by
  `phc.core.registry.discover_modules`.
- `phc.extensions` -- the bundled non-device extensions, discovered by
  `phc.core.registry.discover_extensions`.

`phc.cli` is the `phc` console command; `python -m phc` runs the same
entry point (see `__main__.py`).
"""

__version__ = "0.1.0"
