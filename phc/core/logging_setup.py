"""Logging setup for the "phc" logger tree.

One or more independently levelled destinations (stream or file), plus
an in-place (carriage return, no newline) status line for the
scheduler's live task countdown."""

import logging
import sys
from pathlib import Path


class _InPlaceLineState:
    """Shared between InPlaceLineHandler and NewlineSafeStreamHandler.

    Lets a normal log record know -- and clear -- whether an in-place
    (no trailing newline) line is currently open on the stream. Scoped
    per underlying stream object (stdout and stderr each get their own),
    so two stream destinations sharing one tty don't interleave a
    pending '\\r' line from one with a normal record from the other."""

    def __init__(self):
        self.is_open = False


class _BrokenPipeSilentMixin:
    """Suppresses handleError()'s traceback dump for a broken stream.

    E.g. Ctrl-C while stdout is piped to a process that has already
    exited -- Windows raises OSError EINVAL on the next write/flush,
    not BrokenPipeError. Nothing can be done about a stream that's
    gone, and a shutdown-time record failing to print isn't worth the
    noise; any other handler error still surfaces normally."""

    def handleError(self, record):
        if sys.exc_info()[0] in (BrokenPipeError, OSError):
            return
        super().handleError(record)


class InPlaceLineHandler(_BrokenPipeSilentMixin, logging.Handler):
    """Writes each record as '\\r' + message, no trailing newline.

    So repeated calls overwrite the same terminal line instead of
    scrolling. Used for the scheduler's live per-tick task countdown --
    a status line re-rendered every heartbeat, not a discrete event
    worth a full log line each time. Only ever installed on the first
    stream destination (see configure_logging) -- file destinations
    never receive in-place records at all, since a carriage-return
    status line is a terminal affordance that makes no sense appended
    to a log file."""

    def __init__(self, stream, state: _InPlaceLineState):
        super().__init__()
        self.stream = stream
        self._state = state

    def emit(self, record):
        """Write `record` in place (leading '\\r', no trailing newline)."""
        try:
            self.stream.write("\r" + self.format(record))
            self.stream.flush()
            self._state.is_open = True
        except Exception:
            self.handleError(record)


class NewlineSafeStreamHandler(_BrokenPipeSilentMixin, logging.StreamHandler):
    """A normal, newline-terminated StreamHandler with in-place cleanup.

    Before emitting each record, closes any pending in-place line (see
    InPlaceLineHandler) with a bare newline first, so the new record
    always starts on its own line. This makes the in-place-to-normal
    switch automatic for every logger and every record; no call site
    needs to remember to do anything."""

    def __init__(self, stream, state: _InPlaceLineState):
        super().__init__(stream)
        self._state = state

    def emit(self, record):
        """Close any pending in-place line, then emit `record` normally."""
        if self._state.is_open:
            try:
                self.stream.write("\n")
                self.stream.flush()
            except Exception:
                self.handleError(record)
            self._state.is_open = False
        super().emit(record)


def _parse_level(level_name, default: int) -> int:
    """Resolve a level name (e.g. "DEBUG") to its logging module constant.

    Falls back to `default` if `level_name` is None."""
    if level_name is None:
        return default
    return getattr(logging, str(level_name).upper(), default)


class _LevelMapFilter(logging.Filter):
    """Per-destination logger-name -> level filter.

    A destination's `levels: {default: LEVEL, <name>: LEVEL, ...}` can't be
    expressed by handler.setLevel() -- that's a single level for the whole
    handler, but different destinations (and different loggers within one
    destination) may want different levels. This resolves each record's
    logger name against the map instead: "<name>" matches the dotted
    suffix of a "phc.<name>" logger (e.g. "scheduler" -> "phc.scheduler");
    the bare "phc" logger itself (phc.py's own startup/shutdown lines) is
    matched by the literal key "phc", not by stripping a "phc." prefix that
    isn't there. "default" applies to any logger with no more specific
    entry. Name -> level lookups are memoized per filter instance, since
    the small, fixed set of logger names in this process never changes at
    runtime."""

    def __init__(self, levels: dict, default_level: int):
        super().__init__()
        self._default_level = default_level
        self._overrides = {name: _parse_level(value, default_level)
                            for name, value in levels.items() if name != "default"}
        self._resolved: dict[str, int] = {}

    def _level_for(self, name: str) -> int:
        if name not in self._resolved:
            key = name[len("phc."):] if name.startswith("phc.") else name
            self._resolved[name] = self._overrides.get(key, self._default_level)
        return self._resolved[name]

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self._level_for(record.name)


