"""Native Slack Block Kit clarify + approval over the relay path.

The enterprise Slack lane is served by ``RelayAdapter`` (not the native Bolt
``SlackAdapter``): the gateway hands an OutboundAction to the Team Gateway
connector, whose slackRestSender renders Block Kit ONLY when the action carries
``metadata.blocks`` and falls back to the ``content`` text otherwise.

Before this change RelayAdapter used the base ``send_clarify`` default (a
numbered text list) and had no ``send_exec_approval`` override, so Slack showed
a plain "1. … / 2. … / Reply with the number" prompt instead of interactive
buttons (the reported screenshot).

These are behaviour-contract tests, not snapshots: they assert how the emitted
metadata.blocks relate to the choices/command (button count, encoded values,
retained text fallback) and how an inbound tap routes to the resolve
primitives — the invariants a connector on the other end depends on.
"""

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.relay.adapter import RelayAdapter
from gateway.relay.descriptor import CONTRACT_VERSION, CapabilityDescriptor
from gateway.session import SessionSource


# ---------------------------------------------------------------------------
# Harness (mirrors tests/gateway/relay/test_relay_adapter.py)
# ---------------------------------------------------------------------------
def _make_desc(**kw) -> CapabilityDescriptor:
    base = dict(
        contract_version=CONTRACT_VERSION,
        platform="slack",
        label="Slack",
        max_message_length=4000,
        supports_draft_streaming=False,
        supports_edit=True,
        supports_threads=True,
        markdown_dialect="mrkdwn",
        len_unit="chars",
        emoji="\U0001f4ac",
        platform_hint="",
        pii_safe=False,
    )
    base.update(kw)
    return CapabilityDescriptor(**base)


class _CaptureTransport:
    """Minimal RelayTransport stand-in that records the outbound action."""

    def __init__(self):
        self.sent = None
        self.sent_platform = None
        self._identities = []

    def set_inbound_handler(self, h):  # noqa: D401
        self._h = h

    async def send_outbound(self, action, *, platform=None):
        self.sent = action
        self.sent_platform = platform
        return {"success": True, "message_id": "m1"}


def _slack_adapter():
    t = _CaptureTransport()
    a = RelayAdapter(PlatformConfig(), _make_desc(platform="slack"), transport=t)
    # A clarify/approval is always sent mid-turn in reply to an inbound event;
    # _capture_scope learns the chat's underlying platform from that event.
    # Prime it so _chat_is_slack("C1") is True (matches the live path).
    a._capture_scope(_slack_event("C1", text="hi"))
    return a, t


def _slack_event(chat_id="C1", *, text="hi", user_id="U1"):
    src = SessionSource(
        platform=Platform.SLACK,
        chat_id=chat_id,
        chat_type="channel",
        user_id=user_id,
    )
    return MessageEvent(text=text, source=src, message_type=MessageType.TEXT)


def _actions_blocks(blocks):
    return [b for b in blocks if b.get("type") == "actions"]


def _buttons(blocks):
    out = []
    for b in _actions_blocks(blocks):
        out.extend(b.get("elements", []))
    return out


# ---------------------------------------------------------------------------
# (a) send_clarify with choices → metadata.blocks with N+1 buttons + text
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_clarify_with_choices_emits_blocks_and_keeps_text():
    a, t = _slack_adapter()
    choices = ["Delete the file", "Keep it", "Move to trash"]

    result = await a.send_clarify(
        chat_id="C1",
        question="What should I do?",
        choices=choices,
        clarify_id="cid7",
        session_key="agent:main:slack:C1",
    )
    assert result.success is True

    meta = t.sent["metadata"]
    blocks = meta.get("blocks")
    assert blocks is not None, "Slack clarify must carry Block Kit on metadata.blocks"

    buttons = _buttons(blocks)
    # One button per choice + a trailing "Other".
    assert len(buttons) == len(choices) + 1

    # Each choice button encodes cl:<clarify_id>:<idx>, in order.
    for idx in range(len(choices)):
        assert buttons[idx]["value"] == f"cl:cid7:{idx}"
    assert buttons[-1]["value"] == "cl:cid7:other"

    # A section block carries the question (mrkdwn).
    sections = [b for b in blocks if b.get("type") == "section"]
    assert sections, "expected a section block with the question"
    assert "What should I do?" in sections[0]["text"]["text"]

    # The numbered text is retained as the message content (notification /
    # accessibility / connector text fallback) — identical wording to the base
    # default so behaviour is unchanged when blocks are dropped.
    text = t.sent["content"]
    assert "1. Delete the file" in text
    assert "2. Keep it" in text
    assert "3. Move to trash" in text
    assert "Reply with the number" in text

    # State is tracked so the inbound tap can resolve the right session.
    assert a._clarify_state.get("cid7") == "agent:main:slack:C1"


