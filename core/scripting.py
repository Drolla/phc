"""A restricted-Python sandbox shared by condition expressions and scripted
actions (see core.task's ExprCondition/ScriptAction). Purely mechanical: this
module knows nothing about Device/Task/Endpoint -- it only validates a
Python AST against a small allow-list, compiles it, and runs it against a
caller-supplied namespace dict. What that namespace actually contains (which
functions are available to conditions vs. scripts) is decided in
core.task._build_rule_namespace, the one place that owns that policy.

This targets mistake-containment for a trusted, locally-authored system YAML
file -- not a hostile-author threat model. The whitelist blocks the obvious
footguns (imports, attribute/dunder escapes, unbounded loops, arbitrary
method-call chains) so a typo'd condition fails loudly at config-load time
instead of doing something surprising, not to sandbox an adversarial author."""

import ast
import builtins
import logging

logger = logging.getLogger("phc.scripting")

# Cap for the single-line source snippet in a debug log message (see
# _snippet(), used by compile_expression/compile_script) -- long enough to
# recognize the expression/script at a glance, short enough to not flood
# the log on every tick.
_LOG_SNIPPET_LENGTH = 60


def _snippet(source: str) -> str:
    """Collapse `source` to one line (join()ing on whitespace, so a
    multi-line script's newlines/indentation don't break up a log line) and
    truncate to _LOG_SNIPPET_LENGTH characters."""
    collapsed = " ".join(source.split())
    if len(collapsed) > _LOG_SNIPPET_LENGTH:
        return collapsed[:_LOG_SNIPPET_LENGTH] + "..."
    return collapsed

# Endpoint properties reachable via `.attr` on a bound EndpointRef (see below)
# -- the only attribute names this sandbox permits at all.
_ALLOWED_ATTRIBUTES = frozenset({"state", "changed", "text", "event", "sticky"})

# Namespace functions whose first argument -- if a string literal -- names a
# "<device>.<endpoint>" path. compile_expression()/compile_script() collect
# these as `referenced_paths`, so core.config can validate/subscribe an
# endpoint referenced this way exactly like one declared via `refs:`.
_PATH_TAKING_FUNCTIONS = frozenset({"state", "changed", "text", "event", "sticky",
                                     "set_state", "reset_sticky"})

# A small, fixed subset of real builtins -- no eval/exec/getattr/open/
# __import__/dir/type/vars/isinstance reachable from a namespace, since
# `{"__builtins__": _SAFE_BUILTINS}` replaces Python's real builtins module
# rather than extending it.
_SAFE_BUILTINS = {name: getattr(builtins, name) for name in
                  ("abs", "min", "max", "len", "round", "int", "float", "bool", "str", "sorted")}

# Structurally allowed AST node types. Absence is enough to reject a
# construct (import, def/class/lambda, while, try/raise, with, comprehensions,
# subscript, walrus, augmented assignment, ...); the additional checks in
# _validate() below narrow a few of these further (Attribute, Call, Assign,
# For) since the node *type* alone doesn't capture those constraints.
_ALLOWED_NODE_TYPES = (
    ast.Module, ast.Expression, ast.Expr, ast.Load, ast.Store,
    ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set,
    ast.JoinedStr, ast.FormattedValue,
    ast.Name, ast.Attribute,
    ast.BoolOp, ast.And, ast.Or,
    ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.IfExp, ast.Call,
    ast.Assign, ast.If, ast.For, ast.Pass,
)


class ScriptError(Exception):
    """Raised for a condition/script source that fails to parse or violates
    the sandbox whitelist. Callers (core.config's _build_condition/
    _build_action) catch this at config-load time and re-raise as
    ConfigError, naming the offending task."""


