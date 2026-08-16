"""Exception types shared across PHC.

Deliberately dependency-free -- it imports nothing from the rest of the
package, so anything may import it without creating a cycle. That is the
whole reason it exists: `ConfigError` used to live in `phc.core.config`,
which meant a leaf module like `phc.core.selectors` had to import the
top-of-the-stack config loader just to name an exception, and every
extension did the same. The dependency arrow now points here instead,
which is the only direction that can never loop.
"""


class PhcError(Exception):
    """Base for every error PHC raises deliberately.

    Lets a caller distinguish "PHC rejected this" from an arbitrary
    TypeError/AttributeError escaping a bug, without enumerating
    subclasses.
    """


class ConfigError(PhcError):
    """Raised for any invalid or inconsistent system YAML."""
