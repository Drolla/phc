"""The extension lifecycle contract.

Hooks are discovered by name, so a misspelled one is not a broken hook --
it is not a hook at all, and the extension silently never runs it. These
tests pin the check that turns that into a startup error, and the hook
collection itself.
"""

import pytest

from phc.core.errors import ConfigError
from phc.core.extension import LIFECYCLE_HOOKS, Extension, check_lifecycle_hooks, collect_hook


class FullExtension:
    """Implements every hook."""

    def on_bind(self, system): pass
    async def on_start(self, devices): pass
    def on_tick(self, devices): pass
    async def on_stop(self, devices): pass


class MinimalExtension:
    """Implements none -- valid; mail_alert is like this."""


# ---------- hook collection ----------

def test_collect_hook_returns_only_implementing_instances():
    instances = {"a": FullExtension(), "b": MinimalExtension(), "c": FullExtension()}
    assert len(collect_hook(instances, "on_tick")) == 2
    assert collect_hook(instances, "on_bind") != []


def test_collect_hook_preserves_registry_order():
    """Extensions are configured in declaration order, and some depend on
    that (a hook registered earlier runs earlier)."""
    first, second = FullExtension(), FullExtension()
    hooks = collect_hook({"first": first, "second": second}, "on_tick")
    assert [h.__self__ for h in hooks] == [first, second]


def test_an_extension_with_no_hooks_is_valid():
    assert collect_hook({"x": MinimalExtension()}, "on_tick") == []
    check_lifecycle_hooks(MinimalExtension(), "x")   # must not raise


# ---------- typo detection ----------

@pytest.mark.parametrize("typo", ["on_tik", "on_tickk", "on_starts", "on_stopp", "on_bindd"])
def test_a_misspelled_hook_is_rejected_with_the_intended_name(typo):
    """The failure this exists to prevent: the extension loads fine and
    simply never does its job."""
    instance = type("Typo", (), {typo: lambda self, x=None: None})()
    with pytest.raises(ConfigError) as excinfo:
        check_lifecycle_hooks(instance, "ext.instance")
    message = str(excinfo.value)
    assert typo in message
    assert "ext.instance" in message
    assert any(hook in message for hook in LIFECYCLE_HOOKS), "should name the intended hook"


def test_correctly_named_hooks_pass():
    check_lifecycle_hooks(FullExtension(), "ext.instance")   # must not raise


def test_an_unrelated_on_method_is_left_alone():
    """The check must be conservative: an extension is free to have its
    own callbacks, and flagging them would break valid extensions."""
    class WithOwnCallbacks:
        def on_message(self, payload): pass
        def on_connection_lost(self): pass
        def on_tick(self, devices): pass

    check_lifecycle_hooks(WithOwnCallbacks(), "ext.instance")   # must not raise


def test_non_callable_attributes_are_ignored():
    class WithData:
        on_ticks = [1, 2, 3]      # data, not a misspelled hook

    check_lifecycle_hooks(WithData(), "ext.instance")   # must not raise


# ---------- the protocol ----------

def test_protocol_matches_a_full_implementation():
    assert isinstance(FullExtension(), Extension)


def test_protocol_is_documentation_not_a_base_class():
    """PHC looks for methods, not for a base class -- an extension that
    implements none is still valid, even though it does not satisfy the
    Protocol."""
    assert not isinstance(MinimalExtension(), Extension)
    assert Extension not in type(FullExtension()).__mro__


# ---------- end to end ----------

def test_a_typo_in_a_real_extension_fails_the_load(tmp_path, monkeypatch):
    """Through load_system: the check has to run where instances are
    actually built, not only when called directly."""
    import phc.core.config.system as system_module

    class TypoInstance:
        def on_tik(self, devices):      # never called by anything
            pass

    monkeypatch.setattr(system_module, "_load_extensions",
                        lambda raw, flat, config_dir=None: {"fake.instance": TypoInstance()})

    config = tmp_path / "system.yaml"
    config.write_text("""
heartbeat: 1s
devices:
  - id: lamp
    module: virtual
    endpoints: [{ key: state, writable: true, default: "off" }]
""", encoding="utf-8")

    with pytest.raises(ConfigError, match="on_tik"):
        system_module.load_system(config)
