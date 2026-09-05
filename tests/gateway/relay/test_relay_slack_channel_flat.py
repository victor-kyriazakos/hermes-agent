"""Channel flat mode (gg-peer-addressing P4).

``platforms.relay.extra.slack.channel_reply_in_thread`` decides where a CHANNEL
reply lands. Default True keeps native parity (channel finals thread under the
triggering message). False posts channel replies at the channel root, the same
shape ``reply_in_thread=False`` already gives DMs. The two knobs are
independent: an operator can have flat DMs and threaded channels or the reverse.

Live 2026-09-05: with ``reply_in_thread=false`` the progress bubble posted at the
channel root (run.py honours the flag everywhere) while the final threaded
(``_resolve_reply_to_for_send`` only matched ``chat_type == "dm"``): mixed shape.
"""

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SessionSource
from gateway.relay.adapter import RelayAdapter
from tests.gateway.relay.test_relay_slack_prompt_dm_root import StubConnector, _slack_desc


def _wire(chat_id: str, chat_type: str, scope_id="T1"):
    stub = StubConnector(_slack_desc())
    adapter = RelayAdapter(PlatformConfig(), _slack_desc(), transport=stub)
    src = SessionSource(
        platform=Platform.SLACK, chat_id=chat_id, chat_type=chat_type, user_id="U1", scope_id=scope_id,
    )
    adapter._capture_scope(MessageEvent(text="hi", source=src, message_type=MessageType.TEXT))
    return adapter


def test_channel_default_threads_the_final():
    adapter = _wire("C1", "channel")
    adapter.config.extra = {"slack": {"reply_in_thread": False}}
    # No channel knob: native parity, the triggering ts anchors the reply.
    assert adapter._resolve_reply_to_for_send("C1", "1700.001", {}) == "1700.001"
    assert adapter._effective_channel_reply_in_thread() is True


def test_channel_flat_drops_the_synthetic_anchor():
    adapter = _wire("C1", "channel")
    adapter.config.extra = {"slack": {"reply_in_thread": False, "channel_reply_in_thread": False}}
    assert adapter._resolve_reply_to_for_send("C1", "1700.001", {}) is None
    md = {"reply_to_message_id": "1700.001"}
    assert adapter._apply_slack_thread_anchor("C1", "1700.001", md) is None
    assert "thread_id" not in md and "reply_to_message_id" not in md


def test_channel_flat_keeps_a_real_thread():
    adapter = _wire("C1", "channel")
    adapter.config.extra = {"slack": {"channel_reply_in_thread": False}}
    # A message that arrived INSIDE a thread carries thread_id: stays threaded.
    assert adapter._resolve_reply_to_for_send("C1", "1700.002", {"thread_id": "1700.001"}) == "1700.002"


def test_channel_flat_is_independent_of_dm_mode():
    adapter = _wire("C1", "channel")
    adapter.config.extra = {"slack": {"reply_in_thread": True, "channel_reply_in_thread": False}}
    assert adapter._resolve_reply_to_for_send("C1", "1700.001", {}) is None
    dm = _wire("D1", "dm")
    dm.config.extra = {"slack": {"reply_in_thread": True, "channel_reply_in_thread": False}}
    assert dm._resolve_reply_to_for_send("D1", "1700.001", {}) == "1700.001"


def test_group_counts_as_channel():
    adapter = _wire("G1", "group")
    adapter.config.extra = {"slack": {"channel_reply_in_thread": False}}
    assert adapter._resolve_reply_to_for_send("G1", "1700.001", {}) is None


def test_progress_lane_follows_channel_knob():
    """run.py resolves the progress anchor from the adapter's effective mode for the
    event's chat type, so progress and final agree (no mixed shape)."""
    adapter = _wire("C1", "channel")
    adapter.config.extra = {"slack": {"reply_in_thread": True, "channel_reply_in_thread": False}}
    assert adapter._effective_reply_in_thread_for_chat("C1") is False
    adapter.config.extra = {"slack": {"reply_in_thread": False, "channel_reply_in_thread": True}}
    assert adapter._effective_reply_in_thread_for_chat("C1") is True
    dm = _wire("D1", "dm")
    dm.config.extra = {"slack": {"reply_in_thread": False, "channel_reply_in_thread": True}}
    assert dm._effective_reply_in_thread_for_chat("D1") is False


def test_run_turn_progress_lane_uses_per_chat_mode():
    """The progress lane reads the adapter's per-chat mode, so a channel with
    channel_reply_in_thread=False gets a root-level progress anchor (None) even when
    the DM knob is True."""
    import inspect
    from gateway import run_turn

    src = inspect.getsource(run_turn.GatewayTurnMixin._run_agent_progress_threading)
    assert "_effective_reply_in_thread_for_chat" in src
