"""Native boundary regressions: observation must not replay guarded execution."""
import asyncio
import contextvars
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

relay = pytest.importorskip("nemo_relay")
from agent import relay_llm, relay_runtime, relay_tools


@pytest.fixture
def capture(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    events = []
    relay.subscribers.register("test.passive", lambda event: events.append(json.loads(event.to_json())))
    coordinator = relay_runtime.SESSION_COORDINATOR
    lease = coordinator.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(), session_id="Session-Mixed", platform="cli")
    turn = coordinator.begin_turn(lease, turn_id="Turn-Mixed", task_id="task")
    lease.host.retain_managed_execution("test.passive")
    try:
        yield events, lease, turn
    finally:
        coordinator.end_turn(turn, outcome="success")
        coordinator.finalize_conversation(profile_key=lease.profile_key, session_id=lease.session_id)
        coordinator.release_conversation(lease)
        relay.subscribers.flush()
        relay.subscribers.deregister("test.passive")
        relay_runtime._reset_for_tests()


def flush(events, name):
    relay.subscribers.flush()
    return [e for e in events if e["name"] == name]


def test_headers_are_not_content_and_host_ids_join_scopes(capture):
    events, lease, turn = capture
    request = {"model": "synthetic", "messages": [{"role": "user", "content": "hello"}],
               "extra_headers": {"Authorization": "HEADER_CANARY", "x-test": "kept"}}
    seen = []
    result = {"choices": [{"message": {"role": "assistant", "content": "done"}}]}
    assert relay_llm.execute(request, lambda kwargs: seen.append(kwargs) or result,
                             name="synthetic", model_name="synthetic",
                             metadata={"api_mode": "chat_completions", "api_request_id": "Request-Mixed"}) is result
    attempts = flush(events, "openai.chat_completions")
    assert len(attempts) == 2
    assert "HEADER_CANARY" not in json.dumps(attempts)
    assert seen[0]["extra_headers"]["Authorization"] == "HEADER_CANARY"
    assert request["extra_headers"] == {"Authorization": "HEADER_CANARY", "x-test": "kept"}
    starts = {e["name"]: e for e in events if e["scope_category"] == "start"}
    assert starts[relay_runtime.SESSION_SCOPE]["metadata"]["hermes.session_id"] == lease.session_id
    assert starts[relay_runtime.TURN_SCOPE]["metadata"]["hermes.turn_id"] == turn.turn_id
    assert starts[relay_runtime.TURN_SCOPE]["metadata"]["hermes.session_id"] == lease.session_id
    assert starts[relay_runtime.LOGICAL_LLM_SCOPE]["metadata"]["hermes.api_request_id"] == "Request-Mixed"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("outcome", ["success", "failed", "cancelled"])
def test_guarded_child_thread_records_each_invocation_once(capture, asynchronous, outcome):
    events, lease, turn = capture
    calls = []
    failure = InterruptedError("synthetic cancellation") if outcome == "cancelled" else ValueError("synthetic failure")

    def provider(request):
        calls.append(request)
        if outcome != "success":
            raise failure
        return {"native_extension": ["nested-output"]}

    async def async_provider(request):
        await asyncio.sleep(0)
        return provider(request)

    def child():
        coordinator = relay_runtime.SESSION_COORDINATOR
        child_lease = coordinator.acquire_conversation(
            profile_key=lease.profile_key, session_id="Child-Mixed", platform="subagent", parent_session_id=lease.session_id)
        child_turn = coordinator.begin_turn(child_lease, turn_id="Child-Turn", task_id="child")
        try:
            def nested_tool(args):
                kwargs = dict(name="nested-provider", model_name="synthetic", metadata={"api_request_id": "Nested-Request"})
                if asynchronous:
                    return asyncio.run(relay_llm.execute_async({"input": "nested-input"}, async_provider, **kwargs))
                return relay_llm.execute({"input": "nested-input"}, provider, **kwargs)
            return relay_tools.execute("child-tool", {"effective": True}, nested_tool,
                                       session_id=child_lease.session_id, tool_call_id="tool-id")
        finally:
            coordinator.end_turn(child_turn, outcome=outcome)
            coordinator.release_conversation(child_lease)

    def delegate(args):
        assert relay_runtime.resolve_execution_context(lease.session_id) == (None, None, None)
        context = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(context.run, child).result(timeout=5)

    if outcome == "success":
        relay_tools.execute("delegate", {}, delegate, session_id=lease.session_id)
    else:
        with pytest.raises(type(failure)) as raised:
            relay_tools.execute("delegate", {}, delegate, session_id=lease.session_id)
        assert raised.value is failure
    assert len(calls) == 1
    attempts = flush(events, "nested-provider")
    assert len(attempts) == 2
    assert attempts[0]["uuid"] == attempts[1]["uuid"]
    assert "nested-input" in json.dumps(attempts[0])
    end = next(e for e in attempts if e["scope_category"] == "end")
    assert end["metadata"]["outcome"] == outcome
    if outcome == "failed":
        assert end["metadata"]["otel.status_code"] == "ERROR"
    if outcome == "success":
        assert "nested-output" in json.dumps(end)
    child_tools = flush(events, "child-tool")
    assert len(child_tools) == 2
    sessions = [e for e in events if e["name"] == relay_runtime.SESSION_SCOPE and e["scope_category"] == "start"]
    child_session = next(e for e in sessions if e["metadata"].get("hermes.session_id") == "Child-Mixed")
    parent_turn = next(e for e in events if e["name"] == relay_runtime.TURN_SCOPE and e["metadata"].get("hermes.turn_id") == turn.turn_id)
    assert child_session["parent_uuid"] == parent_turn["uuid"]


