"""emulated_device module: emulates a real device's imperfect behavior.

Injected read/write latency (for demonstrating and testing how a slow
device affects the scheduler) plus, via an endpoint's `simulate:` field,
self-generated readings for endpoints that have no real hardware behind
them to write to."""
