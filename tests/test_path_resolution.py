"""Where an extension's relative file paths land.

They used to resolve against the process's working directory, while
`log:` destinations and `plugin_paths:` resolved against the config file's
directory -- the same config, two base directories, so where an
installation's history and recovery files ended up depended on where it
happened to be started from. A service started from / put them somewhere
different from a hand-started one.

They now all resolve against the config file. Because that relocates
existing data, a file left at the old location is reported rather than
silently ignored.
"""

import logging

import pytest

from phc.core.config import load_system

CONFIG = """
heartbeat: 1s
devices:
  - id: lamp
    module: virtual
    endpoints: [{{ key: state, writable: true, default: "off" }}]
extensions:
  logdb:
    house:
      csv_path: "{csv_path}"
      selectors: ["lamp/state"]
"""


def _write_config(directory, csv_path="data/house.csv"):
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "system.yaml"
    config.write_text(CONFIG.format(csv_path=csv_path), encoding="utf-8")
    return config


@pytest.fixture
def config_log(caplog):
    """caplog attached to "phc.config", pinned against propagation so the
    count is deterministic (see tests/test_health.py's health_log)."""
    logger = logging.getLogger("phc.config")
    previous_propagate, previous_level = logger.propagate, logger.level
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)
        logger.propagate, logger.level = previous_propagate, previous_level


def test_a_relative_path_resolves_against_the_config_file(tmp_path, monkeypatch):
    """The point of the change: where the file lands must not depend on
    where PHC was started from."""
    config_dir = tmp_path / "house"
    config = _write_config(config_dir)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    load_system(config)

    assert (config_dir / "data" / "house.csv").is_file()
    assert not (elsewhere / "data").exists(), "must not follow the working directory"


def test_the_same_config_lands_in_the_same_place_from_any_directory(tmp_path, monkeypatch):
    config_dir = tmp_path / "house"
    config = _write_config(config_dir)
    for cwd_name in ("a", "b"):
        cwd = tmp_path / cwd_name
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        load_system(config)
        assert not (cwd / "data").exists()
    assert (config_dir / "data" / "house.csv").is_file()


def test_an_absolute_path_is_left_alone(tmp_path, monkeypatch):
    config_dir = tmp_path / "house"
    target = tmp_path / "somewhere" / "explicit.csv"
    config = _write_config(config_dir, csv_path=target.as_posix())
    monkeypatch.chdir(tmp_path)

    load_system(config)

    assert target.is_file()


def test_data_left_at_the_old_location_is_reported(tmp_path, monkeypatch, config_log):
    """Silently starting with empty history -- or restoring nothing from a
    recovery file that is still sitting where it used to be -- is exactly
    the failure nobody notices until it matters."""
    config_dir = tmp_path / "house"
    config = _write_config(config_dir)
    old_cwd = tmp_path / "old"
    (old_cwd / "data").mkdir(parents=True)
    (old_cwd / "data" / "house.csv").write_text("phc-logdb,version=1\n", encoding="utf-8")
    monkeypatch.chdir(old_cwd)

    load_system(config)

    warnings = [r.getMessage() for r in config_log.records if r.levelno == logging.WARNING]
    assert any("old working-directory location" in m for m in warnings), warnings
    assert any("logdb.house" in m for m in warnings)


def test_no_warning_when_nothing_is_at_the_old_location(tmp_path, monkeypatch, config_log):
    config = _write_config(tmp_path / "house")
    cwd = tmp_path / "fresh"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    load_system(config)

    warnings = [r.getMessage() for r in config_log.records if r.levelno == logging.WARNING]
    assert not any("old working-directory" in m for m in warnings), warnings


def test_no_warning_when_the_new_location_already_has_the_data(tmp_path, monkeypatch, config_log):
    """Once migrated, the old copy is stale -- warning about it forever
    would train the reader to ignore the message."""
    config_dir = tmp_path / "house"
    config = _write_config(config_dir)
    (config_dir / "data").mkdir(parents=True)
    (config_dir / "data" / "house.csv").write_text("phc-logdb,version=1\n", encoding="utf-8")
    old_cwd = tmp_path / "old"
    (old_cwd / "data").mkdir(parents=True)
    (old_cwd / "data" / "house.csv").write_text("phc-logdb,version=1\n", encoding="utf-8")
    monkeypatch.chdir(old_cwd)

    load_system(config)

    warnings = [r.getMessage() for r in config_log.records if r.levelno == logging.WARNING]
    assert not any("old working-directory" in m for m in warnings), warnings


def test_recovery_and_timer_paths_resolve_the_same_way(tmp_path, monkeypatch):
    """All three path-taking extensions declare `path: true`, so none of
    them needs its own resolution logic."""
    config_dir = tmp_path / "house"
    config_dir.mkdir()
    config = config_dir / "system.yaml"
    config.write_text("""
heartbeat: 1s
devices:
  - id: lamp
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
extensions:
  recovery:
    critical:
      path: "state/recovery.yaml"
      selectors: ["lamp/state"]
  timer:
    house:
      path: "state/timers.yaml"
      selectors: ["lamp/state"]
""", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    system = load_system(config)

    expected = (config_dir / "state").resolve()
    assert system.extensions["recovery.critical"].store.path.parent == expected
    assert system.extensions["timer.house"].store.path.parent == expected
