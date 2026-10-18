"""Extensions: the lifecycle-hook contract, and loading `extensions:`.

extension.py is the contract extension instances follow; config.py
configures instances from a system YAML. Re-exports extension.py.
"""

from phc.core.extension.extension import (
    LIFECYCLE_HOOKS,
    Extension,
    check_lifecycle_hooks,
    collect_hook,
)
