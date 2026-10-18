"""YAML loading for system configs.

A config file is expanded as text first (full-line `!include`, see
phc.core.config.expand), then parsed once with _ConfigLoader, which adds
the `!placeholder` tag to SafeLoader.

Purely syntactic -- this layer knows nothing about devices, endpoints or
tasks. It turns a file on disk into a plain nested dict/list structure,
and everything else in this package works from that.
"""

from pathlib import Path

import yaml

from phc.core.config.expand import _describe_yaml_error, expand_file
from phc.core.errors import ConfigError


class _ConfigLoader(yaml.SafeLoader):
    """SafeLoader plus `!placeholder`, for already-expanded config text."""


def load_yaml(path: str | Path):
    """Expand `path`'s `!include` lines and parse the result.

    Raises ConfigError for a broken include or invalid YAML, with
    positions mapped back to the original file:line."""
    expanded = expand_file(path)
    try:
        return yaml.load(expanded.text, Loader=_ConfigLoader)
    except yaml.MarkedYAMLError as exc:
        raise ConfigError(_describe_yaml_error(exc, expanded)) from None


def _inline_include_constructor(loader: yaml.SafeLoader, node: yaml.Node):
    """Reject an `!include` left as a tag, i.e. not on a line of its own."""
    raise yaml.constructor.ConstructorError(
        None, None,
        f"'!include {loader.construct_scalar(node)}' must be a line of its own, "
        f"indented where the included keys/items belong",
        node.start_mark)


class _Placeholder(str):
    """A scalar built from `!placeholder <example>`.

    Marks a value (a credential, another system's URL, ...) that a
    system YAML's author must replace with something real before the
    file is fit to run. Subclasses str so the example text underneath
    still reads/compares like a normal string to any code that isn't
    specifically checking for this type; see _find_placeholders, which
    load_system() calls right after parsing, before any of that code
    runs, so a leftover !placeholder always aborts the load rather than
    silently reaching a device/extension as a literal value like
    "<URL>"."""


def _placeholder_constructor(loader: yaml.SafeLoader, node: yaml.Node) -> _Placeholder:
    """Build a _Placeholder from a `!placeholder` scalar."""
    return _Placeholder(loader.construct_scalar(node))


def _find_placeholders(value, path: str = "") -> list[str]:
    """Recursively collect a path to every `!placeholder` value in `value`.

    `value` is the raw, just-parsed config tree. Path is dotted for
    mapping keys, bracketed for list entries -- using that entry's own
    `id`/`tag` when it has one, since "devices[2]" is far less useful
    than "devices[2].children['sensor_cellar']"."""
    found = []
    if isinstance(value, _Placeholder):
        found.append(path)
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_find_placeholders(item, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            label = item.get("id") or item.get("tag") if isinstance(item, dict) else None
            found.extend(_find_placeholders(item, f"{path}[{label!r}]" if label else f"{path}[{index}]"))
    return found


_ConfigLoader.add_constructor("!include", _inline_include_constructor)

_ConfigLoader.add_constructor("!placeholder", _placeholder_constructor)
