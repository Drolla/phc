"""EmulatedDevice: an in-memory device standing in for real hardware.

Injected read/write latency (like `virtual` but slower/flakier), plus
optional self-generated readings for endpoints with no real hardware to
write to -- see receive()/`simulate:`."""

import random
import time

from phc.core.device import Device
from phc.core.intervals import parse_duration
from phc.core.registry import register_module

_SIMULATE_KINDS = ("toggle", "drift")


@register_module("emulated_device")
class EmulatedDevice(Device):
    """In-memory device like `virtual` but with injected latency and,
    optionally, endpoints that generate their own readings.

    For testing scheduler behavior under slow I/O (delay includes
    jitter -- natural variation around a baseline -- and random spikes,
    i.e. occasional flakiness), and for examples that stand in for a
    real, read-only device (a sensor, a battery gauge): declaring
    `simulate:` on such an endpoint lets it report plausible values on
    its own, since a read-only endpoint has no writable path a task or
    the web UI could otherwise drive by hand.
    """

    def setup(self):
        """Load read/write latency profiles and simulation state."""
        self._pending: dict = {}
        self._read = self._load_direction("read")
        self._write = self._load_direction("write")
        # Per-endpoint runtime state a `simulate:` generator carries across
        # ticks -- currently only `toggle`'s "when did this last turn on",
        # keyed by endpoint key so multiple simulated endpoints don't share
        # state.
        self._simulate_state: dict = {}

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
        """Sleep for a simulated read delay, then report writes plus any
        self-generated readings.

        An endpoint with `simulate:` generates its own next value UNLESS
        this same tick also buffered an explicit write for it (an
        endpoint can be both writable and simulated -- e.g. a value a
        task nudges directly on top of its normal drift -- and the
        explicit write should win for the tick it happens)."""
        time.sleep(self._next_delay(self._read))
        pending, self._pending = self._pending, {}
        generated = {
            key: self._simulate(ep, ep.params["simulate"])
            for key, ep in self.endpoints.items()
            if ep.params.get("simulate") and key not in pending
        }
        return {**pending, **generated}

    def transmit(self, state: dict) -> None:
        """Sleep for a simulated write delay, then buffer the write."""
        time.sleep(self._next_delay(self._write))
        self._pending.update(state)

    def _simulate(self, ep, spec: dict):
        """Generate this endpoint's next reading per its `simulate:` spec."""
        kind = spec.get("kind")
        if kind == "toggle":
            return self._simulate_toggle(ep, spec)
        if kind == "drift":
            return self._simulate_drift(ep, spec)
        raise ValueError(
            f"{self.qualified_id}: endpoint {ep.key!r} has simulate kind {kind!r}, "
            f"expected one of {_SIMULATE_KINDS}")

    def _simulate_toggle(self, ep, spec: dict):
        """Flip a bool endpoint on with some probability, then clear it
        again after `auto_clear_after` -- a motion sensor or a momentary
        tag-reader event, not a switch someone leaves set. A `values:`
        mapping (e.g. {false: "clear", true: "motion"}), if declared, is
        display-only -- to_text() resolves it independently of the raw
        True/False this returns."""
        turned_on_at = self._simulate_state.get(ep.key)
        if turned_on_at is not None:
            if time.monotonic() - turned_on_at < parse_duration(spec.get("auto_clear_after", 0)):
                return True
            del self._simulate_state[ep.key]
            return False
        if random.random() < float(spec.get("on_probability", 0.0)):
            self._simulate_state[ep.key] = time.monotonic()
            return True
        return False

    def _simulate_drift(self, ep, spec: dict):
        """Random-walk a numeric endpoint's current value by up to `step`,
        clamped to its own declared min/max if it has them."""
        step = float(spec.get("step", 1.0))
        current = ep.get()
        if current is None:
            current = 0.0
        value = current + random.uniform(-step, step)
        if ep.min is not None:
            value = max(ep.min, value)
        if ep.max is not None:
            value = min(ep.max, value)
        return int(round(value)) if ep.value_type == "int" else value

    @staticmethod
    def _next_delay(cfg: dict) -> float:
        """Return this call's delay.

        Base latency (with jitter), or spike latency with
        spike_probability chance."""
        if random.random() < cfg["spike_probability"]:
            return EmulatedDevice._jittered(cfg["spike_latency"], cfg["spike_jitter"])
        return EmulatedDevice._jittered(cfg["latency"], cfg["jitter"])

    @staticmethod
    def _jittered(base: float, jitter: float) -> float:
        """Return base randomly varied by +/- jitter fraction."""
        if jitter <= 0:
            return base
        return random.uniform(base * (1 - jitter), base * (1 + jitter))
