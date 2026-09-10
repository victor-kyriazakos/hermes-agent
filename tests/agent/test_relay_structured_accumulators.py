"""Capture stays structured and independent of Hermes display normalization."""
from agent.chat_completion_helpers_relay import RelayChatAccumulator
from agent.relay_llm import AnthropicStreamAccumulator


def test_responses_primary_capture_keeps_partial_native_items(monkeypatch):
    from types import SimpleNamespace
    from agent import codex_runtime, relay_invocation
    partial = {"type": "response.output_item.added", "output_index": 0,
               "item": {"id": "item", "type": "function_call", "name": "tool", "arguments": ""}}
    delta = {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"partial":'}
    def intercept(request, factory, **options):
        options["on_chunk"](partial)
        options["on_chunk"](delta)
        captured = options["finalizer"]()
        assert captured["output"][0]["arguments"] == '{"partial":'
        raise InterruptedError("stop after capture assertion")
    monkeypatch.setattr(relay_invocation, "ManagedStream", intercept)
    agent = SimpleNamespace(_interrupt_requested=False, _current_api_request_id="request",
                            _fallback_index=0, is_subagent=False, session_id="", provider="test")
    import pytest
    with pytest.raises(InterruptedError):
        codex_runtime.run_codex_stream(agent, {"model": "model"}, client=object())


def test_chat_capture_retains_all_choices_and_refusal():
    acc = RelayChatAccumulator()
    acc.observe({"id": "chat-id", "choices": [
        {"index": 0, "delta": {"content": "one", "refusal": "refused"}},
        {"index": 1, "delta": {"content": "two", "audio": {"id": "audio-id"}}},
    ]})
    result = acc.finalize()
    assert result["id"] == "chat-id"
    assert len(result["choices"]) == 2
    assert result["choices"][0]["message"]["refusal"] == "refused"
    assert result["choices"][1]["message"]["audio"]["id"] == "audio-id"


def test_anthropic_capture_retains_initial_message_extensions():
    acc = AnthropicStreamAccumulator()
    acc.observe({"type": "message_start", "message": {
        "id": "msg-id", "role": "assistant", "vendor": {"retained": True},
        "content": [{"type": "text", "text": "initial"}],
    }})
    result = acc.finalize()
    assert result["vendor"] == {"retained": True}
    assert result["content"][0]["text"] == "initial"
