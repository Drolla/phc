"""VirtualLatencyDevice: an in-memory device like `virtual`, with injected
read/write latency for demonstrating and testing scheduler behavior under slow I/O."""

import random
import time

from core.device import Device
from core.registry import register_module


@register_module("virtual_latency")
class VirtualLatencyDevice(Device):
    """In-memory device like `virtual`, except receive()/transmit() sleep
    first -- for demonstrating how a slow (and occasionally flaky)
    physical device affects the scheduler, which runs each device's
    fetch() synchronously, one after another, so a slow device delays
    every other device's tick.

    Delay has natural variation around a normal value (+/- a jitter
    fraction), and can randomly "spike" to a much longer, itself-variable
    delay -- standing in for occasional real-world flakiness (a dropped
    packet, a retry, a momentarily overloaded gateway) on top of the
    everyday variation any real device has.
    """

    def setup(self):
        """Initialize the write buffer and this device's read/write latency profiles."""
        self._pending: dict = {}
        self._read = self._load_direction("read")
        self._write = self._load_direction("write")

    def _load_direction(self, prefix: str) -> dict:
        """Collect the `{prefix}_*` latency params (latency/jitter/spike_*)
        for one direction ("read" or "write") into a single dict."""
        return {
            "latency": float(self.params[f"{prefix}_latency"]),
            "jitter": float(self.params[f"{prefix}_jitter"]),
            "spike_probability": float(self.params[f"{prefix}_spike_probability"]),
            "spike_latency": float(self.params[f"{prefix}_spike_latency"]),
            "spike_jitter": float(self.params[f"{prefix}_spike_jitter"]),
        }

    def receive(self) -> dict:
        """Sleep for a simulated read delay, then return/clear the write buffer."""
        time.sleep(self._next_delay(self._read))
        pending, self._pending = self._pending, {}
        return pending

    def transmit(self, state: dict) -> None:
        """Sleep for a simulated write delay, then buffer the write."""
        time.sleep(self._next_delay(self._write))
        self._pending.update(state)

    @staticmethod
    def _next_delay(cfg: dict) -> float:
        """Pick this call's delay: usually the jittered base latency, with
        spike_probability chance of the (typically much larger) spike latency."""
        if random.random() < cfg["spike_probability"]:
            return VirtualLatencyDevice._jittered(cfg["spike_latency"], cfg["spike_jitter"])
        return VirtualLatencyDevice._jittered(cfg["latency"], cfg["jitter"])

    @staticmethod
    def _jittered(base: float, jitter: float) -> float:
        """Return `base` randomly varied by +/- `jitter` as a fraction of `base`."""
        if jitter <= 0:
            return base
        return random.uniform(base * (1 - jitter), base * (1 + jitter))