def _validate(tree: ast.AST) -> frozenset[str]:
    """Walk every node in `tree`, raising ScriptError on the first construct
    outside the whitelist. Returns the set of string-literal path arguments
    passed to a _PATH_TAKING_FUNCTIONS call (see module docstring)."""
    referenced_paths: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODE_TYPES):
            raise ScriptError(f"disallowed syntax: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise ScriptError(f"disallowed name: {node.id!r}")
        if isinstance(node, ast.Attribute) and node.attr not in _ALLOWED_ATTRIBUTES:
            raise ScriptError(f"disallowed attribute: {node.attr!r}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ScriptError("calls are only allowed on a plain function name")
            if node.func.id in _PATH_TAKING_FUNCTIONS and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    referenced_paths.add(first.value)
        elif isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                raise ScriptError("assignment target must be a single name")
        elif isinstance(node, ast.For) and not isinstance(node.target, ast.Name):
            raise ScriptError("for-loop target must be a single name")
    return frozenset(referenced_paths)


class Compiled:
    """A validated, compiled expression or script, ready for repeated
    evaluate_expression()/run_script() calls against different namespaces
    (e.g. once per Scheduler tick)."""

    __slots__ = ("code", "referenced_paths")

    def __init__(self, code, referenced_paths: frozenset[str]):
        self.code = code
        self.referenced_paths = referenced_paths


def compile_expression(source: str) -> Compiled:
    """Validate and compile a condition's `expr:` source. Raises ScriptError
    on invalid syntax or a whitelist violation."""
    logger.debug("compile expr: %s", _snippet(source))
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ScriptError(f"invalid expression syntax: {exc}") from None
    compiled = Compiled(compile(tree, "<condition-expr>", "eval"), _validate(tree))
    logger.debug("compile expr done: referenced path(s): %s", sorted(compiled.referenced_paths))
    return compiled


def compile_script(source: str) -> Compiled:
    """Validate and compile a script action's `code:` source. Raises
    ScriptError on invalid syntax or a whitelist violation."""
    logger.debug("compile script: %s", _snippet(source))
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise ScriptError(f"invalid script syntax: {exc}") from None
    compiled = Compiled(compile(tree, "<script-action>", "exec"), _validate(tree))
    logger.debug("compile script done: referenced path(s): %s", sorted(compiled.referenced_paths))
    return compiled


def evaluate_expression(compiled: Compiled, namespace: dict):
    """Evaluate a compile_expression() result against `namespace`. A runtime
    error (e.g. NameError for a typo'd ref) is not caught here -- it
    propagates to the caller, same as any other action/condition failure."""
    logger.debug("evaluate expr: namespace %s", sorted(namespace))
    result = eval(compiled.code, {"__builtins__": _SAFE_BUILTINS, **namespace})
    logger.debug("evaluate expr done: result %r", result)
    return result


def run_script(compiled: Compiled, namespace: dict) -> None:
    """Execute a compile_script() result against `namespace`. See
    evaluate_expression() re: runtime error handling."""
    logger.debug("run script: namespace %s", sorted(namespace))
    exec(compiled.code, {"__builtins__": _SAFE_BUILTINS, **namespace})
    logger.debug("run script done")


class EndpointRef:
    """Lazy, read-only view of one device endpoint against a specific
    namespace-construction's live `devices` dict -- backs both the `refs:`
    attribute-access sugar (`sensor_a.state`) and, via the same underlying
    Endpoint, the state()/changed()/text()/event()/sticky() namespace
    functions. Resolved at attribute-access time, not at construction, since
    `devices` is rebuilt fresh every tick.

    `subscriber_id` (typically the owning task's tag) backs `.sticky`, which
    forwards to Endpoint.get_log_value() -- see core.endpoint's sticky log
    value methods and core.task's sticky-subscription wiring."""

    def __init__(self, device_id: str, endpoint_key: str, devices: dict, subscriber_id: str | None = None):
        self._device_id = device_id
        self._endpoint_key = endpoint_key
        self._devices = devices
        self._subscriber_id = subscriber_id

    def _endpoint(self):
        return self._devices[self._device_id].endpoint(self._endpoint_key)

    @property
    def state(self):
        return self._endpoint().get()

    @property
    def changed(self) -> bool:
        return self._endpoint().get_event() is not None

    @property
    def text(self):
        return self._endpoint().to_text()

    @property
    def event(self):
        return self._endpoint().get_event()

    @property
    def sticky(self):
        return self._endpoint().get_log_value(self._subscriber_id)
