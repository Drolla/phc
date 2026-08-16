"""Smoke test: every shipped examples/*.yaml still loads.

Cheap, broad coverage that no unit test provides -- an example config is
the only place several features are exercised together (profiles,
!include splicing, every extension's configure(), the task/action
registry), and a config-driven project's examples are also its de facto
API documentation. A refactor that breaks module/extension discovery,
descriptor lookup, or any extension's configure() signature shows up here
first, against real configs rather than fixtures.

Deliberately load-only: load_system() builds phc/devices/tasks/extensions but
performs no device I/O (that starts at the Scheduler's first fetch), so
this stays offline and fast.
"""

import logging
from pathlib import Path

import pytest

from phc.core.config import ConfigError, load_system

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

# Examples that ship with `!placeholder` values (credentials, a controller
# URL) and therefore MUST refuse to load until a user substitutes real
# ones -- see phc.core.config._find_placeholders. Asserting the refusal is as
# valuable as asserting the others load: it is what stops a half-configured
# example from silently reaching real hardware with "<URL>" as an address.
PLACEHOLDER_EXAMPLES = {
    "full_house_system.yaml",
    "waveplus_bridge_system.yaml",
    "zway_system.yaml",
}


def _example_paths() -> list[Path]:
    """Every top-level example system config. examples/devices/ is excluded
    on purpose: those are `!include` fragments, not standalone systems."""
    return sorted(EXAMPLES_DIR.glob("*.yaml"))


def _example_id(path: Path) -> str:
    return path.name


@pytest.fixture
def isolated_run(tmp_path, monkeypatch):
    """Run one example with its side effects contained.

    Two kinds of leakage to hold back:

    - Extensions resolve their own file paths (logdb's `csv_path`,
      recovery's/timer's `path`) relative to the process CWD, so loading
      an example would otherwise create files under the repo's own logs/.
      chdir into a tmp_path instead.
    - load_system() calls configure_logging(), which replaces the handlers
      on the shared "phc" logger and sets propagate=False. Left in place,
      that reaches every later test in the session (see test_scheduler.py's
      task_log fixture, which exists to work around exactly this), so the
      previous state is restored afterwards.
    """
    monkeypatch.chdir(tmp_path)
    phc_logger = logging.getLogger("phc")
    saved = (list(phc_logger.handlers), phc_logger.level, phc_logger.propagate)
    try:
        yield tmp_path
    finally:
        phc_logger.handlers, phc_logger.level, phc_logger.propagate = saved


@pytest.mark.parametrize("path", _example_paths(), ids=_example_id)
def test_example_config_loads(path, isolated_run):
    """Each example either builds a System, or -- if it ships
    `!placeholder` values -- refuses to load with a message naming them."""
    if path.name in PLACEHOLDER_EXAMPLES:
        with pytest.raises(ConfigError) as excinfo:
            load_system(path)
        assert "!placeholder" in str(excinfo.value)
        return

    system = load_system(path)
    assert system.devices, f"{path.name}: built no devices"
    # Every task's actions resolved against real phc/devices/extensions at
    # build time; reaching here means none of that raised.
    assert isinstance(system.tasks, list)


def test_every_example_is_covered():
    """Guard against the parametrization silently going empty (e.g. a
    moved examples/ directory), which would make every assertion above
    vacuous."""
    paths = _example_paths()
    assert len(paths) >= 15, f"expected the full example set, found {len(paths)}"
    missing = PLACEHOLDER_EXAMPLES - {p.name for p in paths}
    assert not missing, f"PLACEHOLDER_EXAMPLES names files that no longer exist: {sorted(missing)}"
