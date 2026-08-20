"""LogDb: a CSV-persisted, in-memory-buffered log of numeric samples.

Decoupled from phc.core.device/phc.core.task: log() takes a plain timestamp and a
{label: float} dict, and the query methods are timestamp-oriented. Single-
writer, no file locking -- fine for PHC's single-process asyncio model, not
safe for multi-process use.

CSV format (see _reconcile_header/_replay for the full algorithm):

    phc-logdb,version=1
    type,time,house.desk_lamp/power,house.desk_lamp/brightness,living_light/state<padding...>
    F,1721830000,1,80,0
    D,1721830030,,,1
    D,1721830060,0,nan,
    F,1721833000,0,nan,1

Line 1 is a short fixed info line. Line 2 is the label header, padded with
trailing spaces to a reserved width so it can be rewritten in place if new
labels are added later (see _reconcile_header) -- labels only ever grow,
never shrink. Its first two columns, "type" and "time", are fixed and not
part of the label set.

Each data row is explicitly marked full ("F", every cell populated) or
delta ("D", a blank cell means "unchanged since the previous row", "nan"
marks an explicit transition to no-data), and its time column is a
whole-second Unix timestamp (sub-second precision is not logged). Marking
rows explicitly (rather than inferring "full" from a row-count parity) is
required because header growth can sacrifice leading rows (see below),
which would otherwise desync any inferred parity.

A value cell with no fractional part is written without a decimal point
(e.g. "80" not "80.0") to save space -- float(cell) parses either form
identically on reload, so this is a pure formatting choice.
"""

import array
import bisect
import logging
import math
import time
from pathlib import Path

logger = logging.getLogger("phc.logdb")

_INFO_LINE_PREFIX = "phc-logdb,version="
_VERSION = 1
_MIN_HEADER_RESERVE = 512
_INITIAL_CHUNK_SIZE = 65536
_NAN = float("nan")


