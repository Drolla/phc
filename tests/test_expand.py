"""Tests for phc.core.config.expand: text-level `!include` composition."""

import pytest
import yaml

from phc.cli import main
from phc.core.config.expand import check_syntax, expand_file
from phc.core.errors import ConfigError


def _write(path, text):
    path.write_text(text.lstrip("\n"), encoding="utf-8")
    return path


def _content(expanded) -> list[str]:
    """Expanded lines without the `# >>>`/`# <<<` include markers."""
    return [line for line in expanded.lines if line.strip()[:5] not in ("# >>>", "# <<<")]


def test_include_reindents_to_directive_column(tmp_path):
    _write(tmp_path / "part.yaml", "a: 1\nb:\n  c: 2\n")
    root = _write(tmp_path / "root.yaml", "top:\n  !include part.yaml\n  d: 3\n")
    expanded = expand_file(root)
    assert _content(expanded) == ["top:", "  a: 1", "  b:", "    c: 2", "  d: 3"]
    assert yaml.safe_load(expanded.text) == {"top": {"a": 1, "b": {"c": 2}, "d": 3}}


def test_include_wraps_inserted_lines_in_markers(tmp_path):
    _write(tmp_path / "part.yaml", "a: 1\n")
    root = _write(tmp_path / "root.yaml", "top:\n  !include part.yaml\n")
    assert expand_file(root).lines == [
        "top:", "  # >>> !include part.yaml", "  a: 1", "  # <<< !include part.yaml"]


def test_include_keeps_block_scalars_intact(tmp_path):
    _write(tmp_path / "tasks.yaml", """
- tag: t
  code: |
    if x:
        y()
""")
    root = _write(tmp_path / "root.yaml", "tasks:\n  !include tasks.yaml\n")
    assert yaml.safe_load(expand_file(root).text)["tasks"][0]["code"] == "if x:\n    y()\n"


def test_origins_point_at_source_lines(tmp_path):
    part = _write(tmp_path / "part.yaml", "# comment\na: 1\n")
    root = _write(tmp_path / "root.yaml", "top:\n  !include part.yaml\n")
    expanded = expand_file(root)
    assert expanded.origins[expanded.lines.index("  a: 1")] == (part.resolve(), 2)
    assert expanded.origins[0] == (root.resolve(), 1)


def test_trailing_comment_and_document_marker(tmp_path):
    _write(tmp_path / "part.yaml", "---\na: 1\n...\n")
    root = _write(tmp_path / "root.yaml", "top:\n  !include part.yaml   # a note\n")
    assert yaml.safe_load(expand_file(root).text) == {"top": {"a": 1}}


def test_second_document_rejected(tmp_path):
    _write(tmp_path / "part.yaml", "a: 1\n---\nb: 2\n")
    root = _write(tmp_path / "root.yaml", "top:\n  !include part.yaml\n")
    with pytest.raises(ConfigError, match="one YAML document"):
        expand_file(root)


def test_include_resolves_relative_to_including_file(tmp_path):
    (tmp_path / "sub").mkdir()
    _write(tmp_path / "sub" / "inner.yaml", "x: 1\n")
    _write(tmp_path / "sub" / "outer.yaml", "!include inner.yaml\n")
    root = _write(tmp_path / "root.yaml", "top:\n  !include sub/outer.yaml\n")
    assert yaml.safe_load(expand_file(root).text) == {"top": {"x": 1}}


def test_missing_file_names_directive_location(tmp_path):
    root = _write(tmp_path / "root.yaml", "top:\n  !include nope.yaml\n")
    with pytest.raises(ConfigError, match=r"root\.yaml:2: !include: no such file"):
        expand_file(root)


def test_self_include_is_circular(tmp_path):
    root = _write(tmp_path / "root.yaml", "a:\n  !include root.yaml#b\nb:\n  c: 1\n")
    with pytest.raises(ConfigError, match="circular include"):
        expand_file(root)


# ---------- #path selection ----------

_SITE = """
modules:
  # zway controller
  zway:
    url: ws://x
    #url: ws://y
    token: t
  # heat pump
  viessmann:
    email: e
devices:
  - id: house
    children:
      - id: a
        module: virtual
      - { id: b, module: virtual }
tasks:
  - tag: first
    time: +1s
  - tag: second
    time: +2s
log:
  - dest: logs/phc.log
"""


