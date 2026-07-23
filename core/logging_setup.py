"""Logging setup for the "phc" logger tree, including an in-place (carriage
return, no newline) status line for the scheduler's live task countdown."""

import logging
import sys


class _InPlaceLineState:
    """Shared between InPlaceLineHandler and NewlineSafeStreamHandler: lets
    a normal log record know -- and clear -- whether an in-place (no
    trailing newline) line is currently open on the stream."""

    def __init__(self):
        self.is_open = False


class InPlaceLineHandler(logging.Handler):
    """Writes each record as '\\r' + message, with no trailing newline, so
    repeated calls overwrite the same terminal line instead of scrolling.
    Used for the scheduler's live per-tick task countdown -- a status line
    re-rendered every heartbeat, not a discrete event worth a full log
    line each time."""

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


class NewlineSafeStreamHandler(logging.StreamHandler):
    """A normal, newline-terminated StreamHandler that -- before emitting
    each record -- closes any pending in-place line (see
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
    """Resolve a level name (e.g. "DEBUG") to its logging module constant,
    falling back to `default` if `level_name` is None."""
    if level_name is None:
        return default
    return getattr(logging, str(level_name).upper(), default)


def configure_logging(log_config: dict | None, log_levels: dict | None = None) -> None:
    """Configure the "phc" logger tree.

    log_config: {"dest": "stdout"|"stderr"} -- where log output goes.
    log_levels: {"default": LEVEL, "<name>": LEVEL, ...} -- "default" sets
    the base level for the whole tree; any other key sets the level of
    logging.getLogger(f"phc.{name}") individually (e.g. "scheduler" ->
    "phc.scheduler"). Name-agnostic: no fixed list of known loggers.
    """
    log_config = log_config or {}
    log_levels = log_levels or {}

    dest = log_config.get("dest", "stdout")
    stream = sys.stderr if dest == "stderr" else sys.stdout

    default_level = _parse_level(log_levels.get("default"), logging.INFO)
    overrides = {name: _parse_level(value, default_level)
                 for name, value in log_levels.items() if name != "default"}

    # The handlers must allow through the most verbose level in play, or a
    # module overridden to be more verbose than the default would be
    # filtered at the handler stage regardless of its own logger level.
    # The root logger itself is set to `default_level` (not the min) so
    # that any logger WITHOUT an explicit override correctly inherits
    # `default` via Python's logger-hierarchy walk, rather than picking up
    # some other module's more-verbose override.
    handler_level = min([default_level, *overrides.values()], default=default_level)

    state = _InPlaceLineState()

    handler = NewlineSafeStreamHandler(stream, state)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(lambda record: not getattr(record, "in_place", False))
    handler.setLevel(handler_level)

    in_place_handler = InPlaceLineHandler(stream, state)
    in_place_handler.setFormatter(logging.Formatter("%(message)s"))
    in_place_handler.addFilter(lambda record: getattr(record, "in_place", False))
    in_place_handler.setLevel(handler_level)

    root = logging.getLogger("phc")
    root.setLevel(default_level)
    root.handlers = [handler, in_place_handler]
    root.propagate = False

    for name, level in overrides.items():
        logging.getLogger(f"phc.{name}").setLevel(level)