@pytest.mark.asyncio
async def test_clarify_actions_block_caps_at_five_buttons():
    """Slack actions blocks cap at 5 elements; a larger choice list must chunk
    into multiple actions blocks rather than one oversized (400-ing) block."""
    a, t = _slack_adapter()
    choices = ["a", "b", "c", "d", "e", "f"]  # 6 choices + Other = 7 buttons
    await a.send_clarify(
        chat_id="C1",
        question="pick",
        choices=choices,
        clarify_id="cid9",
        session_key="s",
    )
    blocks = t.sent["metadata"]["blocks"]
    # No single actions block exceeds Slack's 5-element cap.
    for b in _actions_blocks(blocks):
        assert len(b["elements"]) <= 5
    assert len(_buttons(blocks)) == len(choices) + 1


# ---------------------------------------------------------------------------
# (b) open-ended clarify (no choices) → NO blocks, plain text
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_open_ended_clarify_emits_no_blocks():
    a, t = _slack_adapter()
    result = await a.send_clarify(
        chat_id="C1",
        question="What is your name?",
        choices=None,
        clarify_id="cidX",
        session_key="s",
    )
    assert result.success is True
    # Open-ended: plain question, no interactive blocks.
    assert "blocks" not in (t.sent["metadata"] or {})
    assert t.sent["content"] == "❓ What is your name?"


@pytest.mark.asyncio
async def test_non_slack_relay_chat_falls_back_to_numbered_text():
    """One relay adapter fronts N platforms. Block Kit is Slack-only, so a
    chat we know lives on a non-Slack platform must NOT get metadata.blocks —
    it falls through to the base numbered-text rendering."""
    t = _CaptureTransport()
    a = RelayAdapter(PlatformConfig(), _make_desc(platform="discord"), transport=t)
    # Prime the chat as Discord.
    src = SessionSource(platform=Platform.DISCORD, chat_id="D9", chat_type="channel")
    a._capture_scope(MessageEvent(text="hi", source=src, message_type=MessageType.TEXT))

    await a.send_clarify(
        chat_id="D9",
        question="q",
        choices=["one", "two"],
        clarify_id="cidD",
        session_key="s",
    )
    assert "blocks" not in (t.sent["metadata"] or {})
    assert "1. one" in t.sent["content"]


# ---------------------------------------------------------------------------
# (c) send_exec_approval → two buttons approve/deny + text fallback
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_exec_approval_emits_approve_deny_buttons_and_text():
    a, t = _slack_adapter()
    result = await a.send_exec_approval(
        chat_id="C1",
        command="rm -rf /tmp/scratch",
        session_key="agent:main:slack:C1",
        description="delete the temporary file",
    )
    assert result.success is True

    blocks = t.sent["metadata"].get("blocks")
    assert blocks is not None
    buttons = _buttons(blocks)
    assert len(buttons) == 2

    approve, deny = buttons
    assert approve["style"] == "primary"
    assert deny["style"] == "danger"

    # Values encode ap:<approval_id>:approve|deny with a shared approval_id.
    assert approve["value"].startswith("ap:")
    assert approve["value"].endswith(":approve")
    assert deny["value"].endswith(":deny")
    approval_id = approve["value"].split(":", 2)[1]
    assert deny["value"] == f"ap:{approval_id}:deny"

    # Command + reason survive in the text fallback (code block).
    text = t.sent["content"]
    assert "rm -rf /tmp/scratch" in text
    assert "delete the temporary file" in text
    assert "Approval Required" in text

    # State tracked: approval_id → session_key.
    assert a._exec_approval_state.get(approval_id) == "agent:main:slack:C1"


@pytest.mark.asyncio
async def test_exec_approval_non_slack_chat_has_no_blocks():
    t = _CaptureTransport()
    a = RelayAdapter(PlatformConfig(), _make_desc(platform="discord"), transport=t)
    src = SessionSource(platform=Platform.DISCORD, chat_id="D9", chat_type="channel")
    a._capture_scope(MessageEvent(text="hi", source=src, message_type=MessageType.TEXT))
    await a.send_exec_approval(
        chat_id="D9",
        command="rm -rf /",
        session_key="s",
        description="danger",
    )
    assert "blocks" not in (t.sent["metadata"] or {})
    # No state stored — there's no inbound block_action to resolve it.
    assert not a._exec_approval_state


# ---------------------------------------------------------------------------
# (d) inbound action value routing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_inbound_clarify_tap_resolves_gateway_clarify(monkeypatch):
    a, _t = _slack_adapter()
    # Register a real clarify entry so the canonical-choice-text lookup works.
    from tools import clarify_gateway as cg

    cg.register(
        clarify_id="cid7",
        session_key="agent:main:slack:C1",
        question="What should I do?",
        choices=["Delete the file", "Keep it", "Move to trash"],
    )
    a._clarify_state["cid7"] = "agent:main:slack:C1"

    resolved = {}

    def _fake_resolve(clarify_id, response):
        resolved["id"] = clarify_id
        resolved["response"] = response
        return True

    monkeypatch.setattr(cg, "resolve_gateway_clarify", _fake_resolve)

    # Connector relays the block_action click as a MessageEvent whose text is
    # the encoded button value: cl:cid7:2 (the third choice, index 2).
    claimed = a._maybe_resolve_interaction(_slack_event("C1", text="cl:cid7:2"))
    assert claimed is True
    assert resolved["id"] == "cid7"
    # Resolved with the canonical human-readable choice text, not the index.
    assert resolved["response"] == "Move to trash"
    # State popped after a successful resolve.
    assert "cid7" not in a._clarify_state

    cg.clear_session("agent:main:slack:C1")


