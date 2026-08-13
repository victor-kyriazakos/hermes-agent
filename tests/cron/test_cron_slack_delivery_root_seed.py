"""Slack thread-per-message parity: cron delivery roots must seed their thread session.

Live repro (relay-fronted Slack staging, 2026-08-13): with the relay Slack
extra ``reply_in_thread`` + DM-thread-sessions defaults active, every cron
delivery lands as a top-level message whose reply thread resolves to a
NEW thread-keyed session ``(slack, chat, thread=<delivery ts>)``. Nothing
seeded that session, so the user's reply under the delivered brief hit a
blank context ("this thread has no prior topic"). The flat-DM mirror is
invisible from there; the attach_to_session named-thread surface seeds a
DIFFERENT thread. Parity rule: whenever the platform mode makes a reply
spawn a thread session, the delivery root's session must carry the brief.

Flat mode (reply_in_thread off) is deliberately untouched: replies join the
flat session, where the existing mirror already applies.
"""

import asyncio
from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch

from cron import scheduler as sched
from cron.scheduler import _deliver_result, _slack_delivery_thread_sessions_enabled
from gateway.config import Platform


class TestModeDetection:
    def _adapter(self, reply_in_thread=True, dm_sessions=True):
        adapter = MagicMock()
        adapter._effective_reply_in_thread = lambda: reply_in_thread
        adapter._dm_top_level_threads_as_sessions = lambda: dm_sessions
        return adapter

    def test_thread_per_message_mode_detected(self):
        assert _slack_delivery_thread_sessions_enabled(self._adapter()) is True

    def test_flat_mode_not_detected(self):
        assert _slack_delivery_thread_sessions_enabled(
            self._adapter(reply_in_thread=False)) is False

    def test_dm_sessions_opt_out_not_detected(self):
        assert _slack_delivery_thread_sessions_enabled(
            self._adapter(dm_sessions=False)) is False

    def test_adapter_without_gates_fails_safe(self):
        assert _slack_delivery_thread_sessions_enabled(object()) is False


class TestDeliveryRootSeeding:
    def _relay_adapter(self, thread_mode=True):
        adapter = AsyncMock()
        adapter.fronts_platform = lambda p: p == Platform.SLACK
        adapter._effective_reply_in_thread = lambda: thread_mode
        adapter._dm_top_level_threads_as_sessions = lambda: thread_mode
        adapter._session_store = MagicMock()
        # No named-thread surface in this test: attach is off.
        adapter.create_handoff_thread = None
        return adapter

    def _job(self):
        return {
            "id": "seed-job",
            "name": "Seed Job",
            "deliver": "origin",
            "origin": {"platform": "slack", "chat_id": "D0BJTDCSR7C",
                       "chat_name": "dm", "thread_id": None},
        }

    def _run(self, adapter, message_id="1755099999.000200"):
        loop = MagicMock()
        loop.is_running.return_value = True

        def fake_run_coro(coro, _loop):
            future = Future()
            try:
                future.set_result(asyncio.run(coro))
            except BaseException as e:  # noqa: BLE001
                future.set_exception(e)
            return future

        router = MagicMock()

        async def _deliver_to_platform(target, content, metadata):
            return {"success": True, "message_id": message_id,
                    "raw_response": None}

        router._deliver_to_platform = _deliver_to_platform

        config = MagicMock()
        config.platforms = {}
        config.get_home_channel = lambda p: None

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("cron.scheduler.load_config",
                   return_value={"cron": {"wrap_response": False}}), \
             patch("gateway.delivery.DeliveryRouter", return_value=router), \
             patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro), \
             patch("gateway.mirror.mirror_to_session", return_value=True) as mirror:
            result = _deliver_result(
                self._job(), "Nightly brief.",
                adapters={Platform.RELAY: adapter}, loop=loop,
            )
        return result, mirror, adapter

    def test_thread_mode_seeds_delivery_root_session(self):
        """Top-level delivery in thread-per-message mode seeds (chat, ts=root)."""
        adapter = self._relay_adapter(thread_mode=True)
        result, mirror, adapter = self._run(adapter)
        assert result is None  # delivered clean
        # The thread-keyed session row was created for the delivery root.
        create_calls = adapter._session_store.get_or_create_session.call_args_list
        assert any(
            getattr(c.args[0], "thread_id", None) == "1755099999.000200"
            for c in create_calls
        ), f"no thread-keyed session created: {create_calls}"
        # And the brief was mirrored into it.
        assert mirror.called
        m_kwargs = mirror.call_args.kwargs
        assert "Nightly brief." in mirror.call_args.args[2]

    def test_flat_mode_does_not_seed(self):
        """Flat mode: replies join the flat session; no thread seeding."""
        adapter = self._relay_adapter(thread_mode=False)
        result, mirror, adapter = self._run(adapter)
        assert result is None
        create_calls = adapter._session_store.get_or_create_session.call_args_list
        assert not any(
            getattr(c.args[0], "thread_id", None) == "1755099999.000200"
            for c in create_calls
        )

    def test_no_message_id_no_seed(self):
        """Send result without a message id: nothing to key on, no seeding."""
        adapter = self._relay_adapter(thread_mode=True)
        result, mirror, adapter = self._run(adapter, message_id=None)
        assert result is None
        assert not adapter._session_store.get_or_create_session.called
