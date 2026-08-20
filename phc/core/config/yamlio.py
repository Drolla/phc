"""YAML loading for system configs: the `!include`/`!placeholder` tags and
the list-splicing they imply.

Purely syntactic -- this layer knows nothing about devices, endpoints or
tasks. It turns a file on disk into a plain nested dict/list structure,
and everything else in this package works from that.
"""

from pathlib import Path

import yaml

from phc.core.errors import ConfigError

_include_stack: list[Path] = []


class _IncludeLoader(yaml.SafeLoader):
    """SafeLoader that also understands !include <relative-path>, so a
    system YAML can pull in child YAML files (see _include_constructor), and
    <<: !include <relative-path>, which merges the included mapping's keys
    into the surrounding mapping -- the surrounding mapping's own keys win,
    the same precedence an ordinary `<<: *anchor` merge already has (see
    construct_mapping below). Only a single !include as the merge value is
    supported (not a sequence mixing !include with *anchor merges)."""

    def construct_mapping(self, node, deep=False):
        if not isinstance(node, yaml.MappingNode):
            return super().construct_mapping(node, deep=deep)
        # Pull any `<<: !include ...` pairs out of the node's own value list
        # before handing the rest to the normal PyYAML construction (which
        # still runs its own merge-key handling, e.g. `<<: *anchor`, on
        # whatever's left) -- PyYAML's own merge machinery works purely at
        # the node level (splicing raw (key, value) node pairs together
        # before any construction happens), so it can't merge in an
        # !include's *constructed* dict; this constructs each !include
        # target first and merges the resulting dicts by hand instead.
        included = {}
        kept = []
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge" and value_node.tag == "!include":
                target = self.construct_object(value_node, deep=True)
                if not isinstance(target, dict):
                    raise ConfigError(
                        f"'<<: !include' target must be a mapping, got "
                        f"{type(target).__name__} ({value_node.start_mark})")
                included.update(target)
            else:
                kept.append((key_node, value_node))
        node.value = kept
        result = super().construct_mapping(node, deep=deep)
        if not included:
            return result
        included.update(result)
        return included


def _include_constructor(loader: yaml.SafeLoader, node: yaml.Node):
    """Replace an !include node with the parsed contents of the referenced
    file, resolved relative to the including file's own directory (so
    child-of-child includes keep resolving correctly regardless of the root
    file's location or the process's cwd). Raises ConfigError on a missing
    file or a circular include chain."""
    include_rel = loader.construct_scalar(node)
    base_dir = Path(loader.name).resolve().parent
    include_path = (base_dir / include_rel).resolve()
    if not include_path.is_file():
        raise ConfigError(f"!include: no such file: {include_path} (from {loader.name})")
    if include_path in _include_stack:
        chain = " -> ".join(str(p) for p in (*_include_stack, include_path))
        raise ConfigError(f"!include: circular include: {chain}")
    _include_stack.append(include_path)
    try:
        with open(include_path, encoding="utf-8") as f:
            return yaml.load(f, Loader=_IncludeLoader)
    finally:
        _include_stack.pop()


class _Placeholder(str):
    """A scalar built from `!placeholder <example>` -- marks a value (a
    credential, another system's URL, ...) that a system YAML's author must
    replace with something real before the file is fit to run. Subclasses
    str so the example text underneath still reads/compares like a normal
    string to any code that isn't specifically checking for this type; see
    _find_placeholders, which load_system() calls right after parsing, before
    any of that code runs, so a leftover !placeholder always aborts the
    load rather than silently reaching a device/extension as a literal
    value like "<URL>"."""


def _placeholder_constructor(loader: yaml.SafeLoader, node: yaml.Node) -> _Placeholder:
    return _Placeholder(loader.construct_scalar(node))


def _find_placeholders(value, path: str = "") -> list[str]:
    """Recursively collect a human-readable path (dotted for mapping keys,
    bracketed for list entries -- using that entry's own `id`/`tag` when it
    has one, since "devices[2]" is far less useful than
    "devices[2].children['sensor_cellar']") for every `!placeholder` value
    still nested anywhere in `value` (the raw, just-parsed config tree)."""
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


def _flatten_list_entries(raw_entries: list) -> list:
    """Splice a `- !include <path>` item whose target resolves to a list
    into the surrounding list, instead of nesting it as one list-of-lists
    element. See docs/configuration.md#splitting-configuration-across-files.
    Used by every top-level/nested list this module builds: `devices:`,
    a host device's `children:`, `task_specs:`, `tasks:`."""
    flat = []
    for entry in raw_entries:
        if isinstance(entry, list):
            flat.extend(entry)
        else:
            flat.append(entry)
    return flat


_IncludeLoader.add_constructor("!include", _include_constructor)

_IncludeLoader.add_constructor("!placeholder", _placeholder_constructor)
