import json
from contextlib import contextmanager


def test_status_returns_in_process_sync_state(monkeypatch):
    from tools import skill_sync_tool

    expected = {
        "feature_enabled": True,
        "access_allowed": True,
        "opted_in_skills": ["my-skill"],
    }
    monkeypatch.setattr(skill_sync_tool.ssc, "sync_status", lambda: expected)

    result = json.loads(skill_sync_tool.skill_sync_tool(action="status"))

    assert result == {"success": True, "action": "status", "status": expected}


def test_now_pulls_then_pushes_with_resolved_identity(monkeypatch):
    from tools import skill_sync_tool

    identity = {"owner": "owner-1", "access_allowed": True}
    calls = []
    monkeypatch.setattr(skill_sync_tool.ssc, "resolve_identity", lambda: identity)
    monkeypatch.setattr(skill_sync_tool.ssc, "sync_feature_enabled", lambda: True)
    monkeypatch.setattr(
        skill_sync_tool.ssc,
        "resolve_sync_base_url",
        lambda: "https://sync.example.test",
    )
    monkeypatch.setattr(
        skill_sync_tool.ssc,
        "pull_skills",
        lambda *, identity: calls.append(("pull", identity)) or {"updated": ["a"]},
    )
    monkeypatch.setattr(
        skill_sync_tool.ssc,
        "push_skills",
        lambda *, identity, message: calls.append(("push", identity, message))
        or {"head": "abc"},
    )

    result = json.loads(skill_sync_tool.skill_sync_tool(action="now"))

    assert calls == [
        ("pull", identity),
        ("push", identity, "hermes skill_sync now"),
    ]
    assert result == {
        "success": True,
        "action": "now",
        "pull": {"updated": ["a"]},
        "push": {"head": "abc"},
    }


def test_push_runs_synchronously_with_resolved_identity(monkeypatch):
    from tools import skill_sync_tool

    identity = {"owner": "owner-1", "access_allowed": True}
    monkeypatch.setattr(skill_sync_tool, "_ready_identity", lambda: identity)
    monkeypatch.setattr(
        skill_sync_tool.ssc,
        "push_skills",
        lambda *, identity, message: {"owner": identity["owner"], "message": message},
    )

    result = json.loads(skill_sync_tool.skill_sync_tool(action="push"))

    assert result == {
        "success": True,
        "action": "push",
        "result": {"owner": "owner-1", "message": "hermes skill_sync push"},
    }


def test_pull_runs_synchronously_with_resolved_identity(monkeypatch):
    from tools import skill_sync_tool

    identity = {"owner": "owner-1", "access_allowed": True}
    monkeypatch.setattr(skill_sync_tool, "_ready_identity", lambda: identity)
    monkeypatch.setattr(
        skill_sync_tool.ssc,
        "pull_skills",
        lambda *, identity: {"owner": identity["owner"], "updated": ["my-skill"]},
    )

    result = json.loads(skill_sync_tool.skill_sync_tool(action="pull"))

    assert result == {
        "success": True,
        "action": "pull",
        "result": {"owner": "owner-1", "updated": ["my-skill"]},
    }


