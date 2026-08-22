"""Clock: the pair of clock readings that one scheduler tick is driven with."""

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Now:
    """Both clocks read at one instant, carried as a single value.

    `wall` is time.time(): absolute times of day only -- a task's due_time,
    an endpoint's update_time, a logged timestamp. `mono` is
    time.monotonic(): every interval -- a device's update:, a task's
    min_interval, endpoint age, history sampling. See
    phc.core.scheduler.Scheduler's class docstring for why the split exists
    and the NTP/DST bug it fixed.

    One value rather than two parameters so that `now` names one thing
    everywhere and each use site names the clock it reads (`now.mono`,
    `now.wall`) instead of relying on a parameter's position or suffix.

    Frozen: a tick's timestamps are a fact about that tick, and every pass
    and hook it reaches must agree on them.
    """

    wall: float
    mono: float

    @classmethod
    def capture(cls) -> "Now":
        """Read both real clocks -- how the live scheduler starts a tick.

        Reads each clock exactly once. Callers that need a tick's time more
        than once must reuse the returned value rather than capture again:
        a second reading is both a different instant and, for the heartbeat
        grid, the wrong one (see Scheduler._run_async)."""
        return cls(time.time(), time.monotonic())

    @classmethod
    def at(cls, instant: float) -> "Now":
        """Both clocks set to `instant`: one synthetic timeline.

        For a caller driving ticks at explicit times (the test suite). Sound
        only because every clock reading is used as a difference against
        another reading of the SAME clock, so a shared arbitrary origin
        stays self-consistent."""
        return cls(instant, instant)

    @classmethod
    def coerce(cls, value: "Now | float") -> "Now":
        """Return `value` unchanged, or expand a bare number via at().

        The one place the single-number shorthand is expanded, so every
        entry point accepting it (Scheduler.tick, Task.run, build_snapshot)
        shares one definition of what one number means."""
        return value if isinstance(value, Now) else cls.at(value)
