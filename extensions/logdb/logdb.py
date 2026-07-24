"""LogDb: a CSV-persisted, in-memory-buffered log of numeric samples.

Decoupled from core.device/core.task: log() takes a plain timestamp and a
{label: float} dict, and the query methods are timestamp-oriented. Single-
writer, no file locking -- fine for PHC's single-process asyncio model, not
safe for multi-process use.

CSV format (see _reconcile_header/_replay for the full algorithm):

    phc-logdb,version=1
    time,house.desk_lamp/power,house.desk_lamp/brightness,living_light/state<padding...>
    F,1721830000.0,1.0,80.0,0.0
    D,1721830030.0,,,1.0
    D,1721830060.0,0.0,nan,
    F,1721833000.0,0.0,nan,1.0

Line 1 is a short fixed info line. Line 2 is the label header, padded with
trailing spaces to a reserved width so it can be rewritten in place if new
labels are added later (see _reconcile_header) -- labels only ever grow,
never shrink. Each data row is explicitly marked full ("F", every cell
populated) or delta ("D", a blank cell means "unchanged since the previous
row", "nan" marks an explicit transition to no-data). Marking rows
explicitly (rather than inferring "full" from a row-count parity) is
required because header growth can sacrifice leading rows (see below),
which would otherwise desync any inferred parity.
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
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __len__(self) -> int:
        return len(self._times)

    # ---------- public logging API ----------

    def log(self, timestamp: float, values: dict[str, float]) -> None:
        """Append one sample row. `values` maps a subset (or all) of
        self.labels to a numeric reading; any configured label not present
        this call is stored as NaN ("no data this sample"). Trims per
        max_records/max_age (with slack), then writes exactly one row to
        the CSV, flushed immediately -- at realistic sample intervals
        (seconds to minutes, not a hot loop) the extra syscall per row is
        negligible next to the crash-safety it buys."""
        unknown = set(values) - set(self._columns)
        if unknown:
            logger.warning("logdb %s: ignoring unknown label(s) %s", self.csv_path, sorted(unknown))
        self._times.append(timestamp)
        for label, col in self._columns.items():
            if label in values and label not in unknown:
                col.append(float(values[label]))
            else:
                col.append(_NAN)
        self._maybe_trim()
        self._write_row(timestamp)

    def _maybe_trim(self) -> None:
        """Drop oldest records once the buffer exceeds max_records/max_age
        by more than trim_slack, amortizing array.array's O(n) shift
        across many log() calls instead of paying it on every single
        insert. Between trims the buffer may exceed the configured limit
        by up to 10%; documented, not silent."""
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

    def _write_row(self, timestamp: float) -> None:
        full = self._rows_since_full == 0
        cells = ["F" if full else "D", repr(timestamp)]
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
        return "nan" if math.isnan(value) else repr(value)

    # ---------- query API ----------

    def get_at(self, timestamp: float, labels: list[str] | None = None) -> dict[str, float] | None:
        """The record nearest to `timestamp` (either side), or None if
        empty. O(log n) via bisect on the always time-ascending _times."""
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
        """Every record with start <= time <= end, ascending. O(log n + k)."""
        lo = bisect.bisect_left(self._times, start)
        hi = bisect.bisect_right(self._times, end)
        return [self._row_at(pos, labels) for pos in range(lo, hi)]

    def _row_at(self, pos: int, labels: list[str] | None) -> dict[str, float]:
        keys = labels if labels is not None else self.labels
        row = {"time": self._times[pos]}
        for label in keys:
            row[label] = self._columns[label][pos]
        return row

    # ---------- header reconciliation ----------

    def _desired_header_width(self, needed: int) -> int:
        if self._header_reserve_bytes is not None:
            return max(int(self._header_reserve_bytes), needed)
        return max(_MIN_HEADER_RESERVE, needed * 2)

    def _pad_header(self, content: str, width: int) -> bytes:
        content_bytes = content.encode("utf-8")
        pad = width - len(content_bytes) - 1  # -1 for the trailing "\n"
        if pad < 0:
            pad = 0
        return content_bytes + b" " * pad + b"\n"

    def _write_fresh_header(self, labels: list[str]) -> None:
        info = f"{_INFO_LINE_PREFIX}{_VERSION}\n".encode("utf-8")
        content = "time," + ",".join(labels)
        width = self._desired_header_width(len(content.encode("utf-8")) + 1)
        with open(self.csv_path, "wb") as f:
            f.write(info)
            f.write(self._pad_header(content, width))

    def _rotate_existing_file(self) -> None:
        backup = self.csv_path.with_name(self.csv_path.name + f".{int(time.time())}.bak")
        self.csv_path.rename(backup)

    def _consume_leading_lines(self, data_start: int, extra_needed: int) -> tuple[int, int]:
        """Read forward from `data_start`, accumulating whole lines' byte
        lengths until at least `extra_needed` bytes have been consumed (or
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
        """Read/create the CSV header, growing it in place to cover any
        newly configured labels not already persisted. Sets self._data_start
        to the byte offset where data rows begin. Returns the final,
        combined label list (persisted labels, in their original order,
        followed by any newly configured ones)."""
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
            logger.warning("logdb: %s has an unrecognized header; rotating and starting fresh",
                            self.csv_path)
            self._rotate_existing_file()
            self._write_fresh_header(configured_labels)
            self._data_start = self.csv_path.stat().st_size
            return list(configured_labels)

        header_start = len(info_line)
        header_text = header_line.decode("utf-8").rstrip("\n").rstrip(" ")
        persisted_labels = header_text.split(",")[1:]

        new_labels = [l for l in configured_labels if l not in persisted_labels]
        combined_labels = persisted_labels + new_labels

        if not new_labels:
            self._data_start = data_start
            return combined_labels

        new_content = "time," + ",".join(combined_labels)
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
        covers the full desired retention window, so the backward scan can
        stop growing its chunk. Unbounded (no max_records/max_age) never
        stops early -- the scan reads back to the start of the data."""
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
        """Parse one data row, updating `current` (label -> value) in place:
        a blank cell means "unchanged" (keep current[label]); a missing
        trailing cell (row shorter than self.labels, i.e. that label didn't
        exist yet when this row was written) means NaN; anything else is
        parsed as a float ("nan" -> NaN). Returns the row's timestamp.
        Caller must snapshot `current` (dict(current)) before the next call,
        since it's mutated in place."""
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
        n = len(times)
        cutoff = 0
        if self.max_records is not None:
            cutoff = max(cutoff, n - self.max_records)
        if self.max_age is not None:
            cutoff = max(cutoff, bisect.bisect_left(times, reference_time - self.max_age))
        return cutoff

    def _replay(self) -> None:
        """Rebuild self._times/self._columns from the CSV's surviving data
        rows: a backward, growing-chunk scan finds the earliest "F" row
        that already covers the desired retention window (bounded by
        full_vector_interval, unlike an unbounded backward search), then
        replays forward from there and trims to the exact window. Uses
        wall-clock time.time() (not the file's own last timestamp) as the
        max_age reference, so a long process downtime doesn't resurrect
        data that would immediately be re-trimmed once ticking resumes."""
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
                if reached_start or (f_positions and self._window_covered(lines, f_positions[0], reference_time)):
                    break
                chunk_size *= 2

        if not f_positions:
            logger.warning("logdb: no full-vector row found in %s; starting with empty history",
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

        for t, row in zip(times, rows):
            self._times.append(t)
            for label in self.labels:
                self._columns[label].append(row[label])
        if rows:
            self._last_written = dict(rows[-1])
