"""Peer finalization over the Slack relay — end-to-end through the stream consumer (Salt B8).

The P5 fresh-final rule (a peer-addressed streamed final goes out as a NEW post, because
Slack notifies other apps only for new messages, never for ``chat.update``) left three
holes, each reproduced here by driving the REAL ``GatewayStreamConsumer`` →
``RelayAdapter`` → ``StubConnector`` path (no hand-supplied final send):

B8.1  Interim commentary carries the gateway-internal ``_interim_send`` marker, which the
      adapter strips before the wire.  The connector could not tell commentary from the
      final, so a ``@peer`` in commentary pinged the peer BEFORE the final.  The adapter
      must forward an explicit wire marker (``metadata.gg_interim = true``) the
      connector's peer-mention wrapper can skip; the final must NOT carry it.
B8.2  The fresh-final rule matched ``chat_type in ("channel", "group")`` only, but a
      genuine Slack thread reply arrives as ``chat_type == "thread"`` — its peer final
      stayed an edit.  Threads are included, and the fresh post keeps its thread anchor.
B8.3  ``_edit_existing`` returned on identical text BEFORE the fresh-final hook, so a
      complete, cursor-free preview (``cursor=""``) whose final text matched stayed an
      un-notifying preview.  The fresh-final requirement is evaluated first.
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.relay.adapter import RelayAdapter
from gateway.session import SessionSource
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from tests.gateway.relay.stub_connector import StubConnector
from tests.gateway.relay.test_relay_slack_prompt_dm_root import _slack_desc

PEER_FINAL = "Reviewed. <@U0BV1AXFR61> please take it from here."


def _wire(chat_id: str, chat_type: str):
    stub = StubConnector(_slack_desc())
    adapter = RelayAdapter(PlatformConfig(), _slack_desc(), transport=stub)
    src = SessionSource(
        platform=Platform.SLACK, chat_id=chat_id, chat_type=chat_type, user_id="U1", scope_id="T1",
    )
    adapter._capture_scope(MessageEvent(text="hi", source=src, message_type=MessageType.TEXT))
    return adapter, stub


def _consumer(adapter, chat_id, *, chat_type, metadata, cursor=None, reply_to="1700.0002"):
    kw = {} if cursor is None else {"cursor": cursor}
    cfg = StreamConsumerConfig(edit_interval=0.0, buffer_threshold=1, transport="edit",
                               chat_type=chat_type, **kw)
    return GatewayStreamConsumer(adapter=adapter, chat_id=chat_id, config=cfg,
                                 metadata=metadata, initial_reply_to_id=reply_to)


async def _run_ticks(consumer, *steps):
    """Run the consumer loop and apply each step (a callable) on its own tick, like the
    live delta callback does between loop iterations."""
    task = asyncio.create_task(consumer.run())
    for step in steps:
        step()
        await asyncio.sleep(0.12)  # > the loop's 0.05s yield: one tick per step
    consumer.finish()
    await asyncio.wait_for(task, timeout=5.0)


def _sends(stub):
    return [f for f in stub.sent if f["op"] == "send"]


# ── B8.1: interim commentary is marked on the wire, the final is not ──────────
@pytest.mark.asyncio
async def test_commentary_carries_interim_wire_marker_and_final_does_not():
    adapter, stub = _wire("C1", "channel")
    consumer = _consumer(adapter, "C1", chat_type="channel", metadata=None)
    await _run_ticks(
        consumer,
        lambda: consumer.on_commentary("Looking into it, <@U0BV1AXFR61> — one sec."),
        lambda: consumer.on_delta(PEER_FINAL),
    )
    sends = _sends(stub)
    assert len(sends) >= 2, [f["op"] for f in stub.sent]
    commentary = sends[0]
    assert commentary["content"].startswith("Looking into it")
    assert commentary["metadata"].get("gg_interim") is True, (
        "the connector's peer-mention wrapper needs an explicit interim marker to skip"
    )
    # The gateway-internal flag never reaches the wire.
    assert "_interim_send" not in commentary["metadata"]
    final = sends[-1]
    assert final["content"] == PEER_FINAL
    assert "gg_interim" not in final["metadata"], "the notifying final must not be marked interim"
    assert "_interim_send" not in final["metadata"]


# ── B8.2: a Slack thread reply gets the fresh peer final, anchored in its thread ──
@pytest.mark.asyncio
async def test_thread_reply_peer_final_is_fresh_post_in_same_thread():
    adapter, stub = _wire("C1", "thread")
    consumer = _consumer(adapter, "C1", chat_type="thread", metadata={"thread_id": "1699.9000"})
    await _run_ticks(
        consumer,
        lambda: consumer.on_delta("Reviewed."),
        lambda: consumer.on_delta(" <@U0BV1AXFR61> please take it from here."),
    )
    ops = [f["op"] for f in stub.sent]
    assert ops[0] == "send"
    assert ops[-1] == "send", f"peer final in a thread must be a fresh post, got {ops}"
    final = stub.sent[-1]
    assert final["content"] == PEER_FINAL
    assert final["metadata"].get("thread_id") == "1699.9000", "fresh final left its thread"


@pytest.mark.asyncio
async def test_dm_peer_final_stays_edit():
    """Control: a DM has no peer to notify — the fresh-final widening is thread-only."""
    adapter, stub = _wire("D1", "dm")
    consumer = _consumer(adapter, "D1", chat_type="dm", metadata=None)
    await _run_ticks(
        consumer,
        lambda: consumer.on_delta("Reviewed."),
        lambda: consumer.on_delta(" <@U0BV1AXFR61> please take it from here."),
    )
    ops = [f["op"] for f in stub.sent]
    assert ops[0] == "send" and ops[-1] == "edit", ops


# ── B8.3: identical cursor-free preview still gets the notifying fresh final ──
@pytest.mark.asyncio
async def test_complete_cursor_free_preview_still_posts_fresh_peer_final():
    adapter, stub = _wire("C1", "channel")
    consumer = _consumer(adapter, "C1", chat_type="channel", metadata=None, cursor="")
    await _run_ticks(
        consumer,
        lambda: consumer.on_delta("Reviewed."),
        # The preview now holds the COMPLETE final text (no cursor to differ by).
        lambda: consumer.on_delta(" <@U0BV1AXFR61> please take it from here."),
    )
    ops = [f["op"] for f in stub.sent]
    assert ops[0] == "send"
    assert "edit" in ops, "the preview should have been edited up to the full text"
    assert ops[-1] == "send", (
        f"unchanged-text shortcut swallowed the notifying fresh final: {ops}"
    )
    assert stub.sent[-1]["content"] == PEER_FINAL
    assert consumer.final_response_sent is True
