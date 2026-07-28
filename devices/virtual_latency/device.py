"""VirtualLatencyDevice: an in-memory device like `virtual`, with injected
read/write latency for demonstrating and testing scheduler behavior under slow I/O."""

import random
import time

from core.device import Device
from core.registry import register_module


@register_module("virtual_latency")
class VirtualLatencyDevice(Device):
    """In-memory device like `virtual` but with injected latency for testing
    scheduler behavior under slow I/O. Delay includes jitter (natural
    variation around a baseline) and random spikes (occasional flakiness).
    """

    def setup(self):
        """Load read/write latency profiles."""
        self._pending: dict = {}
        self._read = self._load_direction("read")
        self._write = self._load_direction("write")

    def _load_direction(self, prefix: str) -> dict:
        """Collect {prefix}_* latency params into one dict."""
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
        """Return this call's delay: base latency (with jitter) or spike
        latency with spike_probability chance."""
        if random.random() < cfg["spike_probability"]:
            return VirtualLatencyDevice._jittered(cfg["spike_latency"], cfg["spike_jitter"])
        return VirtualLatencyDevice._jittered(cfg["latency"], cfg["jitter"])

    @staticmethod
    def _jittered(base: float, jitter: float) -> float:
        """Return base randomly varied by +/- jitter fraction."""
        if jitter <= 0:
            return base
        return random.uniform(base * (1 - jitter), base * (1 + jitter))