def _select(tmp_path, selector, column=2):
    _write(tmp_path / "site.yaml", _SITE)
    root = _write(tmp_path / "root.yaml", f"top:\n{' ' * column}!include site.yaml#{selector}\n")
    return expand_file(root)


def test_select_mapping_key_inserts_content_not_key_line(tmp_path):
    # Neither the comment above `zway:` nor the one above the next key
    # (`# heat pump`, indented less than zway's content) comes along.
    expanded = _select(tmp_path, "modules.zway")
    assert _content(expanded) == ["top:", "  url: ws://x", "  #url: ws://y", "  token: t"]


def test_select_key_holding_a_list_inserts_its_items(tmp_path):
    expanded = _select(tmp_path, "log")
    assert yaml.safe_load(expanded.text) == {"top": [{"dest": "logs/phc.log"}]}


def test_select_list_entry_by_id_inserts_it_as_item(tmp_path):
    expanded = _select(tmp_path, "devices.house.children.a")
    assert _content(expanded) == ["top:", "  - id: a", "    module: virtual"]


def test_select_flow_style_list_entry(tmp_path):
    expanded = _select(tmp_path, "devices.house.children.b")
    assert yaml.safe_load(expanded.text) == {"top": [{"id": "b", "module": "virtual"}]}


def test_select_list_entry_by_tag_and_index(tmp_path):
    assert yaml.safe_load(_select(tmp_path, "tasks.second").text) == {
        "top": [{"tag": "second", "time": "+2s"}]}
    assert yaml.safe_load(_select(tmp_path, "tasks[0]").text) == {
        "top": [{"tag": "first", "time": "+1s"}]}


def test_select_unknown_key_lists_available(tmp_path):
    with pytest.raises(ConfigError, match=r"no key 'zwya' \(available: 'zway', 'viessmann'\)"):
        _select(tmp_path, "modules.zwya")


def test_select_unknown_list_entry_lists_ids(tmp_path):
    with pytest.raises(ConfigError, match=r"no list entry with id/tag 'c' \(available: 'a', 'b'\)"):
        _select(tmp_path, "devices.house.children.c")


def test_select_scalar_rejected(tmp_path):
    with pytest.raises(ConfigError, match="selects a single value"):
        _select(tmp_path, "modules.zway.url")


def test_select_flow_mapping_value_rejected(tmp_path):
    _write(tmp_path / "site.yaml", "conn: { url: x }\n")
    root = _write(tmp_path / "root.yaml", "top:\n  !include site.yaml#conn\n")
    with pytest.raises(ConfigError, match="flow-style"):
        expand_file(root)


def test_select_invalid_path_rejected(tmp_path):
    with pytest.raises(ConfigError, match="invalid path"):
        _select(tmp_path, "modules..zway")


def test_select_from_a_file_that_includes_others(tmp_path):
    _write(tmp_path / "secret.yaml", "token: t\n")
    _write(tmp_path / "site.yaml", "zway:\n  url: x\n  !include secret.yaml\nother:\n  a: 1\n")
    root = _write(tmp_path / "root.yaml", "top:\n  !include site.yaml#zway\n")
    assert yaml.safe_load(expand_file(root).text) == {"top": {"url": "x", "token": "t"}}


# ---------- syntax check ----------

def test_check_syntax_maps_error_to_source_file(tmp_path):
    _write(tmp_path / "part.yaml", "a: 1\nb: [unclosed\n")
    root = _write(tmp_path / "root.yaml", "top:\n  !include part.yaml\n")
    with pytest.raises(ConfigError, match=r"part\.yaml:2"):
        check_syntax(expand_file(root))


def test_phc_expand_writes_output(tmp_path, capsys):
    _write(tmp_path / "part.yaml", "a: 1\n")
    root = _write(tmp_path / "root.yaml", "top:\n  !include part.yaml\n")
    out = tmp_path / "out.yaml"
    assert main(["expand", "--config", str(root), "--output", str(out)]) == 0
    assert yaml.safe_load(out.read_text(encoding="utf-8")) == {"top": {"a": 1}}


def test_phc_expand_reports_invalid(tmp_path, capsys):
    root = _write(tmp_path / "root.yaml", "top:\n  !include nope.yaml\n")
    assert main(["expand", "--config", str(root)]) == 1
    assert "INVALID" in capsys.readouterr().out
