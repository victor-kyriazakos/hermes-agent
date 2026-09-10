"""Public auxiliary ownership and outer/physical retry contracts."""
import json
from types import SimpleNamespace as NS

import pytest
from agent import auxiliary_client as aux, relay_runtime
from tests.agent.test_relay_passive_capture import capture, flush


def plan(monkeypatch, client):
    def prepare(*args, **kwargs):
        aux._set_relay_auxiliary_route("anthropic", "model", "anthropic_messages")
        return NS(client=client, kwargs={"model": "model", "messages": kwargs["messages"]},
                  request_provider="anthropic", resolved_api_mode="anthropic_messages",
                  base_info="", resolved_base_url=""), {}, {}
    monkeypatch.setattr(aux, "_plan_aux_call", prepare)


def native_client(outcome, calls):
    error = aux.AuxiliaryExplicitCancellation() if outcome == "cancelled" else ValueError("native failure")
    message = NS(id="native", type="message", role="assistant", model="model",
                 content=[NS(type="text", text="native-result")], stop_reason="end_turn", usage=None)
    class Stream:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def close(self): pass
        def get_final_message(self): return message
        def __iter__(self):
            yield NS(type="message_start", message=message)
            if outcome != "success": raise error
    def stream(**kwargs):
        calls.append(kwargs)
        return Stream()
    client = aux.AnthropicAuxiliaryClient(NS(messages=NS(stream=stream)), "model", "test", "https://example.invalid")
    return client, error


