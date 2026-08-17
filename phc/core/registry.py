"""Plugin registry: Device/Endpoint/Action classes register themselves here by
name (via @register_module/@register_endpoint_kind/@register_task_kind).
discover_modules()/discover_extensions() import the modules that carry those
decorators, so registration happens automatically at startup.

A device module or extension can live in three places, all discovered the
same way and indistinguishable to a system YAML afterwards:

1. Bundled with PHC, under phc/devices/ or phc/extensions/.
2. In any installed distribution advertising an entry point in the
   "phc.devices"/"phc.extensions" group -- the standard way to ship a
   plugin::

       [project.entry-points."phc.devices"]
       mysensor = "mypkg.mysensor"

   The entry point NAME is what a config writes as `module: mysensor`; its
   VALUE is the package holding device.py and module.yaml.
3. In a directory listed under the system YAML's `plugin_paths:`, laid out
   exactly like phc/devices/ -- one subdirectory per module. For a local
   plugin not worth packaging.

Where a module's descriptor (module.yaml) lives follows from where its
class was defined -- see module_package() -- so nothing is registered
twice.
"""

import importlib
import importlib.util
import pkgutil
import sys
from importlib.metadata import entry_points
from pathlib import Path

from phc.core.endpoint import Endpoint

_device_modules: dict[str, type] = {}
_endpoint_kinds: dict[str, type[Endpoint]] = {}
_task_kinds: dict[str, type] = {}
# Extension name -> the package holding its extension.py/extension.yaml.
# Unlike a device module, an extension has no class decorator to register
# itself, so discovery records the mapping explicitly.
_extension_packages: dict[str, str] = {}

# Entry point groups a third-party distribution advertises plugins in.
DEVICE_ENTRY_POINT_GROUP = "phc.devices"
EXTENSION_ENTRY_POINT_GROUP = "phc.extensions"

# Results of each discovery scan already performed, keyed by what was
# scanned. discover_*() is called on every load_system(), and scanning is
# not free -- importlib.metadata.entry_points() alone walks the metadata of
# every installed distribution, ~40ms a call, which turns into minutes
# across a test suite that loads hundreds of configs. Imports are
# idempotent anyway, so nothing is gained by repeating a scan; a failed
# scan is not recorded, so it is retried (and re-raises) next time.
_discovered: dict[tuple, list[tuple[str, str]]] = {}


def _scan_once(key: tuple, scan) -> list[tuple[str, str]]:
    """Run `scan` unless this exact key has already been scanned."""
    if key not in _discovered:
        _discovered[key] = scan()
    return _discovered[key]


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


def module_package(module_name: str) -> str:
    """The package holding `module_name`'s device.py and module.yaml.

    Derived from where its Device subclass was actually defined
    ("mypkg.mysensor.device" -> "mypkg.mysensor") rather than assumed to
    sit under phc.devices -- which is what lets a module shipped by
    another distribution keep its descriptor next to its code."""
    cls = get_device_class(module_name)
    return cls.__module__.rsplit(".", 1)[0]


def extension_package(extension_name: str) -> str:
    """The package holding `extension_name`'s extension.py and
    extension.yaml, as recorded by discover_extensions()."""
    try:
        return _extension_packages[extension_name]
    except KeyError:
        raise KeyError(f"no extension registered as {extension_name!r}") from None


def _import_plugin_submodule(package: str, submodule: str) -> bool:
    """Import `package`.`submodule` if it exists; return whether it did.

    A package without the submodule is simply not a plugin -- reported as
    False, not an error.

    Deliberately does NOT swallow ImportError. This used to be
    `except ModuleNotFoundError: continue`, which could not tell "this
    package has no device.py" from "device.py exists, but its own
    `import serial` failed" -- so a plugin with a missing dependency
    silently did not exist, and the config naming it failed later, and
    confusingly, as an unknown module. find_spec answers the first
    question; anything raised by the import itself is the second, and
    propagates."""
    if importlib.util.find_spec(f"{package}.{submodule}") is None:
        return False
    importlib.import_module(f"{package}.{submodule}")
    return True


def _discover_in_package(package_name: str, submodule: str) -> list[tuple[str, str]]:
    """Import `<package_name>/<name>/<submodule>.py` for every subpackage.
    Returns the (name, package) pairs that turned out to be plugins."""
    package = importlib.import_module(package_name)
    found = []
    for _finder, qualified, is_pkg in pkgutil.iter_modules(package.__path__, package_name + "."):
        if not is_pkg:
            continue
        if _import_plugin_submodule(qualified, submodule):
            found.append((qualified.rsplit(".", 1)[1], qualified))
    return found


def _discover_in_entry_points(group: str, submodule: str) -> list[tuple[str, str]]:
    """Import the `submodule` of every package advertised in `group`.
    Returns the (entry point name, package) pairs that were plugins."""
    found = []
    for entry_point in entry_points(group=group):
        if _import_plugin_submodule(entry_point.value, submodule):
            found.append((entry_point.name, entry_point.value))
    return found


def _discover_in_paths(paths, submodule: str) -> list[tuple[str, str]]:
    """Import `<dir>/<name>/<submodule>.py` for every subpackage of every
    directory in `paths`, each laid out like phc/devices/.

    Each directory goes on sys.path first, so its subdirectories import as
    ordinary top-level packages."""
    found = []
    for directory in paths:
        directory = str(Path(directory))
        if directory not in sys.path:
            sys.path.insert(0, directory)
        for _finder, name, is_pkg in pkgutil.iter_modules([directory]):
            if not is_pkg:
                continue
            if _import_plugin_submodule(name, submodule):
                found.append((name, name))
    return found


def _discover_all(package_name: str, group: str, submodule: str,
                   plugin_paths) -> list[tuple[str, str]]:
    """Every (name, package) plugin pair reachable for one submodule kind,
    across all three sources. Each source is scanned at most once per
    process (see _scan_once)."""
    found = _scan_once((submodule, "package", package_name),
                        lambda: _discover_in_package(package_name, submodule))
    found = found + _scan_once((submodule, "entry_points", group),
                                lambda: _discover_in_entry_points(group, submodule))
    for directory in plugin_paths:
        found = found + _scan_once((submodule, "path", str(directory)),
                                    lambda d=directory: _discover_in_paths([d], submodule))
    return found


def discover_modules(package_name: str = "phc.devices", plugin_paths=()) -> None:
    """Import every device module's device.py so its @register_module
    decorator runs and populates the registry -- from the bundled package,
    from the "phc.devices" entry point group, and from any `plugin_paths:`
    directory. Idempotent, and cheap to call repeatedly."""
    _discover_all(package_name, DEVICE_ENTRY_POINT_GROUP, "device", plugin_paths)


def discover_extensions(package_name: str = "phc.extensions", plugin_paths=()) -> None:
    """Import every extension's extension.py (so its @register_task_kind and
    other decorators run) and record which package each extension name
    lives in -- see extension_package(). Mirrors discover_modules(), but for
    the extension.py convention."""
    for name, package in _discover_all(package_name, EXTENSION_ENTRY_POINT_GROUP,
                                        "extension", plugin_paths):
        _extension_packages[name] = package