@pytest.mark.asyncio
async def test_inbound_clarify_other_flips_to_text_capture(monkeypatch):
    a, _t = _slack_adapter()
    from tools import clarify_gateway as cg

    cg.register(
        clarify_id="cidO",
        session_key="s",
        question="q",
        choices=["one", "two"],
    )
    a._clarify_state["cidO"] = "s"

    flips = {}
    monkeypatch.setattr(
        cg, "mark_awaiting_text", lambda cid: flips.setdefault("id", cid) or True
    )

    claimed = a._maybe_resolve_interaction(_slack_event("C1", text="cl:cidO:other"))
    assert claimed is True
    assert flips["id"] == "cidO"
    # 'Other' keeps the mapping live for a future tap on the same prompt.
    assert a._clarify_state.get("cidO") == "s"

    cg.clear_session("s")


@pytest.mark.asyncio
async def test_inbound_approval_tap_resolves_gateway_approval(monkeypatch):
    a, _t = _slack_adapter()
    a._exec_approval_state["appr1"] = "agent:main:slack:C1"

    calls = {}

    def _fake_resolve(session_key, choice):
        calls["session_key"] = session_key
        calls["choice"] = choice
        return 1

    from tools import approval as approval_mod

    monkeypatch.setattr(approval_mod, "resolve_gateway_approval", _fake_resolve)

    claimed = a._maybe_resolve_interaction(_slack_event("C1", text="ap:appr1:approve"))
    assert claimed is True
    # resolve_gateway_approval takes the SESSION KEY (not the approval_id).
    assert calls["session_key"] == "agent:main:slack:C1"
    assert calls["choice"] == "approve"
    assert "appr1" not in a._exec_approval_state


@pytest.mark.asyncio
async def test_inbound_approval_deny_routes_choice(monkeypatch):
    a, _t = _slack_adapter()
    a._exec_approval_state["appr2"] = "s2"
    from tools import approval as approval_mod

    seen = {}
    monkeypatch.setattr(
        approval_mod,
        "resolve_gateway_approval",
        lambda sk, choice: (seen.update(sk=sk, choice=choice), 1)[1],
    )
    claimed = a._maybe_resolve_interaction(_slack_event("C1", text="ap:appr2:deny"))
    assert claimed is True
    assert seen == {"sk": "s2", "choice": "deny"}


@pytest.mark.asyncio
async def test_inbound_plain_text_is_not_claimed():
    """A normal chat message (no cl:/ap: prefix) must fall through to the agent
    path — the interaction dispatcher only claims encoded button values."""
    a, _t = _slack_adapter()
    assert a._maybe_resolve_interaction(_slack_event("C1", text="hello there")) is False


@pytest.mark.asyncio
async def test_inbound_stale_clarify_tap_falls_through():
    """A tap with no matching state (stale / after restart) is NOT claimed, so
    the caller can fall back to normal dispatch instead of silently swallowing
    it."""
    a, _t = _slack_adapter()
    assert a._maybe_resolve_interaction(_slack_event("C1", text="cl:gone:0")) is False


@pytest.mark.asyncio
async def test_on_inbound_claims_interaction_before_agent_dispatch(monkeypatch):
    """End-to-end inbound seam: _on_inbound must claim a button tap and NOT
    forward it to handle_message (which would spawn a bogus agent turn on the
    raw 'ap:...' string)."""
    a, _t = _slack_adapter()
    a._exec_approval_state["apX"] = "sX"

    from tools import approval as approval_mod

    monkeypatch.setattr(approval_mod, "resolve_gateway_approval", lambda sk, c: 1)

    dispatched = []

    async def _fake_handle(event):
        dispatched.append(event)

    monkeypatch.setattr(a, "handle_message", _fake_handle)

    await a._on_inbound(_slack_event("C1", text="ap:apX:approve"))
    assert dispatched == [], "a claimed button tap must not reach handle_message"


@pytest.mark.asyncio
async def test_on_inbound_forwards_normal_message_to_agent(monkeypatch):
    a, _t = _slack_adapter()
    dispatched = []

    async def _fake_handle(event):
        dispatched.append(event)

    monkeypatch.setattr(a, "handle_message", _fake_handle)
    monkeypatch.setattr(a, "_localize_inbound_media", lambda e: _noop())

    ev = _slack_event("C1", text="just a normal message")
    await a._on_inbound(ev)
    assert dispatched == [ev]


async def _noop():
    return None
