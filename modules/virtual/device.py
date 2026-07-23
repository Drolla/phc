"""VirtualDevice: an in-memory device with no real hardware, used for testing and examples."""

from core.device import Device
from core.registry import register_module


@register_module("virtual")
class VirtualDevice(Device):
    """In-memory device with no real hardware. transmit() and receive() are
    two ends of the same buffer: a write staged by transmit() is handed back
    on the next fetch()'s receive() call, then committed by update_state()."""

    def setup(self):
        """Initialize the empty write buffer."""
        self._pending: dict = {}

    def receive(self) -> dict:
        """Return and clear whatever was written since the last receive()."""
        pending, self._pending = self._pending, {}
        return pending

    def transmit(self, state: dict) -> None:
        """Buffer a write for the next receive() to pick up."""
        self._pending.update(state)
