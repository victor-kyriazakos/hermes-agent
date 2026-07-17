"""Unit tests for the generic-OIDC / Nous-Portal caller-identity token resolver.

Covers gateway.relay._resolve_relay_identity_token() — the canonical resolver
shared by the runtime self-provision path and the `hermes gateway enroll` CLI.

Four modes:
  1. Environment-configured OAuth2 client_credentials.
  2. Ambient workload identity from gateway.idp.token_file.
  3. Config-file OAuth2 client_credentials.
  4. Nous Portal (resolve_nous_access_token) otherwise.

The HTTP POST and Nous resolver are monkeypatched where needed; ambient file
coverage uses the real config loader against an isolated temporary Hermes home.
"""

from __future__ import annotations

import io
import json

import pytest

import gateway.relay as relay


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in (
        "GATEWAY_RELAY_IDP_TOKEN_URL",
        "GATEWAY_RELAY_IDP_CLIENT_ID",
        "GATEWAY_RELAY_IDP_CLIENT_SECRET",
        "GATEWAY_RELAY_IDP_SCOPE",
    ):
        monkeypatch.delenv(k, raising=False)
    # Never read the developer's real config.yaml.
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)


def test_defaults_to_nous_portal_when_no_idp_configured(monkeypatch):
    called = {}

    def fake_resolve():
        called["yes"] = True
        return "nous-portal-token"

    monkeypatch.setattr(
        "hermes_cli.auth.resolve_nous_access_token", fake_resolve, raising=False
    )
    assert relay._resolve_relay_identity_token() == "nous-portal-token"
    assert called == {"yes": True}


