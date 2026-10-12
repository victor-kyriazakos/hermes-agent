"""In a shared conversation the unexpected-silence notice reaches the sender alone.

When the gateway rejects a bare silence marker on a human turn it still owes the sender a notice
(#110952). Posted publicly into a channel or thread it breaks a stand-down the whole room asked
for, so outside a DM the notice is a ``SenderOnlyNotice`` and the send path delivers it with the
adapter's ``send_private_notice``. Adapters without a private surface, and a private send that
fails, fall back to the normal public send so the notice is never lost. Covered on all three paths
that reject a silence: the live turn, the queued follow-up and the crash-left redelivery.
"""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.i18n import t
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tests.gateway.test_active_turn_recovery import _close_store_db, _db_runner
from tests.gateway.test_gateway_silence_tokens import _runner

KEY = "agent:main:slack:group:C1:T1"
SENDER = "U_SENDER"


def _notice() -> str:
    return t("gateway.errors.unexpected_silence")


class _PublicOnlyAdapter(BasePlatformAdapter):
    """Records sends; keeps the base ``send_private_notice`` (no private surface)."""

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="t"), Platform.SLACK)
        self.public: list[tuple] = []
        self.private: list[tuple] = []

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

    async def send(self, chat_id, content="", reply_to=None, metadata=None, **_kw):
        self.public.append((chat_id, str(content), dict(metadata or {})))
        return SendResult(success=True, message_id="public-1")


class _PrivateAdapter(_PublicOnlyAdapter):
    """Has a private surface; ``outcome`` is its SendResult, or an exception it raises."""

    def __init__(self, outcome=None):
        super().__init__()
        self.outcome = outcome or SendResult(success=True, message_id="private-1")

    async def send_private_notice(self, chat_id, user_id, content, reply_to=None, metadata=None):
        self.private.append((chat_id, user_id, str(content), dict(metadata or {})))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _source(chat_type="group", user_id=SENDER) -> SessionSource:
    return SessionSource(platform=Platform.SLACK, chat_id="C1", chat_type=chat_type,
                         user_id=user_id, thread_id="T1" if chat_type != "dm" else None)


def _silent_result(**extra):
    return {"final_response": "[SILENT]", "tools": [], "history_offset": 0, "last_prompt_tokens": 0,
            "api_calls": 1, "failed": False,
            "messages": [{"role": "user", "content": "ok sounds good"},
                         {"role": "assistant", "content": "[SILENT]"}], **extra}


