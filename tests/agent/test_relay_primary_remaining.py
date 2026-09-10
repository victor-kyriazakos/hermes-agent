"""Native primary boundary and outer retry regressions (synthetic provider)."""
import json
from types import SimpleNamespace
import pytest
from tests.agent.test_relay_passive_capture import capture, flush as all_events, relay


def flush(events, name):
    # Physical invocation assertions must not mistake the policy proposal for wire data.
    return [e for e in all_events(events, name)
            if e['metadata'].get('hermes.llm.phase') != 'managed_attempt']
from agent import codex_runtime, turn_api_call


@pytest.mark.parametrize('streaming', [False, True])
def test_anthropic_final_boundary(capture, monkeypatch, streaming):
    from agent import chat_completion_helpers as chat
    from agent.client_lifecycle import ClientLifecycleMixin
    events, lease, _ = capture
    calls = []
    failure = ValueError('anthropic-physical-error')
    def create(**kwargs):
        calls.append(kwargs)
        raise failure
    def rewrite(*args):
        request = args[-2]
        return args[-1](relay.LLMRequest(request.headers, {**request.content,
            'messages': [{'role': 'user', 'content': 'final-anthropic'}],
            'instructions': 'strip-me'}))
    agent = SimpleNamespace(session_id=lease.session_id, provider='anthropic', model='synthetic',
        api_mode='anthropic_messages', _current_api_request_id='anthropic-final',
        _disable_streaming=True, _capture_anthropic_response_headers=lambda *a: None)
    request = {'model': 'synthetic', 'messages': [{'role': 'user', 'content': 'proposal'}], 'max_tokens': 10}
    client = SimpleNamespace(messages=SimpleNamespace(create=create, stream=create))
    register = relay.intercepts.register_llm_stream_execution if streaming else relay.intercepts.register_llm_execution
    deregister = relay.intercepts.deregister_llm_stream_execution if streaming else relay.intercepts.deregister_llm_execution
    register('anthropic-final', 1, rewrite)
    try:
        with pytest.raises(ValueError) as raised:
            if streaming:
                call = object.__new__(chat._StreamingCall)
                call.agent, call.api_kwargs, call.last_chunk_time = agent, request, {}
                monkeypatch.setattr(call, '_new_diag', dict)
                call.managed_stream_holder = {}
                call._call_anthropic(client)
            else:
                ClientLifecycleMixin._anthropic_messages_create(agent, request, client=client)
        assert raised.value is failure
    finally:
        deregister('anthropic-final')
    assert len(calls) == 1
    assert 'instructions' not in calls[0]
    assert calls[0]['messages'][0]['content'] == 'final-anthropic'
    starts = [e for e in flush(events, 'anthropic.messages') if e['scope_category'] == 'start']
    assert len(starts) == 1
    assert 'final-anthropic' in json.dumps(starts[0]['data'])
    assert 'instructions' not in json.dumps(starts[0]['data'])


