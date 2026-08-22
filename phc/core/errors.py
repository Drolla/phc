"""Exception types shared across PHC.

Deliberately dependency-free -- it imports nothing from the rest of the
package, so anything may import it without creating a cycle.
"""


class PhcError(Exception):
    """Base for every error PHC raises deliberately.

    Lets a caller distinguish "PHC rejected this" from an arbitrary
    TypeError/AttributeError escaping a bug, without enumerating
    subclasses.
    """


class ConfigError(PhcError):
    """Raised for any invalid or inconsistent system YAML."""
