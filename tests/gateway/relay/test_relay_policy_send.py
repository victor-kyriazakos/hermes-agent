"""Unit tests for the gateway-side relay relevance-policy declaration (Phase 6 ζ).

Covers gateway.relay.relay_relevance_policy() (the projection of the agent's
mention-gating / free-response / allow-bots config into the connector's generic
vocabulary) and send_relay_policy() (the boot-time POST to /relay/policy). The
connector HTTP POST is monkeypatched; the cross-repo E2E (connector repo,
gateway_policy_driver.py) exercises the real route. These prove the PROJECTION
mapping, the auth/skip logic, and the fail-soft boot behaviour.
"""

from __future__ import annotations

import pytest

import gateway.relay as relay


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        "GATEWAY_RELAY_URL",
        "GATEWAY_RELAY_ID",
        "GATEWAY_RELAY_SECRET",
        "GATEWAY_RELAY_PLATFORM",
        "GATEWAY_RELAY_BOT_ID",
        "GATEWAY_RELAY_PLATFORMS",
        "GATEWAY_RELAY_BOT_IDS",
        "DISCORD_ALLOW_BOTS",
        "SLACK_ALLOW_BOTS",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {}, raising=False)


# --------------------------------------------------------------------------
# relay_relevance_policy() — the projection
# --------------------------------------------------------------------------

def test_projection_maps_require_mention_and_free_response(monkeypatch):
    monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "discord")
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"discord": {"require_mention": True, "free_response_channels": ["c-support", "c-help"]}},
        raising=False,
    )
    pol = relay.relay_relevance_policy()
    assert pol == {
        "platform": "discord",
        "requireAddress": True,
        "freeResponseScopes": ["c-support", "c-help"],
        "allowOtherBots": False,
    }


def test_projection_declares_explicit_require_mention_false(monkeypatch):
    # An EXPLICIT `require_mention: false` is a configured (non-default) choice
    # and MUST be declared: the connector's absent-row default is now
    # requireAddress=true, so staying silent would mention-gate an agent the
    # operator configured to free-respond.
    monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "discord")
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"discord": {"require_mention": False}},
        raising=False,
    )
    pol = relay.relay_relevance_policy()
    assert pol == {
        "platform": "discord",
        "requireAddress": False,
        "freeResponseScopes": [],
        "allowOtherBots": False,
    }


# --------------------------------------------------------------------------
# Salt B9: enabling bot authors must never relax mention gating; unset
# require_mention resolves to requireAddress=TRUE (the connector maps a
# missing/false requireAddress to "admit unaddressed traffic").
# --------------------------------------------------------------------------

def test_allow_bots_mentions_alone_keeps_require_address_true(monkeypatch):
    monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "slack")
    monkeypatch.setenv("SLACK_ALLOW_BOTS", "mentions")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {}, raising=False)
    pol = relay.relay_relevance_policy()
    assert pol == {
        "platform": "slack",
        "requireAddress": True,
        "freeResponseScopes": [],
        "allowOtherBots": True,
    }


def test_allow_bots_all_with_explicit_require_mention_false(monkeypatch):
    # An explicit opt-out is the ONLY thing that turns the address requirement off.
    monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "slack")
    monkeypatch.setenv("SLACK_ALLOW_BOTS", "all")
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"platforms": {"relay": {"extra": {"slack": {"require_mention": False}}}}},
        raising=False,
    )
    pol = relay.relay_relevance_policy()
    assert pol["requireAddress"] is False
    assert pol["allowOtherBots"] is True


def test_relay_extra_slack_require_mention_is_canonical_source(monkeypatch):
    # platforms.relay.extra.slack.* (the knob source the relay adapter reads) wins
    # over the native ``slack:`` block; a YAML-quoted "false" is coerced like the adapter does.
    monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "slack")
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {
            "slack": {"require_mention": True},
            "platforms": {"relay": {"extra": {"slack": {"require_mention": "false"}}}},
        },
        raising=False,
    )
    assert relay.relay_relevance_policy()["requireAddress"] is False


def test_unset_require_mention_resolves_true(monkeypatch):
    monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "slack")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {}, raising=False)
    pol = relay.relay_relevance_policy()
    assert pol is not None
    assert pol["requireAddress"] is True
    assert pol["allowOtherBots"] is False


def test_send_publishes_explicit_reset_when_config_removed(monkeypatch):
    # No relevance config at all -> still POST a default policy so a previously
    # permissive connector row is replaced, not left standing.
    monkeypatch.setenv("GATEWAY_RELAY_URL", "wss://connector.example/relay")
    monkeypatch.setenv("GATEWAY_RELAY_ID", "gw-x")
    monkeypatch.setenv("GATEWAY_RELAY_SECRET", "s" * 48)
    monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "slack")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {}, raising=False)
    captured = []

    def _fake_post(*, policy_url, token, policy, timeout=15.0):
        captured.append(policy)
        return 200

    monkeypatch.setattr(relay, "_post_policy", _fake_post)
    assert relay.send_relay_policy() is True
    assert captured == [
        {"platform": "slack", "requireAddress": True, "freeResponseScopes": [], "allowOtherBots": False}
    ]


# --------------------------------------------------------------------------
# send_relay_policy() — the boot-time declaration
# --------------------------------------------------------------------------

def _arm(monkeypatch, *, url="wss://connector.example/relay"):
    monkeypatch.setenv("GATEWAY_RELAY_URL", url)
    monkeypatch.setenv("GATEWAY_RELAY_ID", "gw-x")
    monkeypatch.setenv("GATEWAY_RELAY_SECRET", "s" * 48)
    monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "discord")


def test_send_posts_projected_policy_with_token(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"discord": {"require_mention": True, "free_response_channels": ["c-support"]}},
        raising=False,
    )
    captured = {}

    def _fake_post(*, policy_url, token, policy, timeout=15.0):
        captured["policy_url"] = policy_url
        captured["token"] = token
        captured["policy"] = policy
        return 200

    monkeypatch.setattr(relay, "_post_policy", _fake_post)
    assert relay.send_relay_policy() is True
    assert captured["policy_url"] == "https://connector.example/relay/policy"
    assert captured["token"]  # a real upgrade token was minted
    assert captured["policy"]["requireAddress"] is True
    assert captured["policy"]["freeResponseScopes"] == ["c-support"]


def test_send_skips_when_no_secret(monkeypatch):
    monkeypatch.setenv("GATEWAY_RELAY_URL", "wss://connector.example/relay")
    monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "discord")
    # no GATEWAY_RELAY_ID / SECRET
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"discord": {"require_mention": True}},
        raising=False,
    )
    called = {"n": 0}
    monkeypatch.setattr(relay, "_post_policy", lambda **k: called.__setitem__("n", called["n"] + 1) or 200)
    assert relay.send_relay_policy() is False
    assert called["n"] == 0  # never attempted without a secret to auth with


def test_send_fail_soft_on_transport_error(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"discord": {"require_mention": True}},
        raising=False,
    )

    def _boom(**kwargs):
        raise RuntimeError("connector unreachable")

    monkeypatch.setattr(relay, "_post_policy", _boom)
    # Never raises; returns False so boot proceeds.
    assert relay.send_relay_policy() is False


