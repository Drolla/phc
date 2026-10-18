"""Tasks: Task, conditions, actions, and loading `task_specs:`/`tasks:`.

task.py is the runtime side; config.py builds tasks from a system
YAML. Re-exports task.py.
"""

from phc.core.task.task import (
    Action,
    Condition,
    CreateTaskAction,
    ExprCondition,
    KillTaskAction,
    LogAction,
    ScriptAction,
    SetAction,
    Task,
    TaskRegistry,
    ToggleAction,
    _build_rule_namespace,
    resolve_endpoint_ref,
)
