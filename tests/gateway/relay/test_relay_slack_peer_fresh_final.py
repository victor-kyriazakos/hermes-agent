"""Peer mentions must land in a FRESH message (P5).

Slack emits a ``message`` event to other apps only for new posts. Edit-based
streaming introduces the ``<@Uxxx>`` peer token via ``chat.update``, which
reaches peers as ``message_changed`` and is dropped as a non-message by every
connector (Slack semantics: edits never notify). Live 2026-09-05 18:09: alice's
final linked @carol correctly, carol's app saw only the mention-less preview and
dropped it on relevance. So a streamed final that addresses a peer must go out
through ``send`` (fresh post), which the stream consumer's fresh-final lane does
when ``prefers_fresh_final_streaming`` says so.
"""
from gateway.config import PlatformConfig
from gateway.relay.adapter import RelayAdapter
from tests.gateway.relay.test_relay_slack_prompt_dm_root import StubConnector, _slack_desc


def _adapter(extra=None):
    a = RelayAdapter(PlatformConfig(), _slack_desc(), transport=StubConnector(_slack_desc()))
    a.config.extra = {"slack": dict(extra or {})}
    a._platform_by_chat["C1"] = "slack"
    a._chat_type_by_chat["C1"] = "channel"  # captured from inbound, like live
    return a


def test_peer_token_in_channel_final_prefers_fresh():
    a = _adapter()
    # Live flat-channel streams carry only reply anchors in metadata (no chat_type):
    # the adapter must use the chat_type captured from inbound.
    meta = {"reply_to_message_id": "1.1"}
    assert a.prefers_fresh_final_streaming("<@U0BV1AXFR61> please review", metadata=meta, chat_id="C1") is True


def test_peer_name_in_channel_final_prefers_fresh():
    # The connector rewrites ``@carol`` on egress; the gateway only sees the name.
    a = _adapter()
    meta = {"platform": "slack", "chat_type": "channel"}
    assert a.prefers_fresh_final_streaming("@carol please review this", metadata=meta, chat_id="C1") is True


def test_plain_final_keeps_edit_lane():
    a = _adapter()
    meta = {"platform": "slack", "chat_type": "channel"}
    assert a.prefers_fresh_final_streaming("done, see above", metadata=meta, chat_id="C1") is False


def test_email_is_not_a_mention():
    a = _adapter()
    meta = {"platform": "slack", "chat_type": "channel"}
    assert a.prefers_fresh_final_streaming("mail ops@example.com", metadata=meta, chat_id="C1") is False


def test_dm_final_keeps_edit_lane():
    # No peer can be in a DM; nothing to notify.
    a = _adapter()
    a._chat_type_by_chat["C1"] = "dm"
    assert a.prefers_fresh_final_streaming("<@U0BV1AXFR61> hi", metadata={}, chat_id="C1") is False


def test_unfurl_trigger_still_works():
    a = _adapter({"unfurl_links": True})
    assert a.prefers_fresh_final_streaming("see https://x.dev", chat_id="C1") is True
