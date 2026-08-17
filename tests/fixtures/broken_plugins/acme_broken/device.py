"""A plugin whose own import fails, for the discovery error-reporting test.

Discovery used to swallow ModuleNotFoundError, which made this
indistinguishable from a package that simply has no device.py -- so a
plugin with a missing dependency silently did not exist, and the config
naming it failed later with a misleading "unknown module".
"""

import a_dependency_that_is_not_installed  # noqa: F401

from phc.core.device import Device
from phc.core.registry import register_module


@register_module("acme_broken")
class AcmeBrokenDevice(Device):
    """Never actually registered -- the import above fails first."""
