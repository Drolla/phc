"""The extension contract: what PHC calls on a configured extension
instance, and when.

An extension is a package with an `extension.yaml` and an `extension.py`
exposing `configure()`, which returns an instance object. That object may
implement any of four lifecycle hooks, all optional -- an extension that
only registers a task action kind (e.g. mail_alert) implements none.

The hooks, in the order they first run:

    on_bind(system)     once, at the end of load_system(), synchronously.
                        The System is fully built here -- devices, tasks,
                        and every OTHER extension instance -- which is
                        what configure() cannot see, since it runs while
                        the registry is still being filled. Use it to
                        resolve references to other instances (see
                        extensions/web_ui) or to register tasks
                        (extensions/timer). Raising here aborts the load,
                        which is the point: a bad reference should be a
                        startup error, not a runtime surprise.

    on_start(devices)   once, awaited, before the first tick, on the
                        Scheduler's own event loop. For a long-lived
                        resource that needs a running loop -- an aiohttp
                        server, a connection. Runs only under
                        run_forever(), never around a bare tick().

    on_tick(devices)    once per tick, synchronously, AFTER that tick's
                        state has been committed -- so it sees this
                        tick's values and events, unlike a task, which
                        sees the previous tick's (see
                        docs/configuration.md#tasks-and-the-heartbeat).

    on_stop(devices)    once, awaited, after the last tick, on the same
                        loop as on_start.

on_start/on_tick/on_stop are each isolated: one instance raising is
logged and does not prevent the others from running, or stop the tick.
on_bind is deliberately NOT isolated.

Hook names are checked at load time (see check_lifecycle_hooks) because
they are discovered by name: a method called `on_tik` is not a hook, it
is simply never called, and nothing would otherwise say so.
"""

import difflib
from typing import Protocol, runtime_checkable

from phc.core.errors import ConfigError

# In the order they first run. Also the order load_system collects them.
LIFECYCLE_HOOKS = ("on_bind", "on_start", "on_tick", "on_stop")

# How close a method name has to be to a real hook before it is treated as
# a typo rather than an unrelated method. 0.8 catches on_tik/on_tickk/
# on_starts while leaving a deliberate on_message or on_connect alone.
_TYPO_CUTOFF = 0.8


@runtime_checkable
class Extension(Protocol):
    """Structural type for a configured extension instance.

    Every hook is optional, so this is documentation and a type-checking
    aid rather than something to inherit: PHC looks for the methods, not
    for a base class, and an extension that implements none is valid.
    """

    def on_bind(self, system) -> None:
        """See module docstring."""

    async def on_start(self, devices: dict) -> None:
        """See module docstring."""

    def on_tick(self, devices: dict) -> None:
        """See module docstring."""

    async def on_stop(self, devices: dict) -> None:
        """See module docstring."""


def check_lifecycle_hooks(instance: object, instance_key: str) -> None:
    """Raise ConfigError if `instance` has a method that looks like a
    misspelled lifecycle hook.

    Hooks are found by name, so `on_tik` is not a broken hook -- it is not
    a hook at all, and the extension simply never ticks, silently, until
    someone notices the thing it was supposed to do never happens. This
    turns that into a startup error naming both the method and what it was
    probably meant to be.

    Deliberately conservative: only a close match to a real hook name is
    reported, so an extension is free to have its own `on_message` or
    `on_connect`."""
    known = set(LIFECYCLE_HOOKS)
    for name in dir(instance):
        if name.startswith("__") or name in known:
            continue
        if not callable(getattr(instance, name, None)):
            continue
        matches = difflib.get_close_matches(name.lower(), LIFECYCLE_HOOKS, n=1,
                                             cutoff=_TYPO_CUTOFF)
        if matches:
            raise ConfigError(
                f"extension instance {instance_key!r}: method {name!r} looks like a "
                f"misspelling of the lifecycle hook {matches[0]!r}. Lifecycle hooks are "
                f"found by name, so this one would never be called; rename it to "
                f"{matches[0]!r}, or to something less similar if it is unrelated.")


def collect_hook(instances: dict, hook: str) -> list:
    """Every instance's bound `hook` method, in registry order, for the
    instances that implement it."""
    return [getattr(instance, hook) for instance in instances.values()
            if hasattr(instance, hook)]
