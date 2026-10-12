"""Slack ``send_private_notice``: the ephemeral surface sender-only notices use.

The gateway delivers the unexpected-silence notice in a shared conversation through this
(tests/gateway/test_unexpected_silence_private_notice.py) and falls back to a public send on a
failed result, so the adapter itself must report failure and never post publicly.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

import tests.gateway.test_slack_ignore_other_user_mentions  # mocks the slack modules if absent
from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter

THREAD = "1234567890.123456"


@pytest.fixture()
def slack_adapter():
    a = SlackAdapter(PlatformConfig(enabled=True, token="***"))
    a._app = MagicMock()
    a._app.client = AsyncMock()
    a._bot_user_id = "U_BOT"
    a._running = True
    return a


@pytest.mark.asyncio
async def test_send_private_notice_uses_ephemeral_api_in_the_thread(slack_adapter):
    slack_adapter._app.client.chat_postEphemeral = AsyncMock(return_value={"ok": True, "message_ts": "123.456"})

    result = await slack_adapter.send_private_notice(
        chat_id="C123", user_id="U123", content="private hello", metadata={"thread_id": THREAD})

    assert result.success
    slack_adapter._app.client.chat_postEphemeral.assert_called_once_with(
        channel="C123", user="U123", text="private hello", mrkdwn=True, thread_ts=THREAD)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"ok": False, "error": "user_not_in_channel"}, {}, RuntimeError("boom")],
    ids=["not-ok", "unexpected-shape", "raises"])
async def test_send_private_notice_failure_is_a_failed_result_not_a_public_post(slack_adapter, response):
    """The caller owns the fallback; the private surface itself never posts publicly."""
    # A one-item side_effect returns the payload, or raises it when it is an exception.
    slack_adapter._app.client.chat_postEphemeral = AsyncMock(side_effect=[response])
    slack_adapter._app.client.chat_postMessage = AsyncMock()

    result = await slack_adapter.send_private_notice(
        chat_id="C123", user_id="U123", content="private hello", metadata={"thread_id": THREAD})

    assert not result.success
    slack_adapter._app.client.chat_postMessage.assert_not_called()
