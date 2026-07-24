"""Behavior tests for opt-in relay-backed home adoption."""

import asyncio
from types import SimpleNamespace

import yaml

import pytest

from gateway.config import Platform
from gateway.config import GatewayConfig, load_gateway_config
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from gateway.run import GatewayRunner, _should_auto_adopt_home


class FrontingRelay:
    def fronts_platform(self, platform):
        return platform == Platform.SLACK


@pytest.mark.parametrize("enabled", [True, False])
def test_auto_home_reads_canonical_gateway_config(enabled):
    config = SimpleNamespace(auto_home=enabled)
    source = SimpleNamespace(
        platform=Platform.SLACK,
        chat_id="D123",
        chat_type="dm",
        delivered_via_upstream_relay=True,
        user_id="U123",
    )

    assert _should_auto_adopt_home(source, FrontingRelay(), config) is enabled


@pytest.mark.parametrize(
    "source",
    [
        SimpleNamespace(
            platform=Platform.SLACK,
            chat_id="D123",
            chat_type="dm",
            delivered_via_upstream_relay=False,
            user_id="U123",
        ),
        SimpleNamespace(
            platform=Platform.SLACK,
            chat_id="D123",
            chat_type="dm",
            delivered_via_upstream_relay=1,
            user_id="U123",
        ),
        SimpleNamespace(
            platform=Platform.SLACK,
            chat_id="D123",
            chat_type="channel",
            delivered_via_upstream_relay=True,
            user_id="U123",
        ),
        SimpleNamespace(
            platform=Platform.SLACK,
            chat_id=None,
            chat_type="dm",
            delivered_via_upstream_relay=True,
            user_id="U123",
        ),
        SimpleNamespace(
            platform=Platform.SLACK,
            chat_id="D123",
            chat_type="dm",
            delivered_via_upstream_relay=True,
            user_id=None,
        ),
    ],
)
def test_auto_home_rejects_untrusted_or_non_dm_sources(source):
    config = SimpleNamespace(auto_home=True)

    assert _should_auto_adopt_home(source, FrontingRelay(), config) is False


def test_auto_home_rejects_unadvertised_relay_platform():
    config = SimpleNamespace(auto_home=True)
    source = SimpleNamespace(
        platform=Platform.DISCORD,
        chat_id="D123",
        chat_type="dm",
        delivered_via_upstream_relay=True,
        user_id="U123",
    )

    assert _should_auto_adopt_home(source, FrontingRelay(), config) is False


def test_default_config_disables_auto_home():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["gateway"]["auto_home"] is False


@pytest.mark.parametrize("enabled", [True, False])
def test_gateway_auto_home_loads_from_config_yaml(tmp_path, enabled):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"gateway": {"auto_home": enabled}}),
        encoding="utf-8",
    )
    home_token = set_hermes_home_override(str(tmp_path))
    try:
        config = load_gateway_config()
    finally:
        reset_hermes_home_override(home_token)

    assert config.auto_home is enabled


def _runner_for_adoption(*, notice_success=True):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(auto_home=True)
    runner.adapters = {Platform.RELAY: FrontingRelay()}
    runner._adapter_for_source = lambda source: runner.adapters[Platform.RELAY]
    runner._notice_calls = []

    async def deliver_notice(source, content):
        runner._notice_calls.append((source.chat_id, content))
        return notice_success

    runner._deliver_platform_notice = deliver_notice
    return runner


def _relay_dm(chat_id="D123", user_id="U123"):
    return SimpleNamespace(
        platform=Platform.SLACK,
        chat_id=chat_id,
        chat_name="Owner DM",
        chat_type="dm",
        delivered_via_upstream_relay=True,
        user_id=user_id,
        scope_id="workspace-1",
    )


@pytest.mark.asyncio
async def test_auto_home_requires_successful_visible_notice_before_persisting(monkeypatch):
    runner = _runner_for_adoption(notice_success=False)
    persisted = []
    monkeypatch.setattr("gateway.run.persist_home_channel", persisted.append)

    adopted = await runner._maybe_auto_adopt_home(_relay_dm())

    assert adopted is False
    assert persisted == []
    assert runner.config.get_home_channel(Platform.SLACK) is None


@pytest.mark.asyncio
async def test_auto_home_notice_exception_returns_to_sethome_fallback(monkeypatch):
    runner = _runner_for_adoption()
    persisted = []
    monkeypatch.setattr("gateway.run.persist_home_channel", persisted.append)

    async def fail_notice(source, content):
        raise RuntimeError("transport down")

    runner._deliver_platform_notice = fail_notice

    adopted = await runner._maybe_auto_adopt_home(_relay_dm())

    assert adopted is False
    assert persisted == []
    assert runner.config.get_home_channel(Platform.SLACK) is None


@pytest.mark.asyncio
async def test_auto_home_persistence_failure_returns_to_sethome_fallback(monkeypatch):
    runner = _runner_for_adoption()

    def fail_persistence(home):
        raise OSError("disk full")

    monkeypatch.setattr("gateway.run.persist_home_channel", fail_persistence)

    adopted = await runner._maybe_auto_adopt_home(_relay_dm())

    assert adopted is False
    assert runner.config.get_home_channel(Platform.SLACK) is None


@pytest.mark.asyncio
async def test_auto_home_concurrent_first_dms_adopt_exactly_one(monkeypatch):
    runner = _runner_for_adoption()
    persisted = []

    def capture(home):
        persisted.append(home)

    monkeypatch.setattr("gateway.run.persist_home_channel", capture)

    results = await asyncio.gather(
        runner._maybe_auto_adopt_home(_relay_dm("D-FIRST", "U-FIRST")),
        runner._maybe_auto_adopt_home(_relay_dm("D-SECOND", "U-SECOND")),
    )

    assert results.count(True) == 1
    assert len(persisted) == 1
    home = runner.config.get_home_channel(Platform.SLACK)
    assert home is not None
    assert home.chat_id == persisted[0].chat_id
    assert home.user_id == persisted[0].user_id
    assert home.scope_id == "workspace-1"
