"""Security and admission tests for relay-backed automatic home adoption."""

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run import _should_auto_adopt_home


class FrontingRelay:
    def fronts_platform(self, platform):
        return platform == Platform.SLACK


@pytest.mark.parametrize("enabled", ["1", "true", "TRUE", "yes"])
def test_auto_home_requires_trusted_relay_dm(monkeypatch, enabled):
    monkeypatch.setenv("GATEWAY_AUTO_HOME", enabled)
    source = SimpleNamespace(
        platform=Platform.SLACK,
        chat_id="D123",
        chat_type="dm",
        delivered_via_upstream_relay=True,
        user_id="U123",
    )

    assert _should_auto_adopt_home(source, FrontingRelay()) is True


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
def test_auto_home_rejects_untrusted_or_non_dm_sources(monkeypatch, source):
    monkeypatch.setenv("GATEWAY_AUTO_HOME", "true")

    assert _should_auto_adopt_home(source, FrontingRelay()) is False


def test_auto_home_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("GATEWAY_AUTO_HOME", raising=False)
    source = SimpleNamespace(
        platform=Platform.SLACK,
        chat_id="D123",
        chat_type="dm",
        delivered_via_upstream_relay=True,
        user_id="U123",
    )

    assert _should_auto_adopt_home(source, FrontingRelay()) is False


def test_auto_home_rejects_unadvertised_relay_platform(monkeypatch):
    monkeypatch.setenv("GATEWAY_AUTO_HOME", "true")
    source = SimpleNamespace(
        platform=Platform.DISCORD,
        chat_id="D123",
        chat_type="dm",
        delivered_via_upstream_relay=True,
        user_id="U123",
    )

    assert _should_auto_adopt_home(source, FrontingRelay()) is False
