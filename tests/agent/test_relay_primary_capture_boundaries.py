"""Primary capture regressions at native lifecycle / preparation boundaries."""
import json
from types import SimpleNamespace
import pytest
from tests.agent.test_relay_passive_capture import capture, flush, relay
from agent import relay_llm, codex_runtime, chat_completion_helpers as chat
from agent.chat_completion_helpers_relay import RelayChatAccumulator


@pytest.mark.parametrize("ending", ["error", "cancel", "close"])
def test_managed_stream_retains_partial_on_abort(capture, ending):
    events, lease, _ = capture
    failure = InterruptedError("cancelled") if ending == "cancel" else ValueError("provider failed")
    def provider(request):
        yield {"id": "partial", "choices": [{"index": 0, "delta": {"content": "observed-partial"}}]}
        if ending == "close":
            while True:
                yield {"choices": []}
        raise failure
    acc = RelayChatAccumulator()
    stream = relay_llm.stream({"model": "synthetic", "stream": True}, provider,
        session_id=lease.session_id, name="primary-partial", model_name="synthetic",
        finalizer=acc.finalize, on_chunk=acc.observe,
        metadata={"api_request_id": "partial-logical"})
    next(stream)
    if ending == "close":
        stream.close()
    else:
        with pytest.raises(type(failure)) as raised:
            list(stream)
        assert raised.value is failure
    stream.close()
    ends = [e for e in flush(events, "primary-partial") if e["scope_category"] == "end"]
    assert len(ends) == 1
    assert "observed-partial" in json.dumps(ends[0]["data"])
    from agent import relay_runtime
    logical_ends = [e for e in flush(events, relay_runtime.LOGICAL_LLM_SCOPE) if e["scope_category"] == "end"]
    assert len(logical_ends) == 1
    assert logical_ends[0]["data"]["outcome"] == ("failed" if ending == "error" else "cancelled")


@pytest.mark.parametrize("policy", ["replace", "omit", "error"])
def test_partial_response_sanitizer_never_resurrects_raw(capture, policy):
    events, lease, _ = capture
    seen = []
    def sanitize(response, context):
        seen.append(response)
        if policy == "error":
            raise ValueError("sanitizer failed")
        return {"safe": "sanitized"} if policy == "replace" else None
    relay.guardrails.register_llm_sanitize_response("primary.sanitizer", 0, sanitize)
    failure = ValueError("provider failed")
    def provider(request):
        yield {"choices": [{"index": 0, "delta": {"content": "SECRET_PARTIAL_CANARY"}}]}
        raise failure
    acc = RelayChatAccumulator()
    try:
        stream = relay_llm.stream({}, provider, session_id=lease.session_id,
            name="sanitized-primary", model_name="synthetic", finalizer=acc.finalize, on_chunk=acc.observe)
        with pytest.raises(ValueError) as raised:
            list(stream)
        assert raised.value is failure
        ends = [e for e in flush(events, "sanitized-primary") if e["scope_category"] == "end"]
        assert len(ends) == 1
        assert "SECRET_PARTIAL_CANARY" not in json.dumps(ends)
        assert seen and "SECRET_PARTIAL_CANARY" in json.dumps(seen)
        if policy == "replace":
            assert "sanitized" in json.dumps(ends[0]["data"])
    finally:
        relay.guardrails.deregister_llm_sanitize_response("primary.sanitizer")


