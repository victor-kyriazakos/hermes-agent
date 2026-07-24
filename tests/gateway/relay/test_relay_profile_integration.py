"""Consumer-side integration proof for Team Gateway per-instance profile stamping."""

from gateway.config import GatewayConfig
from gateway.relay.ws_transport import _event_from_wire
from gateway.run import GatewayRunner
from gateway.session import SessionStore


def _team_gateway_frame(*, tenant: str, instance: str, profile: str | None):
    source = {
        "platform": "slack",
        "chat_id": f"D-{tenant}",
        "chat_type": "dm",
        "user_id": f"U-{tenant}",
        "scope_id": tenant,
    }
    if profile is not None:
        source["profile"] = profile
    return {
        "text": f"for {instance}",
        "message_type": "text",
        "message_id": f"m-{instance}",
        "source": source,
    }


def test_team_gateway_profiles_select_isolated_session_and_runtime_profiles(
    tmp_path, monkeypatch
):
    """Two targeted stamps plus an unstamped control cannot share profile/tenant state."""
    default_home = tmp_path / "default"
    coder_home = tmp_path / "profiles" / "coder"
    writer_home = tmp_path / "profiles" / "writer"
    for home in (default_home, coder_home, writer_home):
        home.mkdir(parents=True)

    import hermes_cli.profiles as profiles

    homes = {"default": default_home, "coder": coder_home, "writer": writer_home}
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: homes[name])
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name in homes)

    # These are the post-fan-out frames Team Gateway puts on three independently
    # authenticated instance sockets. Each frame contains only that instance's
    # profilesByInstance selection; the omitted control remains default.
    coder = _event_from_wire(
        _team_gateway_frame(tenant="tenant-a", instance="inst-coder", profile="coder")
    )
    writer = _event_from_wire(
        _team_gateway_frame(tenant="tenant-b", instance="inst-writer", profile="writer")
    )
    control = _event_from_wire(
        _team_gateway_frame(
            tenant="tenant-control", instance="inst-default", profile=None
        )
    )

    config = GatewayConfig(multiplex_profiles=True)
    store = SessionStore(tmp_path / "sessions", config)
    runner = object.__new__(GatewayRunner)
    runner.config = config

    assert [event.source.profile for event in (coder, writer, control)] == [
        "coder",
        "writer",
        None,
    ]
    assert [
        store._generate_session_key(event.source).split(":", 2)[:2]
        for event in (coder, writer, control)
    ] == [
        ["agent", "coder"],
        ["agent", "writer"],
        ["agent", "main"],
    ]
    assert [
        runner._resolve_profile_home_for_source(event.source)
        for event in (coder, writer, control)
    ] == [
        coder_home,
        writer_home,
        default_home,
    ]

    keys = {
        store._generate_session_key(event.source) for event in (coder, writer, control)
    }
    tenants = {event.source.scope_id for event in (coder, writer, control)}
    assert len(keys) == 3
    assert tenants == {"tenant-a", "tenant-b", "tenant-control"}
    assert coder.source.profile != writer.source.profile
    assert coder.source.scope_id != writer.source.scope_id