def test_nonstream_codex_wrapper_captures_physical_request_and_retries(capture):
    events, lease, _ = capture
    calls = []
    failure = ValueError('physical-stop')
    def create(**kwargs):
        calls.append(kwargs)
        raise failure
    agent = SimpleNamespace(
        _disable_streaming=True, api_mode='codex_responses', provider='openai-codex',
        base_url='https://example.test', model='synthetic', session_id=lease.session_id,
        platform='test', _interrupt_requested=False, _current_api_request_id='outer-retry',
        _get_transport=lambda: SimpleNamespace(preflight_kwargs=lambda kwargs, **_: kwargs),
        _is_copilot_url=lambda: False, _is_codex_backend=lambda: True,
        _has_pending_redirect=lambda: False,
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    # Exercise the actual context-copying helper used by request workers.
    from threading import Thread
    from agent.chat_completion_helpers import _context_thread_target
    def worker_call(kwargs):
        errors = []
        def run():
            try:
                codex_runtime.run_codex_stream(agent, kwargs, client=client)
            except BaseException as exc:
                errors.append(exc)
        worker = Thread(target=_context_thread_target(run))
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        if errors:
            raise errors[0]
    agent._interruptible_api_call = worker_call
    request = {'model': 'synthetic', 'input': [{'role': 'user', 'content': 'physical-canary'}],
               'prompt_cache_retention': '24h'}
    with pytest.raises(ValueError) as raised:
        turn_api_call.perform_api_call(agent, api_kwargs=request, _original_api_kwargs=request,
            _llm_middleware_trace=[], _moa_prepared_request=None, _retry=SimpleNamespace(),
            thinking_spinner=None, retry_count=3, api_call_count=1, api_request_id='outer-retry',
            effective_task_id='task', turn_id='turn', interrupted=False)
    assert raised.value is failure
    assert turn_api_call.PRIMARY_RETRY_COUNT.get() == 0
    assert request['prompt_cache_retention'] == '24h'
    assert 'stream' not in request
    assert len(calls) == 1
    starts = [e for e in flush(events, 'openai.responses') if e['scope_category'] == 'start']
    assert len(starts) == 1
    assert 'physical-canary' in json.dumps(starts[0]['data'])
    assert starts[0]['data']['content']['stream'] is True
    assert 'prompt_cache_retention' not in json.dumps(starts[0]['data'])
    assert starts[0]['metadata']['retry_count'] == 3
    assert starts[0]['metadata']['reconnect_ordinal'] == 0


def test_chat_native_metadata_keeps_outer_retry_and_reconnect(capture, monkeypatch):
    from agent import chat_completion_helpers as chat
    events, lease, _ = capture
    call = object.__new__(chat._StreamingCall)
    call.agent = SimpleNamespace(base_url='https://example.test/v1', session_id=lease.session_id,
        provider='test', model='synthetic', _current_api_request_id='chat-retry')
    call.api_kwargs = {'model': 'synthetic', 'messages': [{'role': 'user', 'content': 'chat-canary'}]}
    monkeypatch.setattr(call, '_stream_timeouts', lambda: (30, 30, 10))
    monkeypatch.setattr(call, '_new_diag', dict)
    monkeypatch.setattr(call, '_set_managed_stream', lambda value: value)
    def fail(request):
        assert request['stream'] is True
        raise ValueError('physical-chat')
    monkeypatch.setattr(call, '_open_chat_stream', fail)
    token = turn_api_call.PRIMARY_RETRY_COUNT.set(4)
    try:
        for ordinal in (0, 1):
            call.reconnect_ordinal = ordinal
            with pytest.raises(ValueError, match='physical-chat'):
                call._call_chat_completions(ordinal + 1)
    finally:
        turn_api_call.PRIMARY_RETRY_COUNT.reset(token)
    starts = [e for e in flush(events, 'openai.chat_completions') if e['scope_category'] == 'start']
    assert len(starts) == 2
    assert all('chat-canary' in json.dumps(e['data']) for e in starts)
    assert [e['metadata']['retry_count'] for e in starts] == [4, 4]
    assert [e['metadata']['reconnect_ordinal'] for e in starts] == [0, 1]


def test_codex_reconnect_is_not_outer_retry(capture):
    events, lease, _ = capture
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        raise ConnectionError('connect-canary')
    agent = SimpleNamespace(_interrupt_requested=False, session_id=lease.session_id,
        provider='openai-codex', _current_api_request_id='reconnect',
        _client_log_context=lambda: '', model='synthetic')
    with pytest.raises(ConnectionError):
        codex_runtime.run_codex_stream(agent, {'model': 'synthetic', 'input': 'retry-canary'},
            client=SimpleNamespace(responses=SimpleNamespace(create=create)))
    starts = [e for e in flush(events, 'openai.responses') if e['scope_category'] == 'start']
    assert len(calls) == len(starts) == 2
    assert [e['metadata']['retry_count'] for e in starts] == [0, 0]
    assert [e['metadata']['reconnect_ordinal'] for e in starts] == [0, 1]
    ends = [e for e in flush(events, 'openai.responses') if e['scope_category'] == 'end']
    assert {e['uuid'] for e in starts} == {e['uuid'] for e in ends}
    assert len({e['uuid'] for e in starts}) == 2
    assert len({e['parent_uuid'] for e in starts}) == 1


def test_codex_partial_capture_is_not_a_successful_provider_response(capture):
    events, lease, _ = capture
    agent = SimpleNamespace(_interrupt_requested=False, session_id=lease.session_id,
        provider='openai-codex', _current_api_request_id='missing-terminal', model='synthetic',
        _touch_activity=lambda *args: None, _fire_stream_delta=lambda *args: None)
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: iter([])))
    with pytest.raises(RuntimeError, match='terminal'):
        codex_runtime.run_codex_stream(agent, {'model': 'synthetic', 'input': 'hello'}, client=client)
    ends = [e for e in flush(events, 'openai.responses') if e['scope_category'] == 'end']
    assert len(ends) == 1
    assert 'hello' not in json.dumps(ends[0]['data'])


