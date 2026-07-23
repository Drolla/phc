"""Plugin registry: Device/Endpoint/Action classes register themselves here by
name (via @register_module/@register_endpoint_kind/@register_task_kind), and
discover_modules() imports every modules/<name>/device.py so that registration
happens automatically at startup."""

import importlib
import pkgutil

from core.endpoint import Endpoint

_device_modules: dict[str, type] = {}
_endpoint_kinds: dict[str, type[Endpoint]] = {}
_task_kinds: dict[str, type] = {}


def register_module(name: str):
    """Class decorator: registers a Device subclass under a module name,
    so system YAML can reference it via `module: <name>`."""
    def decorator(cls):
        _device_modules[name] = cls
        return cls
    return decorator


def register_endpoint_kind(kind: str):
    """Class decorator: registers an Endpoint subclass under a `kind` name,
    so module.yaml endpoint entries can reference it via `kind: <kind>`."""
    def decorator(cls):
        _endpoint_kinds[kind] = cls
        return cls
    return decorator


def get_device_class(module_name: str) -> type:
    """Return the Device subclass registered under `module_name`."""
    try:
        return _device_modules[module_name]
    except KeyError:
        raise KeyError(f"no device module registered as {module_name!r}") from None


def get_endpoint_class(kind: str | None) -> type[Endpoint]:
    """Return the Endpoint subclass registered under `kind`, or the base
    Endpoint class if `kind` is None or unregistered."""
    if kind is None:
        return Endpoint
    return _endpoint_kinds.get(kind, Endpoint)


def register_task_kind(kind: str):
    """Class decorator: registers an Action subclass under a `kind` name,
    so task YAML action entries can reference it via `kind: <kind>`."""
    def decorator(cls):
        _task_kinds[kind] = cls
        return cls
    return decorator


def get_task_kind_class(kind: str) -> type:
    """Return the Action subclass registered under `kind`."""
    try:
        return _task_kinds[kind]
    except KeyError:
        raise KeyError(f"no task action kind registered as {kind!r}") from None


def discover_modules(package_name: str = "modules") -> None:
    """Import every modules/<name>/device.py so its @register_module decorator
    runs and populates the registry. Idempotent: re-importing is a no-op."""
    package = importlib.import_module(package_name)
    for _finder, name, is_pkg in pkgutil.iter_modules(package.__path__, package_name + "."):
        if not is_pkg:
            continue
        try:
            importlib.import_module(name + ".device")
        except ModuleNotFoundError:
            continue
