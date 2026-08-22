"""debug_portal extension: a live view of the scheduler's internals.

Explicitly-enabled, loopback-default. Shows the scheduler's task queue,
the device poll queue, and every selected endpoint's
state/event/last_valid_state, pushed once per tick over Server-Sent
Events. See phc/extensions/debug_portal/extension.py."""
