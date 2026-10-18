"""Devices: the Device base class, and loading `modules:`/`devices:`.

device.py is the runtime API device modules build on; config.py
builds a Device tree from a system YAML. Re-exports device.py.
"""

from phc.core.device.device import Device, _write_collector