def test_nested_same_attempt_wrapper_does_not_duplicate_content(capture):
    events, _lease, _turn = capture
    calls = []
    metadata = {"api_request_id": "same-request"}
    def provider(request):
        return relay_llm.execute(request, lambda r: calls.append(r) or {"output": "one"},
                                 name="same-inner", model_name="synthetic", metadata=metadata)
    relay_llm.execute({"input": "one"}, provider, name="same-outer", model_name="synthetic", metadata=metadata)
    relay.subscribers.flush()
    assert len(calls) == 1
    assert len([e for e in events if e["category"] == "llm"]) == 2


@pytest.mark.parametrize("stage", ["start", "end", "logical", "lease"])
def test_passive_lifecycle_failure_never_replays_or_blocks_dispatch(capture, monkeypatch, stage):
    events, lease, _turn = capture
    calls = []
    def broken(*args, **kwargs):
        raise RuntimeError("synthetic telemetry error")
    def tool(args):
        if stage == "logical":
            monkeypatch.setattr(relay.scope, "push", broken)
        elif stage == "lease":
            monkeypatch.setattr(lease.host, "acquire_operation_lease", broken)
        else:
            monkeypatch.setattr(relay.llm, "call" if stage == "start" else "call_end", broken)
        return relay_llm.execute({"input": "hello"}, lambda r: calls.append(r) or "result",
                                 name="passive-fault", model_name="synthetic",
                                 metadata={"api_request_id": "fault-request"})
    result, _ = relay_tools.execute("fault-tool", {}, tool, session_id=lease.session_id)
    assert result == "result"
    assert len(calls) == 1


def test_passive_payload_sanitizers_do_not_rewrite_provider_or_resurrect_null(capture):
    events, lease, _turn = capture
    def sanitize_request(request, context):
        return relay.LLMRequest({}, {"input": "redacted"})
    def sanitize_response(response, context):
        return None
    relay.guardrails.register_llm_sanitize_request("test.request", 0, sanitize_request)
    relay.guardrails.register_llm_sanitize_response("test.response", 0, sanitize_response)
    calls = []
    result = {"output": "RESPONSE_CANARY"}
    def tool(args):
        return relay_llm.execute({"input": "REQUEST_CANARY"}, lambda r: calls.append(r) or result,
                                 name="sanitized-nested", model_name="synthetic")
    try:
        relay_tools.execute("sanitizer-tool", {}, tool, session_id=lease.session_id)
        attempts = flush(events, "sanitized-nested")
        assert len(attempts) == 2
        assert calls == [{"input": "REQUEST_CANARY"}]
        assert "REQUEST_CANARY" not in json.dumps(attempts)
        assert "RESPONSE_CANARY" not in json.dumps(attempts)
        end = next(e for e in attempts if e["scope_category"] == "end")
        assert end["data"] is None
    finally:
        relay.guardrails.deregister_llm_sanitize_request("test.request")
        relay.guardrails.deregister_llm_sanitize_response("test.response")


@pytest.mark.parametrize("outcome", ["success", "failed", "cancelled"])
def test_guarded_stream_retains_observed_partial_without_replay(capture, outcome):
    events, lease, _turn = capture
    calls = []
    observed = []
    error = ValueError("synthetic stream error")

    def factory(request):
        calls.append(request)
        yield {"delta": "partial"}
        if outcome == "failed":
            raise error
        yield {"delta": "tail"}

    def tool(args):
        stream = relay_llm.stream_current(
            {"stream": True}, factory, name="nested-stream", model_name="synthetic",
            finalizer=lambda: {"chunks": list(observed)},
            on_chunk=observed.append,
            metadata={"api_request_id": "Stream-Request"})
        try:
            assert next(stream) == {"delta": "partial"}
            if outcome != "cancelled":
                list(stream)
        finally:
            stream.close()
        return "done"

    if outcome == "failed":
        with pytest.raises(ValueError) as raised:
            relay_tools.execute("stream-tool", {}, tool, session_id=lease.session_id)
        assert raised.value is error
    else:
        relay_tools.execute("stream-tool", {}, tool, session_id=lease.session_id)
    attempts = flush(events, "nested-stream")
    assert len(calls) == 1
    assert len(attempts) == 2
    end = next(e for e in attempts if e["scope_category"] == "end")
    if outcome == "failed":
        assert end["metadata"]["observed_error"]["message"] == str(error)
    assert "partial" in json.dumps(end["data"])
    assert end["metadata"]["outcome"] == outcome