class LogDb:
    """CSV-backed, in-memory ring buffer of numeric samples.

    `labels` is the desired set of columns; on an existing file, the
    persisted label set (which may already include labels no longer
    desired, or lack ones newly desired) is reconciled first -- see
    _reconcile_header. self.labels is always the final, combined set used
    for both the in-memory columns and the CSV's column order.
    """

    def __init__(self, csv_path, labels: list[str], *, full_vector_interval: int = 100,
                 max_records: int | None = None, max_age: float | None = None,
                 header_reserve_bytes: int | None = None):
        """Open (or create) the CSV at `csv_path`.

        Reconciles its header against `labels` and replays any existing
        rows into memory."""
        if len(labels) != len(set(labels)):
            raise ValueError(f"duplicate labels: {labels}")
        for label in labels:
            if "," in label:
                raise ValueError(f"label {label!r} must not contain a comma")

        self.csv_path = Path(csv_path)
        self.full_vector_interval = max(1, int(full_vector_interval))
        self.max_records = max_records
        self.max_age = max_age
        self._header_reserve_bytes = header_reserve_bytes
        self._trim_slack = 1.1

        self._times: array.array = array.array("d")
        self._last_written: dict[str, float] = {}
        # 0 means the next row written is a full-vector ("F") row. Reset to
        # 0 on every process start (rather than trying to persist/recover
        # the exact phase) -- costs at most one extra full row per restart.
        self._rows_since_full = 0
        self._fh = None
        self._data_start = 0

        self.labels = self._reconcile_header(labels)
        self._columns: dict[str, array.array] = {label: array.array("d") for label in self.labels}
        self._replay()
        self._fh = open(self.csv_path, "a", encoding="utf-8", newline="")

    def close(self) -> None:
        """Close the underlying file handle, if open."""
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __del__(self):
        """Best-effort close() on garbage collection, swallowing any errors."""
        try:
            self.close()
        except Exception:
            pass

    def __len__(self) -> int:
        """Return the number of records currently held in memory."""
        return len(self._times)

    # ---------- public logging API ----------

    def log(self, timestamp: float, values: dict[str, float]) -> None:
        """Append one sample row, both in memory and to the CSV.

        `values` maps a subset (or all) of self.labels to a numeric
        reading; any configured label not present this call is stored as
        NaN ("no data this sample"). `timestamp` is truncated to whole
        seconds -- sub-second precision isn't useful at logdb's
        sample-interval scale and would otherwise bloat every row.

        Trims per max_records/max_age (with slack), then writes exactly
        one row to the CSV, flushed immediately."""
        timestamp = int(timestamp)
        unknown = set(values) - set(self._columns)
        if unknown:
            logger.warning("%s: ignoring unknown label(s) %s", self.csv_path, sorted(unknown))
        logger.debug("%s: new record time=%d values=%s", self.csv_path, timestamp, values)
        self._times.append(timestamp)
        for label, col in self._columns.items():
            if label in values and label not in unknown:
                col.append(float(values[label]))
            else:
                col.append(_NAN)
        self._maybe_trim()
        self._write_row(timestamp)

    def _maybe_trim(self) -> None:
        """Drop the oldest in-memory records once retention limits are exceeded.

        Trims once the buffer exceeds max_records/max_age by more than
        trim_slack, amortizing array.array's O(n) shift across many
        log() calls instead of paying it on every single insert. Between
        trims the buffer may exceed the configured limit by up to 10%;
        documented, not silent."""
        if self.max_records is None and self.max_age is None:
            return
        n = len(self._times)
        over_count = self.max_records is not None and n > self.max_records * self._trim_slack
        over_age = (self.max_age is not None and n > 1
                    and (self._times[-1] - self._times[0]) > self.max_age * self._trim_slack)
        if not (over_count or over_age):
            return
        cutoff = 0
        if self.max_records is not None:
            cutoff = max(cutoff, n - self.max_records)
        if self.max_age is not None:
            cutoff = max(cutoff, bisect.bisect_left(self._times, self._times[-1] - self.max_age))
        if cutoff > 0:
            del self._times[:cutoff]
            for col in self._columns.values():
                del col[:cutoff]

    def _write_row(self, timestamp: int) -> None:
        """Write the in-memory record at the current tail to the CSV.

        Written as an "F" or "D" row per full_vector_interval."""
        full = self._rows_since_full == 0
        cells = ["F" if full else "D", str(timestamp)]
        for label in self.labels:
            value = self._columns[label][-1]
            if full:
                cells.append(self._fmt(value))
            else:
                prev = self._last_written.get(label)
                unchanged = prev is not None and (
                    prev == value or (math.isnan(prev) and math.isnan(value)))
                cells.append("" if unchanged else self._fmt(value))
            self._last_written[label] = value
        self._fh.write(",".join(cells) + "\n")
        self._fh.flush()
        self._rows_since_full = (self._rows_since_full + 1) % self.full_vector_interval

    @staticmethod
    def _fmt(value: float) -> str:
        """Format a float for the CSV.

        "nan" for NaN, no decimal point for integer-looking values,
        otherwise repr()."""
        if math.isnan(value):
            return "nan"
        if value == int(value):
            return str(int(value))
        return repr(value)

    # ---------- query API ----------

    def get_at(self, timestamp: float, labels: list[str] | None = None) -> dict[str, float] | None:
        """Return the record nearest to `timestamp` (either side), or None
        if empty.

        O(log n) via bisect on the always time-ascending _times."""
        if not self._times:
            return None
        idx = bisect.bisect_left(self._times, timestamp)
        if idx == 0:
            pos = 0
        elif idx == len(self._times):
            pos = len(self._times) - 1
        else:
            before, after = self._times[idx - 1], self._times[idx]
            pos = idx - 1 if (timestamp - before) <= (after - timestamp) else idx
        return self._row_at(pos, labels)

    def get_range(self, start: float, end: float, labels: list[str] | None = None) -> list[dict[str, float]]:
        """Return every record with start <= time <= end, ascending.

        O(log n + k)."""
        lo = bisect.bisect_left(self._times, start)
        hi = bisect.bisect_right(self._times, end)
        return [self._row_at(pos, labels) for pos in range(lo, hi)]

    def _row_at(self, pos: int, labels: list[str] | None) -> dict[str, float]:
        """Build one record dict at in-memory index `pos`.

        Uses the given labels, or self.labels if None."""
        keys = labels if labels is not None else self.labels
        row = {"time": self._times[pos]}
        for label in keys:
            row[label] = self._columns[label][pos]
        return row

    def get_decimated(self, labels: list[str] | None = None,
                       decimation: list[tuple[float, int]] | None = None,
                       now: float | None = None) -> list[dict[str, float]]:
        """Like get_range() over the full retained window, but coarsens
        older data.

        Older samples are averaged within each decimation tier, trading
        resolution for a bounded response size on a UI's full-history
        request (see phc.extensions.web_ui.panels.GraphPanel).

        `decimation` is a list of (age_seconds, factor) tuples: a sample's
        age (`now` minus its timestamp) is compared against each tier's
        age_seconds, and the tier with the largest age_seconds strictly
        less than that age supplies its factor -- age at or under every
        tier's threshold means factor 1 (undecimated). None or empty is
        equivalent to get_range() over the whole window. `now` defaults
        to time.time(); overridable for tests.

        Groups are formed oldest-to-newest and always restart cleanly at
        a tier boundary (never straddling two factors), each emitting one
        row using the group's last (most recent) timestamp and, per
        label, the average of its non-NaN values in the group (all-NaN
        emits NaN, matching get_range()'s own "no data" convention).

        `labels` (default: self.labels) is filtered down to labels this
        instance actually has, so a caller-supplied label this instance
        never logs (e.g. a graph panel selector matching more than one
        logdb instance covers) is silently dropped rather than raising
        KeyError -- unlike get_at()/get_range(), this is the first query
        method called with an externally-derived label list that could
        mismatch."""
        keys = [label for label in (labels if labels is not None else self.labels)
                if label in self._columns]
        n = len(self._times)
        if n == 0:
            return []
        if now is None:
            now = time.time()

        # Largest age first: the oldest section of data is handled by the
        # coarsest applicable tier. A tier with factor <= 1 is a no-op
        # (factor 1 is already the default for anything not covered by a
        # tier) and is dropped rather than risking a zero/negative-size
        # group below.
        tiers = sorted(((age, factor) for age, factor in (decimation or []) if factor > 1),
                       key=lambda tier: tier[0], reverse=True)

        # Precompute (section_start_index, factor) boundaries, oldest
        # first, covering [0, n) with no gaps -- mirrors _maybe_trim's own
        # bisect usage against the always-ascending _times.
        boundaries: list[tuple[int, int]] = []
        prev_index = 0
        for age, factor in tiers:
            cutoff = max(bisect.bisect_left(self._times, now - age), prev_index)
            if cutoff > prev_index:
                boundaries.append((prev_index, factor))
                prev_index = cutoff
        boundaries.append((prev_index, 1))

        rows = []
        for section, (start, factor) in enumerate(boundaries):
            end = boundaries[section + 1][0] if section + 1 < len(boundaries) else n
            pos = start
            while pos < end:
                group_end = min(pos + factor, end)
                rows.append(self._averaged_row(pos, group_end, keys))
                pos = group_end
        return rows

    def _averaged_row(self, start: int, end: int, keys: list[str]) -> dict[str, float]:
        """Build one decimated record averaging in-memory rows [start, end).

        Averages the given labels; uses the last row's timestamp."""
        row = {"time": self._times[end - 1]}
        for label in keys:
            col = self._columns[label]
            total, count = 0.0, 0
            for pos in range(start, end):
                value = col[pos]
                if not math.isnan(value):
                    total += value
                    count += 1
            row[label] = total / count if count else _NAN
        return row

    # ---------- header reconciliation ----------

    def _desired_header_width(self, needed: int) -> int:
        """Compute the reserved header width in bytes.

        Uses header_reserve_bytes if configured, otherwise double the
        content currently needed (at least _MIN_HEADER_RESERVE)."""
        if self._header_reserve_bytes is not None:
            return max(int(self._header_reserve_bytes), needed)
        return max(_MIN_HEADER_RESERVE, needed * 2)

    def _pad_header(self, content: str, width: int) -> bytes:
        """Encode `content` as a header line.

        Padded with trailing spaces to `width` bytes."""
        content_bytes = content.encode("utf-8")
        pad = width - len(content_bytes) - 1  # -1 for the trailing "\n"
        if pad < 0:
            pad = 0
        return content_bytes + b" " * pad + b"\n"

    def _write_fresh_header(self, labels: list[str]) -> None:
        """Write a brand-new info + label header to csv_path.

        Replaces any existing content."""
        info = f"{_INFO_LINE_PREFIX}{_VERSION}\n".encode()
        content = "type,time," + ",".join(labels)
        width = self._desired_header_width(len(content.encode("utf-8")) + 1)
        with open(self.csv_path, "wb") as f:
            f.write(info)
            f.write(self._pad_header(content, width))

    def _rotate_existing_file(self) -> None:
        """Rename an existing, unrecognized csv_path aside.

        Renamed to a timestamped .bak file."""
        backup = self.csv_path.with_name(self.csv_path.name + f".{int(time.time())}.bak")
        self.csv_path.rename(backup)

    def _consume_leading_lines(self, data_start: int, extra_needed: int) -> tuple[int, int]:
        """Read forward from `data_start`, accumulating whole lines' byte
        lengths.

        Reads until at least `extra_needed` bytes have been consumed (or
        the file runs out). Returns (bytes_consumed, records_consumed)."""
        consumed = 0
        count = 0
        with open(self.csv_path, "rb") as f:
            f.seek(data_start)
            while consumed < extra_needed:
                line = f.readline()
                if not line:
                    break
                consumed += len(line)
                count += 1
        return consumed, count

    def _reconcile_header(self, configured_labels: list[str]) -> list[str]:
        """Read or create the CSV header, growing it in place for any
        newly configured labels.

        Sets self._data_start to the byte offset where data rows begin.
        Returns the final, combined label list (persisted labels, in
        their original order, followed by any newly configured ones)."""
        if not self.csv_path.exists() or self.csv_path.stat().st_size == 0:
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_fresh_header(configured_labels)
            self._data_start = self.csv_path.stat().st_size
            return list(configured_labels)

        with open(self.csv_path, "rb") as f:
            info_line = f.readline()
            header_line = f.readline()
            data_start = f.tell()

        info_text = info_line.decode("utf-8", errors="replace")
        if not info_text.startswith(_INFO_LINE_PREFIX) or not header_line:
            logger.warning("%s has an unrecognized header; rotating and starting fresh",
                            self.csv_path)
            self._rotate_existing_file()
            self._write_fresh_header(configured_labels)
            self._data_start = self.csv_path.stat().st_size
            return list(configured_labels)

        header_start = len(info_line)
        header_text = header_line.decode("utf-8").rstrip("\n").rstrip(" ")
        persisted_labels = header_text.split(",")[2:]

        new_labels = [label for label in configured_labels if label not in persisted_labels]
        combined_labels = persisted_labels + new_labels

        if not new_labels:
            self._data_start = data_start
            return combined_labels

        logger.info("%s: extending columns with new label(s) %s", self.csv_path, new_labels)
        new_content = "type,time," + ",".join(combined_labels)
        needed = len(new_content.encode("utf-8")) + 1
        old_header_width = len(header_line)

        if needed <= old_header_width:
            with open(self.csv_path, "r+b") as f:
                f.seek(header_start)
                f.write(self._pad_header(new_content, old_header_width))
            self._data_start = data_start
            return combined_labels

        # Padding exhausted: grow the header into the data area, consuming
        # (and sacrificing) whole leading data lines until enough space is
        # freed -- documented, intentionally lossy fallback.
        consumed_bytes, consumed_records = self._consume_leading_lines(
            data_start, needed - old_header_width)
        new_width = max(old_header_width + consumed_bytes, needed)
        with open(self.csv_path, "r+b") as f:
            f.seek(header_start)
            f.write(self._pad_header(new_content, new_width))
        logger.warning(
            "logdb: %s header grew beyond its reserved space; sacrificed %d leading "
            "record(s) to make room (consider a larger header_reserve_bytes)",
            self.csv_path, consumed_records)
        self._data_start = header_start + new_width
        return combined_labels

    # ---------- restore ----------

    def _window_covered(self, lines: list[str], anchor_index: int, reference_time: float) -> bool:
        """Whether replaying from `lines[anchor_index]` onward already
        covers the full retention window.

        Used by _replay so its backward scan can stop growing its chunk.
        Unbounded (no max_records/max_age) never stops early -- the scan
        reads back to the start of the data."""
        if self.max_records is None and self.max_age is None:
            return False
        covered = True
        if self.max_records is not None:
            covered = covered and (len(lines) - anchor_index) >= self.max_records
        if self.max_age is not None:
            anchor_time = float(lines[anchor_index].split(",", 2)[1])
            covered = covered and (reference_time - anchor_time) <= self.max_age
        return covered

    def _parse_row(self, line: str, current: dict[str, float]) -> float:
        """Parse one CSV data row, updating `current` (label -> value) in place.

        A blank cell means "unchanged" (keep current[label]); a missing
        trailing cell (row shorter than self.labels, i.e. that label
        didn't exist yet when this row was written) means NaN; anything
        else is parsed as a float ("nan" -> NaN). Returns the row's
        timestamp.

        Caller must snapshot `current` (dict(current)) before the next
        call, since it's mutated in place."""
        fields = line.split(",")
        timestamp = float(fields[1])
        cells = fields[2:]
        for i, label in enumerate(self.labels):
            if i >= len(cells):
                current[label] = _NAN
                continue
            cell = cells[i]
            if cell == "":
                continue
            current[label] = _NAN if cell == "nan" else float(cell)
        return timestamp

    def _retention_cutoff(self, times: list[float], reference_time: float) -> int:
        """Compute the index below which replayed records fall outside
        max_records/max_age."""
        n = len(times)
        cutoff = 0
        if self.max_records is not None:
            cutoff = max(cutoff, n - self.max_records)
        if self.max_age is not None:
            cutoff = max(cutoff, bisect.bisect_left(times, reference_time - self.max_age))
        return cutoff

    def _replay(self) -> None:
        """Rebuild self._times/self._columns from the CSV's surviving data rows.

        A backward, growing-chunk scan finds the earliest "F" row that
        already covers the desired retention window (bounded by
        full_vector_interval, unlike an unbounded backward search), then
        replays forward from there and trims to the exact window.

        Uses wall-clock time.time() (not the file's own last timestamp)
        as the max_age reference, so a long process downtime doesn't
        resurrect data that would immediately be re-trimmed once ticking
        resumes."""
        file_size = self.csv_path.stat().st_size
        if file_size <= self._data_start:
            return

        reference_time = time.time()
        chunk_size = _INITIAL_CHUNK_SIZE
        lines: list[str] = []
        f_positions: list[int] = []
        with open(self.csv_path, "rb") as f:
            while True:
                start = max(self._data_start, file_size - chunk_size)
                f.seek(start)
                if start > self._data_start:
                    f.readline()  # discard partial leading line, snap to a line boundary
                chunk = f.read().decode("utf-8")
                lines = chunk.split("\n")
                if lines and lines[-1] == "":
                    lines.pop()
                f_positions = [i for i, line in enumerate(lines) if line.startswith("F,")]
                reached_start = start <= self._data_start
                covered = f_positions and self._window_covered(
                    lines, f_positions[0], reference_time)
                if reached_start or covered:
                    break
                chunk_size *= 2

        if not f_positions:
            logger.warning("no full-vector row found in %s; starting with empty history",
                            self.csv_path)
            return

        anchor = f_positions[0]
        current = {label: _NAN for label in self.labels}
        times: list[float] = []
        rows: list[dict[str, float]] = []
        for line in lines[anchor:]:
            t = self._parse_row(line, current)
            times.append(t)
            rows.append(dict(current))

        cutoff = self._retention_cutoff(times, reference_time)
        times, rows = times[cutoff:], rows[cutoff:]

        for t, row in zip(times, rows, strict=True):
            self._times.append(t)
            for label in self.labels:
                self._columns[label].append(row[label])
        if rows:
            self._last_written = dict(rows[-1])
        logger.info("%s: loaded %d record(s) from existing file", self.csv_path, len(self._times))
