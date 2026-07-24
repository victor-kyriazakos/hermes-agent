"""Cold-start integration proof for relay-backed durable logical homes."""

import asyncio

import pytest

import gateway.run as gateway_run
from cron.scheduler import _deliver_result
from gateway.config import Platform, PlatformConfig, load_gateway_config
from gateway.platforms.base import MessageEvent, MessageType
from gateway.relay.adapter import RelayAdapter
from gateway.relay.descriptor import CONTRACT_VERSION, CapabilityDescriptor
from tests.gateway.relay.stub_connector import StubConnector
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _slack_descriptor() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        contract_version=CONTRACT_VERSION,
        platform="slack",
        label="Slack",
        max_message_length=4000,
        supports_draft_streaming=False,
        supports_edit=True,
        supports_threads=True,
        markdown_dialect="slack",
        len_unit="chars",
    )


@pytest.mark.asyncio
async def test_relay_sethome_survives_cold_reload_for_cron_and_lifecycle(
    tmp_path, monkeypatch
):
    """Destroy all volatile routing state, then deliver only from durable provenance."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "platforms:\n  relay:\n    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    # Legacy env persistence is orthogonal to the structured provenance under
    # test; avoid mutating the developer's real .env.
    monkeypatch.setattr("hermes_cli.config.save_env_value", lambda _key, _value: None)

    first_transport = StubConnector(_slack_descriptor())
    first_relay = RelayAdapter(
        PlatformConfig(enabled=True), _slack_descriptor(), first_transport
    )
    first_runner, _native = make_restart_runner()
    first_runner.config = load_gateway_config()
    first_runner.adapters = {Platform.RELAY: first_relay}
    first_runner._adapter_for_source = lambda _source: first_relay

    source = make_restart_source(chat_id="D123")
    source.platform = Platform.SLACK
    source.user_id = "U123"
    source.scope_id = "T123"
    source.thread_id = None  # plain Slack DM: never synthesize a thread
    source.delivered_via_upstream_relay = True
    result = await first_runner._handle_set_home_command(
        MessageEvent(
            text="/sethome",
            message_type=MessageType.TEXT,
            source=source,
            message_id="m-sethome",
        )
    )
    assert "Home channel set" in result

    # Cold boundary: throw away the runner, adapter, transport, and all per-chat
    # caches. Reload from config with no native Slack token/configuration.
    del first_runner, first_relay, first_transport
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    reloaded = load_gateway_config()
    home = reloaded.platforms[Platform.SLACK].home_channel
    assert reloaded.platforms[Platform.SLACK].enabled is False
    assert home is not None
    assert (home.chat_id, home.thread_id, home.user_id, home.scope_id) == (
        "D123",
        None,
        "U123",
        "T123",
    )

    fresh_transport = StubConnector(_slack_descriptor())
    fresh_relay = RelayAdapter(
        reloaded.platforms[Platform.RELAY], _slack_descriptor(), fresh_transport
    )
    fresh_runner, _native = make_restart_runner()
    fresh_runner.config = reloaded
    fresh_runner.adapters = {Platform.RELAY: fresh_relay}

    loop = asyncio.get_running_loop()
    cron_error = await asyncio.to_thread(
        _deliver_result,
        {
            "id": "cold-relay-cron",
            "name": "Cold relay cron",
            "deliver": "origin",
            "origin": {
                "platform": "slack",
                "chat_id": "D123",
                "chat_type": "dm",
                "user_id": "stale-user",
                "scope_id": "stale-scope",
            },
        },
        "scheduled result",
        fresh_runner.adapters,
        loop,
    )
    assert cron_error is None
    assert await fresh_runner._send_home_channel_startup_notifications() == {
        ("slack", "D123", None)
    }
    await fresh_runner._notify_active_sessions_of_shutdown()

    assert fresh_transport.sent_platforms == ["slack", "slack", "slack"]
    assert [frame["chat_id"] for frame in fresh_transport.sent] == ["D123"] * 3
    for frame in fresh_transport.sent:
        assert frame["metadata"]["user_id"] == "U123"
        assert frame["metadata"]["scope_id"] == "T123"
        assert "thread_id" not in frame["metadata"]
