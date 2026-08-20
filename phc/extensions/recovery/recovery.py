"""RecoveryStore: a small YAML file persisting selected writable device
endpoint values, so PHC can restore them to their last-known-good state
after a crash/restart (see phc/extensions/recovery/extension.py for the
on_start/on_tick/on_stop wiring that drives it). Whole-file rewrite, not
append -- unlike phc.extensions.logdb.logdb.LogDb's much larger append-only
CSV, this file holds one entry per subscribed endpoint, so there's no
replay/reconciliation logic to write."""

import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger("phc.recovery")


class RecoveryStore:
    """A YAML file at `path` holding {"<qualified_id>/<endpoint_key>":
    value} for a fixed set of subscribed pairs.

    write() rewrites the whole file atomically; load() reads it back,
    tolerating a missing or corrupt file (returns {} either way -- see
    load()'s docstring)."""

    def __init__(self, path):
        self.path = Path(path)

    def write(self, values: dict[str, object]) -> None:
        """Atomically rewrite the whole file with `values`.

        Writes to a sibling "<path>.tmp" file first, then os.replace()s it
        over `path` -- os.replace() is atomic on both POSIX and Windows,
        so a crash mid-write can never leave a half-written file in place
        of a previously-good one; the reader always sees either the old
        content or the fully-written new content, never a partial mix.
        Creates `path`'s parent directory if it doesn't exist yet, mirroring
        phc.extensions.logdb.logdb.LogDb's own csv_path.parent.mkdir()."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(values, f)
        os.replace(tmp_path, self.path)

    def load(self) -> dict[str, object]:
        """Read and return the persisted {pair_key: value} dict.

        Returns {} if the file doesn't exist yet (first run -- nothing to
        recover, not an error), is empty, or can't be parsed as a YAML
        mapping (corrupt/truncated file, e.g. from a crash that hit before
        the first successful write() -- also not an error, just logged as
        a warning, mirroring THC's silent-catch-on-source but with
        logging, consistent with how the rest of this codebase logs
        rather than stays silent)."""
        if not self.path.exists():
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as exc:
            logger.warning(
                "recovery: %s is missing or corrupt (%s); starting with nothing to recover",
                self.path, exc)
            return {}
        if data is None:
            return {}
        if not isinstance(data, dict):
            logger.warning("recovery: %s does not contain a YAML mapping; ignoring", self.path)
            return {}
        return data
