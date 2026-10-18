"""System YAML loading: parses a system config file into a System.

This package holds what is not owned by any one config section:

- `expand` -- full-line `!include` directives spliced into one YAML
  text.
- `yamlio` -- that text parsed (with `!placeholder`) into a nested dict.
- `system` -- the System object and `load_system()`, which hands each
  top-level section to its owner: phc.core.logging_setup (`log:`),
  phc.core.registry (`plugin_paths:`), phc.core.scheduler
  (`heartbeat:`/`max_workers:`/`fetch_timeout:`), phc.core.intervals
  (`intervals:`), phc.core.device.config (`modules:`/`devices:`, with
  phc.core.endpoint.config for endpoints), phc.core.extension.config
  (`extensions:`) and phc.core.task.config (`task_specs:`/`tasks:`).

Re-exports `load_system`/`System`/`ConfigError`, plus the private names
the test suite reaches for, from wherever they live.
"""

from phc.core.config.system import System, load_system
from phc.core.config.yamlio import _ConfigLoader, _find_placeholders, _Placeholder, load_yaml
from phc.core.device.config import (
                                          ModuleDescriptor,
                                          _build_device,
                                          _build_effective_module,
                                          _load_module_descriptor,
                                          _merge_params,
                                          _parse_profile_library,
                                          _resolve_module_config,
)
from phc.core.endpoint.config import (
                                          _collect_history_records,
                                          _expand_endpoint_specs,
                                          _make_history_tick_hook,
                                          _merge_endpoints,
                                          _overlay_endpoint_spec,
                                          _parse_history_spec,
                                          _parse_interval_value,
                                          _resolve_interval,
                                          _substitute_endpoint_spec,
                                          _substitute_templates,
)
from phc.core.errors import ConfigError
from phc.core.extension.config import (
                                          ExtensionDescriptor,
                                          _load_extension_descriptor,
                                          _load_extensions,
                                          _merge_extension_params,
)
from phc.core.registry import _read_descriptor_yaml
from phc.core.task.config import (
                                          _build_action,
                                          _build_condition,
                                          _build_task,
                                          _make_sticky_tick_hook,
                                          _subscribe_referenced_endpoints,
)

__all__ = ["ConfigError", "System", "load_system"]
