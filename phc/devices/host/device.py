"""HostDevice: a pure grouping device used to organize other devices into a tree."""

from phc.core.device import Device
from phc.core.registry import register_module


@register_module("host")
class HostDevice(Device):
    """Pure grouping device: no endpoints, only children.

    All behavior is inherited unchanged from Device."""
    pass
