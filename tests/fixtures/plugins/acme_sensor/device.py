"""A device module living entirely outside the phc package.

Exists to prove that a plugin shipped by someone other than PHC works
exactly like a bundled one: discovered from a `plugin_paths:` directory,
its module.yaml found next to its own code, and usable from a system YAML
as `module: acme_sensor` with no further registration.
"""

from phc.core.device import Device
from phc.core.registry import register_module


@register_module("acme_sensor")
class AcmeSensorDevice(Device):
    """Reports a fixed reading, offset by this instance's `offset` param."""

    def setup(self):
        self._offset = float(self.params.get("offset", 0.0))

    def receive(self) -> dict:
        return {"temperature": 20.0 + self._offset}