@pytest.mark.parametrize("outcome", ["success", "failed", "cancelled"])
def test_public_native_explicit_stream_finishes_logical(capture, monkeypatch, outcome):
    events, _, _ = capture
    calls = []
    client, error = native_client(outcome, calls)
    plan(monkeypatch, client)
    if outcome == "success":
        assert aux.call_llm("title", messages=[{"role": "user", "content": "input"}], stream=True).choices[0].message.content == "native-result"
    else:
        with pytest.raises(type(error)) as raised:
            aux.call_llm("title", messages=[{"role": "user", "content": "input"}], stream=True)
        assert raised.value is error
    attempts = flush(events, "anthropic.messages")
    assert len(calls) == 1
    assert len(attempts) == 2
    assert attempts[0]["uuid"] == attempts[1]["uuid"]
    logical = [e for e in events if e["name"] == relay_runtime.LOGICAL_LLM_SCOPE]
    assert len(logical) == 2
    assert logical[0]["uuid"] == logical[1]["uuid"]
    assert logical[-1]["data"]["outcome"] == outcome
    assert logical[0]["metadata"]["hermes.api_request_id"] == attempts[0]["metadata"]["api_request_id"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_public_negotiation_then_outer_retry(capture, monkeypatch, asynchronous):
    import asyncio
    events, _, _ = capture
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1: raise ValueError("stream not supported")
        if len(calls) == 2: raise ConnectionError("connection reset")
        return NS(choices=[NS(message=NS(content="ok"))])
    async def acreate(**kwargs): return create(**kwargs)
    client = NS(chat=NS(completions=NS(create=acreate if asynchronous else create)))
    plan(monkeypatch, client)
    monkeypatch.setattr(aux, "_should_retry_same_provider", lambda *a: True)
    monkeypatch.setattr(aux, "_transient_retry_count", lambda: 1)
    monkeypatch.setattr(aux.time, "sleep", lambda *_: None)
    with aux.aux_progress_hook(lambda: None):
        result = asyncio.run(aux.async_call_llm("title", messages=[])) if asynchronous else aux.call_llm("title", messages=[])
    assert result.choices[0].message.content == "ok"
    flush(events, "unused")
    starts = [e for e in events if e["category"] == "llm" and e["scope_category"] == "start"]
    assert len(calls) == len(starts) == 3
    assert [e["metadata"]["attempt_ordinal"] for e in starts] == [0, 1, 2]
    assert [e["metadata"]["retry_count"] for e in starts] == [0, 0, 1]
    assert len({e["metadata"]["api_request_id"] for e in starts}) == 1


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("outcome", ["success", "failed", "cancelled", "close", "close_mid"])
def test_public_standalone_stream_lifetime(tmp_path, monkeypatch, native, outcome):
    import nemo_relay as relay
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    host = relay_runtime.get_runtime()
    host.retain_managed_execution("test.public")
    events, calls = [], []
    relay.subscribers.register("test.public", lambda e: events.append(json.loads(e.to_json())))
    try:
        error = aux.AuxiliaryExplicitCancellation() if outcome == "cancelled" else ValueError("wire failed")
        if native:
            client, error = native_client("success" if outcome.startswith("close") else outcome, calls)
        else:
            def chunks():
                yield NS(choices=[NS(index=0, delta=NS(content="chunk"))])
                if outcome not in {"success", "close"}: raise error
            def create(**kwargs):
                calls.append(kwargs)
                return chunks()
            client = NS(chat=NS(completions=NS(create=create)))
        plan(monkeypatch, client)
        if native and outcome in {"failed", "cancelled"}:
            with pytest.raises(type(error)) as raised:
                aux.call_llm("title", messages=[], stream=True)
            assert raised.value is error
        else:
            result = aux.call_llm("title", messages=[], stream=True)
            assert relay_runtime.current_turn() is None
            if not native:
                assert host._sessions
                flush(events, "unused")
                assert not [e for e in events if e["name"] == relay_runtime.SESSION_SCOPE and e["scope_category"] == "end"]
                if outcome == "success":
                    assert len(list(result)) == 1
                elif outcome.startswith("close"):
                    if outcome == "close_mid":
                        next(result)
                    result.close()
                    result.close()
                else:
                    with pytest.raises(type(error)) as raised:
                        list(result)
                    assert raised.value is error
        flush(events, "unused")
        assert not host._sessions
        logical = [e for e in events if e["name"] == relay_runtime.LOGICAL_LLM_SCOPE and e["scope_category"] == "end"]
        assert len(logical) == 1
        expected = ("success" if native else "cancelled") if outcome.startswith("close") else outcome
        assert logical[0]["data"]["outcome"] == expected
        turns = [e for e in events if e["name"] == relay_runtime.TURN_SCOPE and e["scope_category"] == "end"]
        assert len(turns) == 1
        assert turns[0]["data"]["outcome"] == expected
    finally:
        relay.subscribers.deregister("test.public")
        relay_runtime._reset_for_tests()


@pytest.mark.parametrize("during_close", [False, True])
def test_standalone_unrelated_generator_exit_is_not_cancellation(tmp_path, monkeypatch, during_close):
    import nemo_relay as relay
    from agent.relay_auxiliary import call_with_stream_lifetime, standalone_context
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    host = relay_runtime.get_runtime()
    host.retain_managed_execution("test.unrelated")
    events = []
    relay.subscribers.register("test.unrelated", lambda e: events.append(json.loads(e.to_json())))
    error = GeneratorExit("unrelated provider failure")
    class Stream:
        def __next__(self):
            raise error
        def close(self):
            raise error
    try:
        stream = call_with_stream_lifetime(standalone_context("unrelated"), Stream)
        with pytest.raises(GeneratorExit) as raised:
            stream.close() if during_close else next(stream)
        assert raised.value is error
        flush(events, "unused")
        turns = [e for e in events if e["name"] == relay_runtime.TURN_SCOPE and e["scope_category"] == "end"]
        assert len(turns) == 1
        assert turns[0]["data"]["outcome"] == "failed"
        assert not host._sessions
        assert relay_runtime.current_turn() is None
    finally:
        relay.subscribers.deregister("test.unrelated")
        relay_runtime._reset_for_tests()


@pytest.mark.parametrize("disabled_turn", [False, True])
def test_public_stream_without_consumer_preserves_identity(tmp_path, monkeypatch, disabled_turn):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    stream = iter(["unchanged"])
    client = NS(chat=NS(completions=NS(create=lambda **kwargs: stream)))
    plan(monkeypatch, client)
    try:
        if disabled_turn:
            token = relay_runtime._CURRENT_TURN.set(NS(relay_enabled=False, closed=False))
            try:
                assert aux.call_llm("title", messages=[], stream=True) is stream
            finally:
                relay_runtime._CURRENT_TURN.reset(token)
        else:
            assert aux.call_llm("title", messages=[], stream=True) is stream
        assert relay_runtime.get_runtime(create=False) is None
    finally:
        relay_runtime._reset_for_tests()


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
@pytest.mark.parametrize("policy", ["replace", "null", "throw"])
def test_public_native_error_payload_sanitizer(capture, monkeypatch, outcome, policy):
    import nemo_relay as relay
    events, _, _ = capture
    calls, sanitized = [], []
    client, error = native_client(outcome, calls)
    plan(monkeypatch, client)
    def sanitize(response, context):
        sanitized.append(response)
        if policy == "throw": raise ValueError("sanitizer failed")
        if policy == "null": return None
        return {"sanitized": "replacement"}
    relay.guardrails.register_llm_sanitize_response("test.aux.payload", 0, sanitize)
    try:
        with pytest.raises(type(error)) as raised:
            aux.call_llm("title", messages=[], stream=True)
        assert raised.value is error
        attempts = flush(events, "anthropic.messages")
        assert len(calls) == 1 and sanitized
        end = next(e for e in attempts if e["scope_category"] == "end")
        assert "native-result" not in json.dumps(end["data"])
        if policy == "replace":
            assert end["data"] == {"sanitized": "replacement"}
        else:
            assert end["data"] is None
    finally:
        relay.guardrails.deregister_llm_sanitize_response("test.aux.payload")


@pytest.mark.parametrize("provider", ["openai-codex", "xai-oauth"])
def test_public_moa_native_bypass_retains_full_capture(capture, monkeypatch, provider):
    events, _, _ = capture
    calls = []
    response = {
        "id": "native", "model": "model", "status": "completed",
        "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "native-result"}]}],
        "vendor_extension": "native-only",
    }

    def create(**kwargs):
        calls.append(kwargs)
        from tests.agent.test_relay_auxiliary_stream_boundaries import ns
        return iter([
            ns({"type": "response.output_item.done", "output_index": 0, "item": response["output"][0]}),
            ns({"type": "response.completed", "response": response}),
        ])

    native = NS(api_key="test", base_url="https://example.invalid", responses=NS(create=create))
    client = aux.CodexAuxiliaryClient(native, "model")

    def prepare(*args, **kwargs):
        aux._set_relay_auxiliary_route(provider, "model", "codex_responses")
        return NS(client=client, kwargs={"model": "model", "messages": kwargs["messages"]},
                  request_provider=provider, resolved_api_mode="codex_responses"), {}, {}

    monkeypatch.setattr(aux, "_plan_aux_call", prepare)
    def wrong_stream(*args, **kwargs):
        pytest.fail("Native MoA response entered caller-owned Chat stream seam")
    monkeypatch.setattr(aux, "_relay_sync_stream", wrong_stream)
    result = aux.call_llm("moa_aggregator", messages=[{"role": "user", "content": "native-input"}], stream=True)
    assert result.choices[0].message.content == "native-result"
    flush(events, "unused")
    attempts = [e for e in events if e["category"] == "llm"]
    assert len(calls) == 1 and len(attempts) == 2
    assert attempts[0]["uuid"] == attempts[1]["uuid"]
    assert "native-input" in json.dumps(attempts[0]["data"])
    assert "native-only" in json.dumps(attempts[1]["data"])
    logical = [e for e in events if e["name"] == relay_runtime.LOGICAL_LLM_SCOPE and e["scope_category"] == "end"]
    assert len(logical) == 1 and logical[0]["data"]["outcome"] == "success"