def test_codex_authorized_request_capture_matches_post_intercept_safety_gate(capture):
    events, lease, _ = capture
    calls = []
    def rewrite(request, next_call):
        return next_call(relay.LLMRequest(request.headers,
            {**request.content, 'input': 'authorized-final-canary', 'prompt_cache_retention': '24h'}))
    def create(**kwargs):
        calls.append(kwargs)
        raise ValueError('post-intercept-stop')
    relay.intercepts.register_llm_stream_execution('primary-final-preparation', 1, rewrite)
    try:
        agent = SimpleNamespace(_interrupt_requested=False, session_id=lease.session_id,
            provider='openai-codex', _current_api_request_id='post-intercept',
            model='synthetic', _is_codex_backend=lambda: True)
        with pytest.raises(ValueError, match='post-intercept-stop'):
            codex_runtime.run_codex_stream(agent, {'model': 'synthetic', 'input': 'authorized-canary'},
                client=SimpleNamespace(responses=SimpleNamespace(create=create)))
    finally:
        relay.intercepts.deregister_llm_stream_execution('primary-final-preparation')
    assert len(calls) == 1
    assert 'prompt_cache_retention' not in calls[0]
    assert calls[0]['input'] == 'authorized-final-canary'
    starts = [e for e in flush(events, 'openai.responses') if e['scope_category'] == 'start']
    assert len(starts) == 1
    assert 'authorized-final-canary' in json.dumps(starts[0]['data'])
    assert 'prompt_cache_retention' not in json.dumps(starts[0]['data'])


def test_codex_reconnect_partial_payloads_do_not_bleed(capture):
    events, lease, _ = capture
    calls = []
    failures = [ConnectionError('first-stream'), ConnectionError('second-stream')]
    def create(**kwargs):
        ordinal = len(calls)
        calls.append(kwargs)
        def stream():
            yield {'type': 'response.output_text.delta', 'output_index': 0,
                   'content_index': 0, 'delta': f'partial-attempt-{ordinal}'}
            raise failures[ordinal]
        return stream()
    agent = SimpleNamespace(_interrupt_requested=False, session_id=lease.session_id,
        provider='openai-codex', _current_api_request_id='partial-reconnect',
        _client_log_context=lambda: '', model='synthetic',
        _touch_activity=lambda *args: None, _fire_stream_delta=lambda *args: None)
    token = turn_api_call.PRIMARY_RETRY_COUNT.set(7)
    try:
        with pytest.raises(ConnectionError) as raised:
            codex_runtime.run_codex_stream(agent, {'model': 'synthetic', 'input': 'history-canary'},
                client=SimpleNamespace(responses=SimpleNamespace(create=create)))
    finally:
        turn_api_call.PRIMARY_RETRY_COUNT.reset(token)
    assert raised.value is failures[1]
    ends = [e for e in flush(events, 'openai.responses') if e['scope_category'] == 'end']
    assert len(calls) == len(ends) == 2
    for ordinal, end in enumerate(ends):
        assert end['metadata']['retry_count'] == 7
        assert end['metadata']['reconnect_ordinal'] == ordinal
        assert f'partial-attempt-{ordinal}' in json.dumps(end['data'])
        assert f'partial-attempt-{1 - ordinal}' not in json.dumps(end['data'])