async def _live_turn(monkeypatch, tmp_path, adapter, *, chat_type="group", reply_expected=None, **result):
    """One real turn: the runner's handler under the adapter's real delivery path."""
    runner = _runner(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(return_value=_silent_result(**result))
    event = MessageEvent(text="ok sounds good", source=_source(chat_type), message_id="m-1",
                         reply_expected=reply_expected)
    adapter.set_message_handler(lambda ev: runner._handle_message_with_agent(ev, ev.source, KEY, 1))
    with patch.object(adapter, "_keep_typing", new=AsyncMock()):
        await adapter._process_message_background(event, KEY)


@pytest.mark.asyncio
async def test_shared_thread_notice_goes_privately_to_the_sender(monkeypatch, tmp_path, caplog):
    adapter = _PrivateAdapter()
    with caplog.at_level("INFO"):
        await _live_turn(monkeypatch, tmp_path, adapter)
    assert adapter.private == [("C1", SENDER, _notice(), adapter.private[0][3])]
    assert adapter.private[0][3].get("thread_id") == "T1"
    assert adapter.public == []
    # The rejection stays a WARNING; the private routing is one INFO line naming the chat.
    assert any(r.levelname == "WARNING" and "silence marker rejected on a user turn" in r.message
               for r in caplog.records)
    assert any(r.levelname == "INFO" and "delivered privately" in r.message and "chat=C1" in r.message
               for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("make_adapter, chat_type, tries_private", [
    (_PrivateAdapter, "dm", False),
    (_PublicOnlyAdapter, "group", False),
    (lambda: _PrivateAdapter(SendResult(success=False, error="user_not_in_channel")), "group", True),
    (lambda: _PrivateAdapter(RuntimeError("socket closed")), "group", True),
], ids=["dm-unchanged", "adapter-without-private-surface", "private-send-fails", "private-send-raises"])
async def test_notice_falls_back_to_the_public_send(monkeypatch, tmp_path, make_adapter, chat_type, tries_private):
    adapter = make_adapter()
    await _live_turn(monkeypatch, tmp_path, adapter, chat_type=chat_type)
    assert len(adapter.private) == (1 if tries_private else 0)
    assert [content for _chat, content, _meta in adapter.public] == [_notice()]


@pytest.mark.asyncio
async def test_unaddressed_turn_still_sends_nothing(monkeypatch, tmp_path):
    adapter = _PrivateAdapter()
    await _live_turn(monkeypatch, tmp_path, adapter, reply_expected=False)
    assert (adapter.private, adapter.public) == ([], [])


@pytest.mark.asyncio
async def test_queued_chain_notice_goes_to_the_terminal_turns_sender(monkeypatch, tmp_path):
    """A queued chain's LAST message owns the silence verdict, so its sender gets the notice."""
    adapter = _PrivateAdapter()
    await _live_turn(monkeypatch, tmp_path, adapter, queued_terminal_user_id="U_LATER",
                     queued_terminal_reply_expected=None)
    assert [(user, content) for _chat, user, content, _meta in adapter.private] == [("U_LATER", _notice())]
    assert adapter.public == []


def _queued_turn_ctx(chat_type):
    return SimpleNamespace(
        session_key=KEY, stream_consumer_holder=[None], mute_notification_reply=False,
        persist_user_display_kind=None, reply_expected=None, source=_source(chat_type),
        _status_thread_metadata={"thread_id": "T1"} if chat_type != "dm" else None,
        event_message_id=None, inbound_message_id="m-1", run_generation=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type, private, public", [("group", 1, 0), ("dm", 0, 1)],
                         ids=["shared", "dm-unchanged"])
async def test_queued_follow_up_routes_the_notice_the_same_way(chat_type, private, public):
    runner = GatewayRunner(GatewayConfig())
    adapter = _PrivateAdapter()
    result = {"final_response": "NO_REPLY", "failed": False}

    await runner._run_agent_deliver_first_response(_queued_turn_ctx(chat_type), adapter, result, result, None)

    assert [(user, content) for _chat, user, content, _meta in adapter.private] == [(SENDER, _notice())] * private
    assert [content for _chat, content, _meta in adapter.public] == [_notice()] * public
    if private:
        assert adapter.private[0][3].get("thread_id") == "T1"


def _crash_left_silence(store, chat_type):
    source = SessionSource(platform=Platform.DISCORD, chat_id=f"crash-{chat_type}", chat_type=chat_type,
                           user_id="user-1", thread_id="thread-1" if chat_type != "dm" else None)
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)
    store.append_to_transcript(entry.session_id, {"role": "user", "content": "ok sounds good"})
    store.append_to_transcript(entry.session_id, {"role": "assistant", "content": "NO_REPLY"})


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type, private, public", [("channel", 1, 0), ("dm", 0, 1)],
                         ids=["shared", "dm-unchanged"])
async def test_crash_left_notice_is_redelivered_privately_in_a_shared_chat(tmp_path, chat_type, private, public):
    """A process killed after persisting the marker owes the same notice live delivery would
    have sent; the ledger row remembers its recipient so the boot sweep keeps it private."""
    from gateway.delivery_ledger import sweep_recoverable

    runner, store = _db_runner(tmp_path)
    _crash_left_silence(store, chat_type)
    assert await runner._recover_unclean_sessions() == (0, 1)
    rows = sweep_recoverable(now=time.time(), deliverable_platforms={"discord"})
    assert [row.get("private_recipient") for row in rows] == (["user-1"] if private else [None])

    adapter = _PrivateAdapter()
    runner._obligation_adapter = AsyncMock(return_value=adapter)
    runner._arm_flood_timers_for_waiting_rows = AsyncMock()
    assert await runner._redeliver_claimed_obligations(rows) == 1

    assert [(user, _notice() in content) for _chat, user, content, _meta in adapter.private] == [("user-1", True)] * private
    assert [_notice() in content for _chat, content, _meta in adapter.public] == [True] * public
    if private:
        assert adapter.private[0][3] == {"thread_id": "thread-1"}
    _close_store_db(store)
