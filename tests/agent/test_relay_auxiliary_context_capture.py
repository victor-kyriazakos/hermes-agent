"""Auxiliary logical context is finite, gated, and cancellation-aware."""
import asyncio
import json
from types import SimpleNamespace

import pytest

relay = pytest.importorskip("nemo_relay")
from agent import auxiliary_client as aux, relay_runtime


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_standalone_auxiliary_closes_scopes_and_preserves_cancellation(tmp_path, monkeypatch, asynchronous, cancel):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    host = relay_runtime.get_runtime()
    host.retain_managed_execution("test.aux")
    events, calls = [], []
    relay.subscribers.register("test.aux", lambda e: events.append(json.loads(e.to_json())))
    error = aux.AuxiliaryExplicitCancellation()

    def provider(**kwargs):
        calls.append(kwargs)
        if cancel:
            raise error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="synthetic result"))])

    async def async_provider(**kwargs):
        return provider(**kwargs)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=async_provider if asynchronous else provider)))

    @aux._relay_auxiliary_call
    def sync_run(task):
        aux._set_relay_auxiliary_route("synthetic", "model", "chat_completions")
        return aux._validate_llm_response(aux._relay_sync_completion(client, {"model": "model", "messages": []}), task)

    @aux._relay_auxiliary_call_async
    async def async_run(task):
        aux._set_relay_auxiliary_route("synthetic", "model", "chat_completions")
        return aux._validate_llm_response(await aux._relay_async_completion(client, {"model": "model", "messages": []}), task)

    try:
        def invoke():
            return asyncio.run(async_run("title")) if asynchronous else sync_run("title")
        if cancel:
            with pytest.raises(aux.AuxiliaryExplicitCancellation) as raised:
                invoke()
            assert raised.value is error
        else:
            assert invoke().choices[0].message.content == "synthetic result"
        relay.subscribers.flush()
        assert len(calls) == 1
        sessions = [e for e in events if e["name"] == relay_runtime.SESSION_SCOPE]
        assert len(sessions) == 2
        assert sessions[0]["uuid"] == sessions[1]["uuid"]
        assert not host._sessions
        assert relay_runtime.current_turn() is None
        logical = [e for e in events if e["name"] == relay_runtime.LOGICAL_LLM_SCOPE and e["scope_category"] == "end"]
        assert len(logical) == 1
        assert logical[0]["data"]["outcome"] == ("cancelled" if cancel else "success")
    finally:
        relay.subscribers.deregister("test.aux")
        relay_runtime._reset_for_tests()


def test_standalone_auxiliary_does_not_activate_a_host_without_consumer(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    @aux._relay_auxiliary_call
    def run(task):
        assert relay_runtime.current_turn() is None
        return "ok"
    assert run("title") == "ok"
    assert relay_runtime.get_runtime(create=False) is None