@pytest.mark.parametrize("ending", ["success", "error", "cancel", "close"])
def test_stream_close_publishes_sanitized_end_before_loop_shutdown(capture, monkeypatch, ending):
    """Hold native publication beyond aclose without relying on scheduler timing."""
    import asyncio
    from concurrent.futures import Future

    events, lease, _ = capture
    release = Future()
    sanitized = []
    loops = []
    close_requests = []
    original_aclose = relay_llm._aclose_on_loop

    def close_stream(loop, stream):
        loops.append(loop)
        real_close = loop.close

        def checked_close():
            close_requests.append(bool(sanitized) and any(
                e["name"] == "lifetime-primary" and e["scope_category"] == "end" for e in events))
            # Rescue only after recording the violation, so RED teardown cannot
            # strand native publication forever on an already closed loop.
            loop.run_until_complete(relay.subscribers.flush_async())
            real_close()

        monkeypatch.setattr(loop, "close", checked_close)
        try:
            original_aclose(loop, stream)
        finally:
            # The sanitizer cannot finish until after native producer cleanup.
            release.set_result(None)

    async def sanitize(response, context):
        await asyncio.wrap_future(release)
        sanitized.append(response)
        return {"safe": "published-before-loop-close"}

    monkeypatch.setattr(relay_llm, "_aclose_on_loop", close_stream)
    relay.guardrails.register_llm_sanitize_response("primary.lifetime", 0, sanitize)
    failure = InterruptedError("cancelled") if ending == "cancel" else ValueError("provider failed")

    def provider(request):
        yield {"choices": [{"index": 0, "delta": {"content": "LIFETIME_SECRET_CANARY"}}]}
        if ending == "close":
            while True:
                yield {"choices": []}
        if ending != "success":
            raise failure

    acc = RelayChatAccumulator()
    try:
        stream = relay_llm.stream({}, provider, session_id=lease.session_id,
            name="lifetime-primary", model_name="synthetic", finalizer=acc.finalize, on_chunk=acc.observe)
        if ending == "close":
            next(stream)
            stream.close()
        elif ending == "success":
            list(stream)
        else:
            with pytest.raises(type(failure)) as raised:
                list(stream)
            assert raised.value is failure
        assert close_requests == [True], "loop closed before sanitized END publication"
        assert sanitized and "LIFETIME_SECRET_CANARY" in json.dumps(sanitized)
        ends = [e for e in events if e["name"] == "lifetime-primary" and e["scope_category"] == "end"]
        assert len(ends) == 1
        assert ends[0]["data"] == {"safe": "published-before-loop-close"}
        assert "LIFETIME_SECRET_CANARY" not in json.dumps(ends)
        assert loops and all(loop.is_closed() for loop in loops)
    finally:
        relay.guardrails.deregister_llm_sanitize_response("primary.lifetime")


def test_collector_failure_does_not_replace_provider_exception(capture):
    _, lease, _ = capture
    failure = ValueError("provider failed")
    def provider(request):
        yield {"delta": "partial"}
        raise failure
    def broken_finalizer():
        raise RuntimeError("collector failed")
    stream = relay_llm.stream({}, provider, session_id=lease.session_id,
        name="broken-collector", model_name="synthetic", finalizer=broken_finalizer)
    with pytest.raises(ValueError) as raised:
        list(stream)
    assert raised.value is failure


def test_codex_capture_matches_prepared_sdk_request(monkeypatch):
    request = {"model": "synthetic", "input": [{"role": "user", "content": "hi"}],
               "prompt_cache_retention": "24h", "extra_body": {"prompt_cache_retention": "24h"}}
    original = json.loads(json.dumps(request))
    wire = []
    class StopProbe(Exception): pass
    def create(**kwargs):
        wire.append(kwargs)
        raise StopProbe()
    def capture_request(prepared, factory, **kwargs):
        with pytest.raises(StopProbe): factory(dict(prepared))
        sdk_body = {k: v for k, v in wire[0].items() if k != "extra_body"}
        sdk_body.update(wire[0].get("extra_body") or {})
        assert prepared == sdk_body
        assert prepared["stream"] is True
        raise StopProbe()
    monkeypatch.setattr(relay_llm, "stream", capture_request)
    agent = SimpleNamespace(_interrupt_requested=False, _is_codex_backend=lambda: True)
    with pytest.raises(StopProbe):
        codex_runtime.run_codex_stream(agent, request, client=SimpleNamespace(responses=SimpleNamespace(create=create)))
    assert request == original


def test_chat_capture_has_effective_stream_flags(monkeypatch):
    request = {"model": "synthetic", "messages": []}
    call = object.__new__(chat._StreamingCall)
    call.agent = SimpleNamespace(base_url="https://example.test/v1", session_id="", provider="test", model="synthetic")
    call.api_kwargs = request
    monkeypatch.setattr(call, "_stream_timeouts", lambda: (30, 30, 10))
    monkeypatch.setattr(call, "_new_diag", dict)
    monkeypatch.setattr(call, "_set_managed_stream", lambda value: value)
    class StopProbe(Exception): pass
    def capture_request(prepared, factory, **kwargs):
        assert prepared["stream"] is True
        assert prepared["stream_options"] == {"include_usage": True}
        raise StopProbe()
    monkeypatch.setattr(relay_llm, "stream", capture_request)
    with pytest.raises(StopProbe): call._call_chat_completions(0)
    assert request == {"model": "synthetic", "messages": []}