def test_push_denies_identity_without_sync_authority(monkeypatch):
    from tools import skill_sync_tool

    monkeypatch.setattr(
        skill_sync_tool.ssc,
        "resolve_identity",
        lambda: {"owner": "owner-2", "access_allowed": False},
    )
    monkeypatch.setattr(skill_sync_tool.ssc, "sync_feature_enabled", lambda: True)
    monkeypatch.setattr(
        skill_sync_tool.ssc,
        "resolve_sync_base_url",
        lambda: "https://sync.example.test",
    )
    called = False

    def unexpected_push(**_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(skill_sync_tool.ssc, "push_skills", unexpected_push)

    result = json.loads(skill_sync_tool.skill_sync_tool(action="push"))

    assert called is False
    assert result == {"error": "Skill sync failed: sync is not enabled for this identity"}


def test_tool_is_available_to_default_agents_and_scheduled_runs():
    from tools.skill_sync_tool import SKILL_SYNC_SCHEMA
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS

    assert SKILL_SYNC_SCHEMA["parameters"]["properties"]["action"]["enum"] == [
        "status",
        "pull",
        "push",
        "now",
    ]
    assert "skill_sync" in _HERMES_CORE_TOOLS
    assert "skill_sync" in TOOLSETS["skills"]["tools"]


def test_push_rejects_non_https_sync_endpoint_before_sending_credentials(monkeypatch):
    from tools import skill_sync_tool

    monkeypatch.setattr(
        skill_sync_tool.ssc,
        "resolve_identity",
        lambda: {"owner": "owner-1", "access_allowed": True, "api_key": "secret"},
    )
    monkeypatch.setattr(skill_sync_tool.ssc, "sync_feature_enabled", lambda: True)
    monkeypatch.setattr(
        skill_sync_tool.ssc,
        "resolve_sync_base_url",
        lambda: "http://sync.example.test",
    )
    called = False

    def unexpected_push(**_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(skill_sync_tool.ssc, "push_skills", unexpected_push)

    result = json.loads(skill_sync_tool.skill_sync_tool(action="push"))

    assert called is False
    assert result == {"error": "Skill sync failed: sync base URL must use HTTPS"}


def test_push_restricts_sync_endpoint_to_managed_relay_host(monkeypatch):
    from tools import skill_sync_tool

    monkeypatch.setenv("GATEWAY_RELAY_URL", "wss://relay.example.test/relay")
    monkeypatch.setattr(skill_sync_tool.ssc, "sync_feature_enabled", lambda: True)
    monkeypatch.setattr(
        skill_sync_tool.ssc,
        "resolve_sync_base_url",
        lambda: "https://attacker.example.test",
    )
    monkeypatch.setattr(
        skill_sync_tool.ssc,
        "resolve_identity",
        lambda: (_ for _ in ()).throw(AssertionError("identity must not be resolved")),
    )

    result = json.loads(skill_sync_tool.skill_sync_tool(action="push"))

    assert result == {
        "error": "Skill sync failed: sync base URL host must match the configured relay host"
    }


def test_push_surfaces_unsuccessful_sync_result(monkeypatch):
    from tools import skill_sync_tool

    monkeypatch.setattr(
        skill_sync_tool,
        "_ready_identity",
        lambda: {"owner": "owner-1", "access_allowed": True},
    )
    monkeypatch.setattr(
        skill_sync_tool.ssc,
        "push_skills",
        lambda **_kwargs: {"ok": False, "conflict": True},
    )

    result = json.loads(skill_sync_tool.skill_sync_tool(action="push"))

    assert result == {
        "success": False,
        "action": "push",
        "result": {"ok": False, "conflict": True},
    }


def test_now_holds_one_sync_lock_across_pull_and_push(monkeypatch):
    from tools import skill_sync_tool

    events = []

    @contextmanager
    def recording_operation():
        events.append("enter")
        yield
        events.append("exit")

    def pull_skills(*, identity):
        assert events == ["enter"]
        return {"ok": True, "owner": identity["owner"]}

    def push_skills(*, identity, message):
        assert events == ["enter"]
        return {"ok": True, "owner": identity["owner"], "message": message}

    monkeypatch.setattr(
        skill_sync_tool,
        "_ready_identity",
        lambda: {"owner": "owner-1", "access_allowed": True},
    )
    monkeypatch.setattr(skill_sync_tool.ssc, "sync_operation", recording_operation)
    monkeypatch.setattr(skill_sync_tool.ssc, "pull_skills", pull_skills)
    monkeypatch.setattr(skill_sync_tool.ssc, "push_skills", push_skills)

    result = json.loads(skill_sync_tool.skill_sync_tool(action="now"))

    assert result["success"] is True
    assert events == ["enter", "exit"]
