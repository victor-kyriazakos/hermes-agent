"""Native auxiliary capture must happen after protocol preparation."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from agent import auxiliary_client as aux
from tests.agent.test_relay_passive_capture import capture, flush


@pytest.mark.parametrize("asynchronous", [False, True])
def test_anthropic_native_auxiliary_payload(capture, asynchronous):
    events, _, _ = capture
    seen = []
    def create(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(id="native-id", type="message", role="assistant", model="model",
                               content=[SimpleNamespace(type="text", text="native output")],
                               stop_reason="end_turn", usage=None, vendor_extension="native-only")
    native = SimpleNamespace(messages=SimpleNamespace(create=create))
    client = aux.AnthropicAuxiliaryClient(native, "model", "test", "https://example.invalid")
    kwargs = {"model": "model", "messages": [{"role": "system", "content": "system-native"},
              {"role": "user", "content": "hello"}], "extra_headers": {"Authorization": "HEADER_CANARY"}}
    @aux._relay_auxiliary_call
    def run(task):
        return aux._relay_sync_completion(client, kwargs)
    @aux._relay_auxiliary_call_async
    async def arun(task):
        return await aux._relay_async_completion(aux.AsyncAnthropicAuxiliaryClient(client), kwargs)
    result = asyncio.run(arun("title")) if asynchronous else run("title")
    assert result.choices[0].message.content == "native output"
    flush(events, "anthropic.messages")
    llm = [e for e in events if e["category"] == "llm"]
    assert len(llm) == 2
    start = next(e for e in llm if e["scope_category"] == "start")
    end = next(e for e in llm if e["scope_category"] == "end")
    assert start["name"] == "anthropic.messages"
    assert "system-native" in json.dumps(start["data"])
    assert "native-only" in json.dumps(end["data"])
    assert "HEADER_CANARY" not in json.dumps(llm)
    assert seen[0]["extra_headers"]["Authorization"] == "HEADER_CANARY"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_stream_rejection_and_cancel_have_separate_attempts(capture, asynchronous):
    events, _, _ = capture
    failure = aux.AuxiliaryExplicitCancellation()
    def stream(**kwargs):
        raise ValueError("stream not supported")
    def create(**kwargs):
        raise failure
    native = SimpleNamespace(messages=SimpleNamespace(create=create, stream=stream))
    client = aux.AnthropicAuxiliaryClient(native, "model", "test", "https://example.invalid")
    kwargs = {"model": "model", "messages": [{"role": "user", "content": "hello"}]}
    @aux._relay_auxiliary_call
    def run(task):
        return aux._relay_sync_completion(client, kwargs)
    @aux._relay_auxiliary_call_async
    async def arun(task):
        return await aux._relay_async_completion(aux.AsyncAnthropicAuxiliaryClient(client), kwargs)
    with pytest.raises(aux.AuxiliaryExplicitCancellation) as raised:
        asyncio.run(arun("title")) if asynchronous else run("title")
    assert raised.value is failure
    attempts = flush(events, "anthropic.messages")
    starts = [e for e in attempts if e["scope_category"] == "start"]
    assert len(starts) == 2
    assert starts[0]["uuid"] != starts[1]["uuid"]
    assert [e["metadata"]["retry_count"] for e in starts] == [0, 0]
    assert [e["metadata"]["attempt_ordinal"] for e in starts] == [0, 1]
    assert len({e["metadata"]["api_request_id"] for e in starts}) == 1
    ends = [e for e in attempts if e["scope_category"] == "end"]
    assert len(ends) == 2
