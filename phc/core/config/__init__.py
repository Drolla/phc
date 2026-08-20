"""System YAML loading: parses a system config file into a System (devices,
tasks, and scheduler settings), merging each device's params/endpoints
against its module's declared module.yaml along the way.

This was a single 1450-line module. It is now a package, one module per
stage of the load, in dependency order:

- `yamlio` -- the `!include`/`!placeholder` tags; file to nested dict.
- `descriptors` -- parsed module.yaml/extension.yaml, and the key sets
  defining what a config entry may contain.
- `params` -- parameter scope/override resolution, `modules.<name>:`.
- `endpoints` -- profile expansion, `{param}` templating, Endpoint
  construction, interval/history parsing.
- `devices` -- a `devices:` entry into a Device tree.
- `extensions` -- the `extensions:` section into configured instances.
- `tasks` -- a `tasks:` entry into a Task, with its condition and actions.
- `hooks` -- the per-tick hooks the loader synthesizes (sticky windows,
  history sampling).
- `system` -- the System object and `load_system()`, tying it together.

Every name the package exposed as a flat module is re-exported here, so
`from phc.core.config import load_system` (and the private names the test
suite reaches for) keeps working unchanged. Import from a submodule
directly when you want to be explicit about which stage you mean.
"""

from phc.core.config.descriptors import (
                                          ExtensionDescriptor,
                                          ModuleDescriptor,
                                          _load_extension_descriptor,
                                          _load_module_descriptor,
                                          _parse_profile_library,
                                          _read_descriptor_yaml,
)
from phc.core.config.devices import _build_device
from phc.core.config.endpoints import (
                                          _expand_endpoint_specs,
                                          _merge_endpoints,
                                          _overlay_endpoint_spec,
                                          _parse_history_spec,
                                          _parse_interval_value,
                                          _resolve_interval,
                                          _substitute_endpoint_spec,
                                          _substitute_templates,
)
from phc.core.config.extensions import _load_extensions, _merge_extension_params
from phc.core.config.hooks import _collect_history_records, _make_history_tick_hook, _make_sticky_tick_hook
from phc.core.config.params import _build_effective_module, _merge_params, _resolve_module_config
from phc.core.config.system import System, load_system
from phc.core.config.tasks import (
                                          _build_action,
                                          _build_condition,
                                          _build_task,
                                          _subscribe_referenced_endpoints,
)
from phc.core.config.yamlio import _find_placeholders, _flatten_list_entries, _IncludeLoader, _Placeholder
from phc.core.errors import ConfigError

__all__ = ["ConfigError", "System", "load_system"]
