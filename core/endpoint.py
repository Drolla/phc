"""Endpoint: a single piece of readable/writable device state."""

import time


class Endpoint:
    """Holds one piece of device state with a two-phase set/update_state model.

    A new value written via set() is staged as the "next" state; it only
    becomes the active state (visible via get()) once update_state() runs,
    which the PHC scheduler calls once per device cycle after fetch().
    """

    kind: str = "generic"

    def __init__(self, key: str, *, readable: bool = True, writable: bool = False,
                 parameters: dict | None = None, description: str = ""):
        self.key = key
        self.readable = readable
        self.writable = writable
        # Module-authored facts about what this endpoint kind means (e.g.
        # which CSV column backs it). An instance may add or override
        # individual keys (merged per-key in config._merge_endpoints).
        self.parameters = parameters or {}
        self.description = description

        self._next_state = None
        self._state = None
        self._last_valid_state = None
        self._event = None
        self._update_time = 0.0

    def set(self, new_state):
        """Stage `new_state` as the next value; not visible via get() until update_state()."""
        self._next_state = new_state

    def get(self):
        """Return the current (last-committed) value."""
        return self._state

    def get_event(self):
        """Return the value that changed on the most recent update_state(), or None."""
        return self._event

    def get_update_time(self):
        """Return the time.time() of the most recent update_state() that changed the value."""
        return self._update_time

    def update_state(self):
        """Commit the staged value: promote _next_state to _state and compute this
        tick's change event (see class docstring for the two-phase model)."""
        self._event = None
        if self._next_state != self._state:
            if self._next_state not in (None, ''):
                if self._next_state != self._last_valid_state:
                    self._event = self._next_state
                    self._last_valid_state = self._next_state
            self._state = self._next_state
            self._update_time = time.time()

    state = property(get, set)
    event = property(get_event)
    update_time = property(get_update_time)
