"""Text-level config composition: `!include` lines spliced into one YAML text.

A config is built as text before anything parses it: every line of the form

    !include <file>
    !include <file>#<path>

is replaced by the lines of `<file>`, re-indented to the directive's own
column, until no directive is left. The result is one YAML text, parsed
once, so a duplicate key resolves exactly as PyYAML resolves it anywhere
else (the last one wins) -- ordering literal keys before or after an
include line is how a config chooses which value stands.

- A directive must be a line of its own. A trailing comment needs
  whitespace before its `#`; a `#` directly after the file name starts
  the path instead.
- `<file>` resolves against the directory of the file the directive is
  in. Each included file is fully expanded first, so a nested directive
  never leaves its own file unresolved.
- `#<path>` selects part of the (expanded) included file: dotted mapping
  keys, with a list entry picked by its `id`/`tag` or by `[n]`, e.g.
  `site_config.yaml#zway` or `devices.yaml#house.children[0]`. A mapping
  key inserts what sits *under* it (its keys or items, not the key line
  itself); a list entry is inserted as a list item, `- ` included. A
  scalar or a flow-style `{...}`/`[...]` value cannot fill a full line
  and is rejected.
- Every file must be valid YAML once its own directives are expanded.
  Errors name the original file and line, via the line map every
  ExpandedText carries.

Known limitation: a line starting with `!include` inside a block scalar
(`code: |`) is still taken as a directive.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from phc.core.errors import ConfigError

_DIRECTIVE_RE = re.compile(r"^(?P<indent> *)!include[ \t]+(?P<spec>.+?)(?:[ \t]+#.*)?[ \t]*$")
_DOCUMENT_START_RE = re.compile(r"^---(?:[ \t]+#.*)?[ \t]*$")
_DOCUMENT_END_RE = re.compile(r"^\.\.\.(?:[ \t]+#.*)?[ \t]*$")
_SELECTOR_RE = re.compile(r"(?:[^.\[\]]+|\[\d+\])(?:\.[^.\[\]]+|\[\d+\])*")
_SELECTOR_SEGMENT_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


@dataclass
class ExpandedText:
    """Expanded config lines, each paired with where it came from.

    `origins[i]` is the (file, 1-based line) that `lines[i]` was copied
    from -- for an include's marker comments, the directive's own line."""

    lines: list[str] = field(default_factory=list)
    origins: list[tuple[Path, int]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(line + "\n" for line in self.lines)

    def append(self, line: str, origin: tuple[Path, int]) -> None:
        self.lines.append(line)
        self.origins.append(origin)

    def where(self, index: int) -> str:
        """`file:line` for a 0-based index into `lines`.

        Clamped: a YAML error at end of input points one past the last
        line."""
        if not self.origins:
            return "<empty>"
        path, line = self.origins[min(max(index, 0), len(self.origins) - 1)]
        return f"{path}:{line}"


def expand_file(path: str | Path) -> ExpandedText:
    """Expand every `!include` in the config at `path`, recursively."""
    root = Path(path).resolve()
    return _expand(root, (root,))


def check_syntax(expanded: ExpandedText) -> None:
    """Raise ConfigError if `expanded` is not one valid YAML document.

    Composes the text without constructing it, so tags such as
    `!placeholder` need no loader support here."""
    try:
        yaml.compose(expanded.text, Loader=yaml.SafeLoader)
    except yaml.MarkedYAMLError as exc:
        raise ConfigError(_describe_yaml_error(exc, expanded)) from None


def _expand(path: Path, stack: tuple[Path, ...]) -> ExpandedText:
    """Expand one file.

    `stack` is the include chain leading to it, `path` included, for
    circular-include detection."""
    result = ExpandedText()
    seen_content = False
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if _DOCUMENT_START_RE.match(line):
            if seen_content:
                raise ConfigError(f"{path}:{number}: only one YAML document per file is supported")
            continue
        if _DOCUMENT_END_RE.match(line):
            continue
        match = _DIRECTIVE_RE.match(line)
        if match is None:
            result.append(line, (path, number))
            stripped = line.strip()
            seen_content = seen_content or bool(stripped and not stripped.startswith("#"))
            continue
        seen_content = True
        indent, spec = match["indent"], match["spec"]
        included = _include(spec, path, number, stack)
        result.append(f"{indent}# >>> !include {spec}", (path, number))
        for included_line, origin in zip(included.lines, included.origins, strict=True):
            result.append(f"{indent}{included_line}" if included_line.strip() else "", origin)
        result.append(f"{indent}# <<< !include {spec}", (path, number))
    return result


def _include(spec: str, path: Path, number: int, stack: tuple[Path, ...]) -> ExpandedText:
    """Resolve one directive's `<file>[#<path>]` into its expanded lines."""
    location = f"{path}:{number}"
    if len(spec) >= 2 and spec[0] == spec[-1] and spec[0] in "\"'":
        spec = spec[1:-1]
    file_part, _, selector = spec.partition("#")
    file_part = file_part.strip()
    if not file_part:
        raise ConfigError(f"{location}: !include: no file given")
    target = (path.parent / file_part).resolve()
    if not target.is_file():
        raise ConfigError(f"{location}: !include: no such file: {target}")
    if target in stack:
        chain = " -> ".join(str(p) for p in (*stack, target))
        raise ConfigError(f"{location}: !include: circular include: {chain}")
    included = _expand(target, (*stack, target))
    if selector:
        included = _select(included, selector.strip(), target, location)
    return included