def test_client_credentials_via_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_RELAY_IDP_TOKEN_URL", "https://idp.test/token")
    monkeypatch.setenv("GATEWAY_RELAY_IDP_CLIENT_ID", "agent-client")
    monkeypatch.setenv("GATEWAY_RELAY_IDP_CLIENT_SECRET", "shh")
    monkeypatch.setenv("GATEWAY_RELAY_IDP_SCOPE", "connector.provision")

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data.decode()
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return io.BytesIO(json.dumps({"access_token": "idp-workload-token"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    token = relay._resolve_relay_identity_token()
    assert token == "idp-workload-token"
    assert captured["url"] == "https://idp.test/token"
    assert captured["method"] == "POST"
    # client_credentials grant, form-encoded, with all fields.
    assert "grant_type=client_credentials" in captured["body"]
    assert "client_id=agent-client" in captured["body"]
    assert "client_secret=shh" in captured["body"]
    assert "scope=connector.provision" in captured["body"]
    assert captured["headers"]["content-type"] == "application/x-www-form-urlencoded"


def test_client_credentials_via_config_yaml(monkeypatch):
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda **_: {
            "gateway": {
                "idp": {
                    "token_url": "https://idp.test/token",
                    "client_id": "cfg-client",
                    "client_secret": "cfg-secret",
                }
            }
        },
        raising=False,
    )

    def fake_urlopen(req, timeout=None):
        body = req.data.decode()
        assert "client_id=cfg-client" in body
        assert "client_secret=cfg-secret" in body
        # No scope configured -> not sent.
        assert "scope=" not in body
        return io.BytesIO(json.dumps({"access_token": "cfg-token"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert relay._resolve_relay_identity_token() == "cfg-token"


def test_env_token_url_takes_precedence_over_config(monkeypatch):
    monkeypatch.setenv("GATEWAY_RELAY_IDP_TOKEN_URL", "https://env.test/token")
    monkeypatch.setenv("GATEWAY_RELAY_IDP_CLIENT_ID", "env-client")
    monkeypatch.setenv("GATEWAY_RELAY_IDP_CLIENT_SECRET", "env-secret")
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda **_: {"gateway": {"idp": {"token_url": "https://cfg.test/token"}}},
        raising=False,
    )

    def fake_urlopen(req, timeout=None):
        assert req.full_url == "https://env.test/token"
        return io.BytesIO(json.dumps({"access_token": "t"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert relay._resolve_relay_identity_token() == "t"


def test_raises_when_client_creds_missing(monkeypatch):
    monkeypatch.setenv("GATEWAY_RELAY_IDP_TOKEN_URL", "https://idp.test/token")
    # No client_id / client_secret.
    with pytest.raises(RuntimeError, match="client_id/client_secret missing"):
        relay._resolve_relay_identity_token()


def test_raises_when_no_access_token_in_response(monkeypatch):
    monkeypatch.setenv("GATEWAY_RELAY_IDP_TOKEN_URL", "https://idp.test/token")
    monkeypatch.setenv("GATEWAY_RELAY_IDP_CLIENT_ID", "c")
    monkeypatch.setenv("GATEWAY_RELAY_IDP_CLIENT_SECRET", "s")

    def fake_urlopen(req, timeout=None):
        return io.BytesIO(json.dumps({"token_type": "Bearer"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="no access_token"):
        relay._resolve_relay_identity_token()


def test_ambient_token_file_from_real_config_is_reread(monkeypatch, tmp_path):
    """Platform-rotated tokens are read from config on every resolution."""
    import gateway.run as gateway_run

    token_path = tmp_path / "identity-token"
    token_path.write_text("workload-token-1\n")
    (tmp_path / "config.yaml").write_text(
        "gateway:\n"
        "  idp:\n"
        f"    token_file: {token_path}\n"
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    assert relay._resolve_relay_identity_token() == "workload-token-1"
    token_path.write_text("workload-token-2\n")
    assert relay._resolve_relay_identity_token() == "workload-token-2"


def test_ambient_token_file_wins_over_config_token_url(monkeypatch, tmp_path):
    token_path = tmp_path / "identity-token"
    token_path.write_text("ambient-token")
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda **_: {
            "gateway": {
                "idp": {
                    "token_file": str(token_path),
                    "token_url": "https://idp.test/token",
                    "client_id": "client",
                    "client_secret": "secret",
                }
            }
        },
        raising=False,
    )

    def unexpected_request(*args, **kwargs):
        raise AssertionError("client_credentials used despite ambient config")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_request)
    assert relay._resolve_relay_identity_token() == "ambient-token"


def test_env_token_url_still_overrides_ambient_config(monkeypatch, tmp_path):
    """PR #60730's environment-over-config contract remains intact."""
    token_path = tmp_path / "identity-token"
    token_path.write_text("ambient-token")
    monkeypatch.setenv("GATEWAY_RELAY_IDP_TOKEN_URL", "https://env.test/token")
    monkeypatch.setenv("GATEWAY_RELAY_IDP_CLIENT_ID", "env-client")
    monkeypatch.setenv("GATEWAY_RELAY_IDP_CLIENT_SECRET", "env-secret")
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda **_: {"gateway": {"idp": {"token_file": str(token_path)}}},
        raising=False,
    )

    def fake_urlopen(req, timeout=None):
        assert req.full_url == "https://env.test/token"
        return io.BytesIO(json.dumps({"access_token": "env-token"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert relay._resolve_relay_identity_token() == "env-token"


@pytest.mark.parametrize("contents", ["", "   \n"])
def test_ambient_token_file_rejects_empty_content(monkeypatch, tmp_path, contents):
    token_path = tmp_path / "identity-token"
    token_path.write_text(contents)
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda **_: {"gateway": {"idp": {"token_file": str(token_path)}}},
        raising=False,
    )
    with pytest.raises(RuntimeError, match="is empty"):
        relay._resolve_relay_identity_token()


def test_ambient_token_file_rejects_missing_path(monkeypatch, tmp_path):
    missing = tmp_path / "missing-token"
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda **_: {"gateway": {"idp": {"token_file": str(missing)}}},
        raising=False,
    )
    with pytest.raises(RuntimeError, match="token_file.*could not be read"):
        relay._resolve_relay_identity_token()


def test_ambient_token_file_accepts_maximum_size(monkeypatch, tmp_path):
    token_path = tmp_path / "identity-token"
    token_path.write_text("x" * (64 * 1024))
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda **_: {"gateway": {"idp": {"token_file": str(token_path)}}},
        raising=False,
    )
    assert len(relay._resolve_relay_identity_token()) == 64 * 1024


def test_ambient_token_file_rejects_oversized_content(monkeypatch, tmp_path):
    token_path = tmp_path / "identity-token"
    token_path.write_text("x" * (64 * 1024 + 1))
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda **_: {"gateway": {"idp": {"token_file": str(token_path)}}},
        raising=False,
    )
    with pytest.raises(RuntimeError, match="exceeds 65536 bytes"):
        relay._resolve_relay_identity_token()


def test_ambient_token_rejects_invalid_utf8(monkeypatch, tmp_path):
    token_path = tmp_path / "identity-token"
    token_path.write_bytes(b"\xff")
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda **_: {"gateway": {"idp": {"token_file": str(token_path)}}},
        raising=False,
    )
    with pytest.raises(RuntimeError, match="valid UTF-8"):
        relay._resolve_relay_identity_token()


def test_ambient_token_rejects_multiple_lines(monkeypatch, tmp_path):
    token_path = tmp_path / "identity-token"
    token_path.write_text("token\ndiagnostic")
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda **_: {"gateway": {"idp": {"token_file": str(token_path)}}},
        raising=False,
    )
    with pytest.raises(RuntimeError, match="exactly one line"):
        relay._resolve_relay_identity_token()


def test_ambient_token_rejects_invalid_yaml(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text("gateway: [")
    with pytest.raises(RuntimeError, match="Could not parse gateway config"):
        relay._resolve_relay_identity_token()


def test_gateway_config_loader_remains_tolerant_by_default(tmp_path):
    import gateway.run as gateway_run

    (tmp_path / "config.yaml").write_text("gateway: [")
    assert gateway_run._load_gateway_config() == {}


def test_gateway_config_loader_preserves_raw_config_when_overlay_fails(
    monkeypatch, tmp_path
):
    import gateway.run as gateway_run
    from hermes_cli import managed_scope

    (tmp_path / "config.yaml").write_text("gateway:\n  relay_url: wss://relay.test\n")

    def fail_overlay(_raw):
        raise RuntimeError("overlay unavailable")

    monkeypatch.setattr(managed_scope, "apply_managed_overlay", fail_overlay)
    assert gateway_run._load_gateway_config() == {
        "gateway": {"relay_url": "wss://relay.test"}
    }


def test_ambient_token_rejects_scalar_yaml_root(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text("false\n")
    with pytest.raises(RuntimeError, match="gateway config must be a mapping"):
        relay._resolve_relay_identity_token()


def test_ambient_token_rejects_malformed_root_config(monkeypatch):
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda **_: ["not", "a", "mapping"],
        raising=False,
    )
    with pytest.raises(RuntimeError, match="gateway config must be a mapping"):
        relay._resolve_relay_identity_token()


@pytest.mark.parametrize("contents", ["\ntoken\n", "token\n\n", "token\r\n\r\n"])
def test_ambient_token_rejects_extra_line_endings(
    monkeypatch, tmp_path, contents
):
    token_path = tmp_path / "identity-token"
    token_path.write_text(contents)
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda **_: {"gateway": {"idp": {"token_file": str(token_path)}}},
        raising=False,
    )
    with pytest.raises(RuntimeError, match="exactly one line"):
        relay._resolve_relay_identity_token()


def test_ambient_token_rejects_malformed_gateway_config(monkeypatch):
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda **_: {"gateway": []},
        raising=False,
    )
    with pytest.raises(RuntimeError, match="gateway must be a mapping"):
        relay._resolve_relay_identity_token()


def test_ambient_token_rejects_malformed_idp_config(monkeypatch):
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda **_: {"gateway": {"idp": []}},
        raising=False,
    )
    with pytest.raises(RuntimeError, match="gateway.idp must be a mapping"):
        relay._resolve_relay_identity_token()
