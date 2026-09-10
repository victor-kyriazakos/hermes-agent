"""Exercise configured Hermes ownership with the real Relay binding."""
from __future__ import annotations

import asyncio

import pytest

from agent import relay_runtime


@pytest.fixture
def native_plugins(tmp_path, monkeypatch):
    # Isolate native user-file discovery as well as Hermes profile state.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv(relay_runtime.RELAY_PLUGINS_CONFIG_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    import nemo_relay as relay

    relay_runtime._reset_for_tests()
    seen = []

    class ProbePlugin:
        def validate(self, plugin_config):
            return []

        def register(self, plugin_config, context):
            seen.append(plugin_config["source"])
            context.register_tool_request_intercept(
                "activation-probe", 0, False,
                lambda name, args: {**args, "activation_source": plugin_config["source"]},
            )

    kind = "hermes_owned_activation_test"
    relay.plugin.register(kind, ProbePlugin())
    selected = tmp_path / "selected.toml"
    selected.write_text(
        f'[[components]]\nkind = "{kind}"\n[components.config]\nsource = "selected"\n',
        encoding="utf-8",
    )
    # A conflicting user layer proves Hermes passes the explicitly selected file.
    user_config = tmp_path / "config" / "nemo-relay" / "plugins.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text("invalid = [", encoding="utf-8")
    try:
        yield relay, seen, selected, kind
    finally:
        relay_runtime._reset_for_tests()
        relay.plugin.deregister(kind)


@pytest.mark.parametrize("configured", [False, True])
def test_native_configuration_retained_until_final_owner(native_plugins, monkeypatch, configured):
    relay, seen, selected, _ = native_plugins
    if configured:
        monkeypatch.setenv(relay_runtime.RELAY_PLUGINS_CONFIG_ENV, str(selected))
    a = relay_runtime.RelayRuntime(relay=relay, profile_key="a")
    b = relay_runtime.RelayRuntime(relay=relay, profile_key="b")
    activation = relay_runtime._PLUGIN_CONFIGURATION._activation
    try:
        assert a.managed_execution_enabled() is configured
        assert b.managed_execution_enabled() is configured
        assert a._plugin_configuration_state.name == ("ACTIVE" if configured else "DISABLED")
        assert seen == (["selected"] if configured else [])
        if configured:
            assert isinstance(activation, relay.plugin.PluginHostActivation)
            assert activation.is_active
            assert relay.tools.request_intercepts("probe", {}) == {"activation_source": "selected"}
        a.shutdown()
        if configured:
            assert activation.is_active
            assert relay.tools.request_intercepts("probe", {}) == {"activation_source": "selected"}
    finally:
        a.shutdown()
        b.shutdown()
    if configured:
        assert not activation.is_active
    assert relay.tools.request_intercepts("probe", {}) == {}


def test_native_foreign_owner_is_not_replaced_or_closed(native_plugins, monkeypatch):
    relay, seen, selected, kind = native_plugins
    foreign_config = selected.with_name("foreign.toml")
    foreign_config.write_text(
        f'[[components]]\nkind = "{kind}"\n[components.config]\nsource = "foreign"\n',
        encoding="utf-8",
    )
    foreign = asyncio.run(relay.plugin.initialize({}, foreign_config))
    monkeypatch.setenv(relay_runtime.RELAY_PLUGINS_CONFIG_ENV, str(selected))
    host = None
    try:
        host = relay_runtime.RelayRuntime(relay=relay, profile_key="a")
        assert host._plugin_configuration_state.name == "FOREIGN"
        assert not host.managed_execution_enabled()
        assert seen == ["foreign"]
        host.shutdown()
        assert foreign.is_active
        assert relay.tools.request_intercepts("probe", {}) == {"activation_source": "foreign"}
    finally:
        if host is not None:
            host.shutdown()
        asyncio.run(foreign.close())