def _select(included: ExpandedText, selector: str, target: Path, location: str) -> ExpandedText:
    """The lines of `included` that `selector` points at, dedented to column 0."""
    if not _SELECTOR_RE.fullmatch(selector):
        raise ConfigError(
            f"{location}: !include: invalid path {selector!r} -- expected dotted keys, "
            f"list entries by id/tag or [n], e.g. 'devices.house.children[0]'")
    try:
        node = yaml.compose(included.text, Loader=yaml.SafeLoader)
    except yaml.MarkedYAMLError as exc:
        raise ConfigError(f"{location}: !include {target}:\n{_describe_yaml_error(exc, included)}") from None

    is_entry = False
    walked = ""
    for name, index in _SELECTOR_SEGMENT_RE.findall(selector):
        segment = name or f"[{index}]"
        node, is_entry = _step(node, name, index, f"{location}: !include {target}#{walked or '<top>'}")
        walked = f"{walked}{'' if index or not walked else '.'}{segment}"

    if is_entry:
        start = node.start_mark.line
        column = included.lines[start].rfind("-", 0, node.start_mark.column)
        if column < 0:
            raise ConfigError(
                f"{location}: !include {target}#{selector}: list entry must start on its "
                f"'- ' line ({included.where(start)})")
    else:
        if not isinstance(node, yaml.MappingNode | yaml.SequenceNode):
            raise ConfigError(
                f"{location}: !include {target}#{selector}: selects a single value -- a "
                f"full-line !include needs a mapping or a list ({included.where(node.start_mark.line)})")
        if node.flow_style:
            raise ConfigError(
                f"{location}: !include {target}#{selector}: selects a flow-style {{...}}/[...] "
                f"value, which cannot be spliced as lines -- write it in block style "
                f"({included.where(node.start_mark.line)})")
        start, column = node.start_mark.line, node.start_mark.column

    # A block node's end mark sits at the next token, often the start of a
    # later line, which the slice stops before; an end mark after content on
    # its own line (a flow or scalar list entry) keeps that line.
    end_line, end_column = node.end_mark.line, node.end_mark.column
    end = end_line
    if end_line < len(included.lines) and included.lines[end_line][:end_column].strip():
        end = end_line + 1
    end = max(end, start + 1)

    # Trailing blank/comment lines up to the next token belong to whatever
    # follows the selected node, unless they are indented as its content.
    min_indent = column + 1 if is_entry else column
    while end > start + 1:
        stripped = included.lines[end - 1].strip()
        indent = len(included.lines[end - 1]) - len(included.lines[end - 1].lstrip())
        if stripped and not (stripped.startswith("#") and indent < min_indent):
            break
        end -= 1

    selected = ExpandedText()
    for line, origin in zip(included.lines[start:end], included.origins[start:end], strict=True):
        selected.append(line[column:] if not line[:column].strip() else line.lstrip(), origin)
    return selected


def _step(node, name: str, index: str, where: str):
    """One selector segment down from `node`: (child, is_list_entry)."""
    if isinstance(node, yaml.MappingNode):
        if index:
            raise ConfigError(f"{where}: [{index}] used on a mapping, not a list")
        keys = [key.value for key, _ in node.value if isinstance(key, yaml.ScalarNode)]
        for key, value in node.value:
            if isinstance(key, yaml.ScalarNode) and key.value == name:
                return value, False
        raise ConfigError(f"{where}: no key {name!r} (available: {', '.join(map(repr, keys))})")
    if isinstance(node, yaml.SequenceNode):
        if index:
            if int(index) >= len(node.value):
                raise ConfigError(f"{where}: [{index}] out of range ({len(node.value)} entries)")
            return node.value[int(index)], True
        labels = []
        for item in node.value:
            label = _entry_label(item)
            if label == name:
                return item, True
            if label is not None:
                labels.append(label)
        raise ConfigError(f"{where}: no list entry with id/tag {name!r} "
                          f"(available: {', '.join(map(repr, labels))})")
    raise ConfigError(f"{where}: cannot select {name or f'[{index}]'!r} inside a single value")


def _entry_label(item) -> str | None:
    """A list entry's `id`, or else its `tag`, if it is a mapping with one."""
    if not isinstance(item, yaml.MappingNode):
        return None
    found = {key.value: value.value for key, value in item.value
             if isinstance(key, yaml.ScalarNode) and isinstance(value, yaml.ScalarNode)}
    return found.get("id", found.get("tag"))


def _describe_yaml_error(exc: yaml.MarkedYAMLError, expanded: ExpandedText) -> str:
    """A PyYAML error message, its positions mapped back to file:line."""
    parts = []
    if exc.context:
        parts.append(exc.context)
        if exc.context_mark is not None:
            parts.append(f"  at {expanded.where(exc.context_mark.line)}")
    if exc.problem:
        parts.append(exc.problem)
        if exc.problem_mark is not None:
            parts.append(f"  at {expanded.where(exc.problem_mark.line)}")
    return "\n".join(parts) or str(exc)