def configure_logging(log_config: list | None, log_levels_override: dict | None = None,
                       config_dir: Path | None = None) -> None:
    """Configure the "phc" logger tree from `log_config`.

    A list of independently levelled destinations -- see
    docs/configuration.md for the `log:` YAML schema.

    `dest` is "stdout"/"stderr" for a stream, or any other string for a
    file path -- resolved relative to `config_dir` (the system YAML's own
    directory; deliberately NOT relative to the file the `dest:` value
    itself came from via !include, since !include discards that
    provenance -- see phc.core.config._include_constructor), opened in append
    mode, lazily (no file is created until the first record for it).

    `log_levels_override` (CLI --log-level/--log-level-module) is merged
    into every STREAM destination's levels only, never a file
    destination's -- the CLI flags mean "make my console louder/quieter",
    and applying them to a file dest would permanently pollute the sparse
    log a user configured for exactly the opposite reason.

    The "phc" root logger itself is set to the minimum level requested by
    any destination, so nothing is dropped before a handler's own
    per-destination filter gets a chance to see it. Per-logger levels
    (logging.getLogger("phc.<name>").setLevel(...)) are deliberately never
    set: one logger can hold only one level, but two destinations may
    legitimately want different levels for the same logger.

    At most one destination gets the in-place countdown-line handler (the
    first stream destination) -- installing it on two streams could
    interleave two independently rewound '\\r' lines on one terminal.

    Absent/empty log_config defaults to a single stdout destination at
    INFO. Raises ValueError if log_config is a mapping rather than a list,
    or if two destinations share one `dest`. Closes any handlers
    previously installed on "phc" before installing the new ones, so
    repeated calls (load_system() makes one every time it's called)
    don't accumulate open file descriptors -- harmless for a
    StreamHandler, but a FileHandler holds a real fd.
    """
    if isinstance(log_config, dict):
        raise ValueError(
            "log: must be a list of destinations, e.g. "
            "[{dest: stdout, levels: {default: INFO}}] -- the old single-mapping "
            "log: {dest: stdout} form (with a separate top-level log_levels:) has been removed")
    log_config = log_config or [{"dest": "stdout"}]
    log_levels_override = log_levels_override or {}

    seen_dests = set()
    handlers: list[logging.Handler] = []
    all_levels: list[int] = []
    in_place_installed = False
    state_by_stream: dict[int, _InPlaceLineState] = {}
    record_formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    for dest_entry in log_config:
        dest = dest_entry.get("dest", "stdout")
        if dest in seen_dests:
            raise ValueError(f"log: duplicate dest {dest!r}")
        seen_dests.add(dest)

        levels = dict(dest_entry.get("levels") or {})
        is_stream = dest in ("stdout", "stderr")
        if is_stream:
            levels.update(log_levels_override)

        default_level = _parse_level(levels.get("default"), logging.INFO)
        all_levels.append(default_level)
        all_levels.extend(_parse_level(v, default_level) for k, v in levels.items() if k != "default")
        level_filter = _LevelMapFilter(levels, default_level)
        def not_in_place(record):
            """Keep every record except the scheduler's in-place status line."""
            return not getattr(record, "in_place", False)

        if is_stream:
            stream = sys.stderr if dest == "stderr" else sys.stdout
            state = state_by_stream.setdefault(id(stream), _InPlaceLineState())

            handler = NewlineSafeStreamHandler(stream, state)
            handler.setFormatter(record_formatter)
            handler.addFilter(not_in_place)
            handler.addFilter(level_filter)
            handlers.append(handler)

            if not in_place_installed:
                in_place_handler = InPlaceLineHandler(stream, state)
                in_place_handler.setFormatter(logging.Formatter("%(message)s"))
                in_place_handler.addFilter(lambda record: getattr(record, "in_place", False))
                in_place_handler.addFilter(level_filter)
                handlers.append(in_place_handler)
                in_place_installed = True
        else:
            path = Path(dest)
            if config_dir is not None and not path.is_absolute():
                path = config_dir / path
            handler = logging.FileHandler(path, mode="a", encoding="utf-8", delay=True)
            handler.setFormatter(record_formatter)
            handler.addFilter(not_in_place)
            handler.addFilter(level_filter)
            handlers.append(handler)

    root = logging.getLogger("phc")
    for old_handler in root.handlers:
        old_handler.close()
    root.setLevel(min(all_levels, default=logging.INFO))
    root.handlers = handlers
    root.propagate = False
