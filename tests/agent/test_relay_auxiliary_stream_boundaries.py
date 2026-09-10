"""Native physical streams must retain payloads before Chat display normalization."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from agent import auxiliary_client as aux
from tests.agent.test_relay_passive_capture import capture, flush


def ns(value):
    if isinstance(value, dict):
        return NS(**{k: ns(v) for k, v in value.items()})
    if isinstance(value, list):
        return [ns(v) for v in value]
    return value


@pytest.mark.parametrize("protocol", ["anthropic", "responses", "chat"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("outcome", ["success", "failed", "cancelled"])
def test_physical_stream_payload(capture, protocol, asynchronous, outcome):
    if protocol == "chat" and asynchronous:
        pytest.skip("Explicit async streaming is not an auxiliary public transport")
    events, _, _ = capture
    seen = []
    failure = aux.AuxiliaryExplicitCancellation() if outcome == "cancelled" else ValueError("broken wire")
    message = ns({"id": "native", "type": "message", "role": "assistant", "model": "model",
                  "content": [{"type": "text", "text": "partial-native"}], "stop_reason": "end_turn", "usage": None})
    if protocol == "anthropic":
        frames = [{"type": "message_start", "message": {"id": "native", "type": "message", "role": "assistant", "content": [], "vendor": "native-extension"}},
                  {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "partial-native"}}]
    elif protocol == "responses":
        frames = [{"type": "response.output_item.added", "output_index": 0, "item": {"id": "fc", "type": "function_call", "call_id": "call", "name": "tool", "arguments": ""}},
                  {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": "partial-native"}]
        if outcome == "success":
            frames = [{"type": "response.completed", "response": {"id": "native", "model": "model", "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "partial-native"}]}], "vendor": "native-extension"}}]
    else:
        frames = [{"id": "native", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "partial-native", "refusal": "retain-refusal"}}]}]
    class Stream:
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
        def close(self): pass
        def get_final_message(self): return message
        def __iter__(self):
            yield from map(ns, frames)
            if outcome != "success": raise failure
    def create(**kwargs):
        seen.append(kwargs)
        return Stream()
    native = NS(api_key="test", base_url="https://example.invalid", responses=NS(create=create), messages=NS(stream=create))
    async_client = None
    if protocol == "anthropic":
        client = aux.AnthropicAuxiliaryClient(native, "model", "test", "https://example.invalid")
        async_client = aux.AsyncAnthropicAuxiliaryClient(client)
    elif protocol == "responses":
        client = aux.CodexAuxiliaryClient(native, "model")
        async_client = aux.AsyncCodexAuxiliaryClient(client)
    else:
        client = NS(chat=NS(completions=NS(create=create)))
    kwargs = {"model": "model", "messages": [{"role": "system", "content": "native-system"}, {"role": "user", "content": "hello"}], "extra_headers": {"Authorization": "HEADER_CANARY"}}
    @aux._relay_auxiliary_call
    def run(task):
        if protocol == "chat":
            return list(aux._relay_sync_stream(client, {**kwargs, "stream": True}))
        return aux._relay_sync_completion(client, kwargs)
    @aux._relay_auxiliary_call_async
    async def arun(task):
        return await aux._relay_async_completion(async_client, kwargs)
    if outcome == "success":
        asyncio.run(arun("title")) if asynchronous else run("title")
    else:
        with pytest.raises(type(failure)) as raised:
            asyncio.run(arun("title")) if asynchronous else run("title")
        assert raised.value is failure
    flush(events, "unused")
    attempts = [e for e in events if e["category"] == "llm"]
    starts = [e for e in attempts if e["scope_category"] == "start"]
    ends = [e for e in attempts if e["scope_category"] == "end"]
    assert len(starts) == len(ends) == 1
    assert starts[0]["uuid"] == ends[0]["uuid"]
    assert "partial-native" in json.dumps(ends[0]["data"])
    assert starts[0]["name"] == {"anthropic": "anthropic.messages", "responses": "openai.responses", "chat": "openai.chat_completions"}[protocol]
    assert "HEADER_CANARY" not in json.dumps(attempts)
    assert seen[0]["extra_headers"]["Authorization"] == "HEADER_CANARY"
    if protocol == "responses":
        assert "instructions" in json.dumps(starts[0]["data"])
        assert "input" in json.dumps(starts[0]["data"])


@pytest.mark.parametrize("outcome", ["success", "failed", "cancelled"])
def test_async_chat_physical_stream_capture(capture, outcome):
    events, _, _ = capture
    seen, closed = [], []
    failure = asyncio.CancelledError() if outcome == "cancelled" else ValueError("broken async wire")
    frames = [
        {"id": "async-native", "model": "actual-model", "choices": [{"index": 0, "delta": {
            "role": "assistant", "content": "partial-native", "refusal": "retain-refusal",
            "tool_calls": [{"index": 0, "id": "call", "type": "function", "function": {"name": "lookup", "arguments": '{"q":'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"value"}'}}]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 11, "completion_tokens": 7}},
    ]
    class Stream:
        def __aiter__(self): return self.iterate()
        async def iterate(self):
            for frame in frames:
                await asyncio.sleep(0)
                yield ns(frame)
            if outcome != "success": raise failure
        async def close(self): closed.append(True)
    async def create(**kwargs):
        seen.append(kwargs)
        return Stream()
    client = NS(chat=NS(completions=NS(create=create)))
    kwargs = {"model": "model", "messages": [{"role": "system", "content": "system-canary"}, {"role": "user", "content": "history-canary"}],
              "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
              "extra_headers": {"Authorization": "HEADER_CANARY"}}
    @aux._relay_auxiliary_call_async
    async def run(task):
        with aux.aux_progress_hook(lambda: None):
            return await aux._relay_async_completion(client, kwargs)
    if outcome == "success":
        result = asyncio.run(run("title"))
        assert result.choices[0].message.tool_calls[0].function.arguments == '{"q":"value"}'
    else:
        with pytest.raises(type(failure)) as raised:
            asyncio.run(run("title"))
        assert raised.value is failure
    attempts = flush(events, "openai.chat_completions")
    starts = [e for e in attempts if e["scope_category"] == "start"]
    ends = [e for e in attempts if e["scope_category"] == "end"]
    assert len(seen) == len(starts) == len(ends) == 1
    assert starts[0]["uuid"] == ends[0]["uuid"]
    assert starts[0]["data"]["content"] == {k: v for k, v in seen[0].items() if k != "extra_headers"}
    assert seen[0]["stream"] is True
    payload = json.dumps(ends[0]["data"])
    assert "partial-native" in payload and "retain-refusal" in payload
    assert "lookup" in payload and "value" in payload
    # Managed native streams expose OTel status, not the passive lane's outcome.
    assert ends[0]["metadata"]["otel.status_code"] == ("OK" if outcome == "success" else "ERROR")
    assert "HEADER_CANARY" not in json.dumps(attempts)
    assert closed


@pytest.mark.parametrize("completed", [False, True])
def test_async_factory_priming_and_explicit_close(capture, completed):
    from agent import relay_llm
    events, _, _ = capture
    calls, pulls, closed = [], [], []
    response = ns({"choices": [{"message": {"content": "complete-object"}}]})
    class Stream:
        def __aiter__(self): return self
        async def __anext__(self):
            pulls.append(True)
            await asyncio.Event().wait()
        async def close(self): closed.append(True)
    async def factory(request):
        calls.append(request)
        return response if completed else Stream()
    @aux._relay_auxiliary_call_async
    async def run(task):
        stream = await asyncio.wait_for(relay_llm.stream_current_async(
            {"model": "model", "messages": [], "stream": True}, factory,
            name="openai", model_name="model", metadata={"api_mode": "chat_completions"},
            finalizer=lambda: {"choices": []},
            completed_response_predicate=lambda value: hasattr(value, "choices"),
        ), timeout=2)
        assert len(calls) == 1 and not pulls
        if completed:
            assert stream is response
        else:
            await asyncio.wait_for(stream.aclose(), timeout=2)
            await stream.aclose()
            assert closed == [True]
    asyncio.run(run("title"))
    attempts = flush(events, "openai.chat_completions")
    assert len([e for e in attempts if e["scope_category"] == "end"]) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("retry", ["credit", "negotiation"])
def test_progress_retry_is_two_physical_attempts(capture, retry, asynchronous):
    if asynchronous and retry == "credit":
        pytest.skip("Async credit-budget recovery is not an existing auxiliary behavior")
    events, _, _ = capture
    seen = []
    class CreditError(Exception):
        status_code = 402
    def create(**kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            if retry == "credit":
                raise CreditError("can only afford 2000 tokens")
            raise ValueError("stream not supported")
        return ns({"choices": [{"message": {"content": "retry-output"}}]})
    client = NS(chat=NS(completions=NS(create=create)))
    kwargs = {"model": "model", "max_tokens": 4000, "messages": [{"role": "user", "content": "retry-input"}]}
    @aux._relay_auxiliary_call
    def run(task):
        with aux.aux_progress_hook(lambda: None):
            return aux._relay_sync_completion(client, kwargs)
    @aux._relay_auxiliary_call_async
    async def arun(task):
        async def acreate(**request):
            return create(**request)
        async_client = NS(chat=NS(completions=NS(create=acreate)))
        with aux.aux_progress_hook(lambda: None):
            return await aux._relay_async_completion(async_client, kwargs)
    asyncio.run(arun("title")) if asynchronous else run("title")
    attempts = flush(events, "openai.chat_completions")
    starts = [e for e in attempts if e["scope_category"] == "start"]
    ends = [e for e in attempts if e["scope_category"] == "end"]
    assert len(seen) == len(starts) == len(ends) == 2
    assert len({e["uuid"] for e in starts}) == 2
    assert [e["metadata"]["retry_count"] for e in starts] == [0, 0]
    assert [e["metadata"]["attempt_ordinal"] for e in starts] == [0, 1]
    assert len({e["metadata"]["api_request_id"] for e in starts}) == 1
    assert "retry-output" in json.dumps(ends[-1]["data"])
    assert '"stream": true' in json.dumps(starts[0]["data"])
    if retry == "credit":
        assert seen[1]["max_tokens"] < seen[0]["max_tokens"]
    else:
        assert not seen[1].get("stream")
