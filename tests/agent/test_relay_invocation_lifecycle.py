"""Bounded primary attempt correlation and physical cleanup regressions."""
import json
from contextlib import contextmanager
from threading import Thread
from types import SimpleNamespace

import pytest

from agent import relay_invocation as invocation, turn_api_call
from tests.agent.test_relay_passive_capture import capture, flush, relay


@pytest.mark.parametrize('api_mode', ['anthropic_messages', 'chat_completions'])
def test_outer_nonstream_attempt_correlates_in_worker(capture, api_mode):
    from agent.client_lifecycle import ClientLifecycleMixin
    from agent.chat_completion_helpers import _context_thread_target
    events, lease, _ = capture
    failure = ValueError('physical-error')
    calls = []
    policy = []
    def create(**kwargs):
        calls.append(kwargs)
        raise failure
    client = SimpleNamespace(messages=SimpleNamespace(create=create),
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    agent = SimpleNamespace(session_id=lease.session_id, provider='anthropic', model='synthetic',
        api_mode=api_mode, _current_api_request_id='stale-shared-value',
        _disable_streaming=True, _capture_anthropic_response_headers=lambda *a: None,
        base_url='https://example.test', platform='test', _has_pending_redirect=lambda: False)
    def worker_call(request):
        errors = []
        def run():
            try:
                if api_mode == 'anthropic_messages':
                    ClientLifecycleMixin._anthropic_messages_create(agent, request, client=client)
                else:
                    from agent.chat_completion_helpers import _dispatch_nonstreaming_api_request
                    _dispatch_nonstreaming_api_request(agent, request, make_client=lambda *a: client)
            except BaseException as exc:
                errors.append(exc)
        worker = Thread(target=_context_thread_target(run))
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
        if errors:
            raise errors[0]
    agent._interruptible_api_call = worker_call
    def rewrite(name, request, next_call):
        policy.append(request)
        return next_call(relay.LLMRequest(request.headers, {**request.content,
            'instructions': 'invalid', 'messages': [{'role': 'user', 'content': 'authorized'}]}))
    relay.intercepts.register_llm_execution('invocation-lifecycle', 1, rewrite)
    try:
        for retry in (2, 3):
            request = {'model': 'synthetic', 'messages': [{'role': 'user', 'content': 'proposal'}], 'max_tokens': 10}
            with pytest.raises(ValueError) as raised:
                turn_api_call.perform_api_call(agent, api_kwargs=request, _original_api_kwargs=request,
                    _llm_middleware_trace=[], _moa_prepared_request=None, _retry=SimpleNamespace(),
                    thinking_spinner=None, retry_count=retry, api_call_count=1,
                    api_request_id='actual-request', effective_task_id='task', turn_id='turn', interrupted=False)
            assert raised.value is failure
    finally:
        relay.intercepts.deregister_llm_execution('invocation-lifecycle')
    assert len(policy) == len(calls) == 2
    assert all(('instructions' not in request) if api_mode == 'anthropic_messages' else True for request in calls)
    operation = 'anthropic.messages' if api_mode == 'anthropic_messages' else 'openai.chat_completions'
    starts = [e for e in flush(events, operation) if e['scope_category'] == 'start']
    managed = [e for e in starts if e['metadata'].get('hermes.llm.phase') == 'managed_attempt']
    physical = [e for e in starts if e['metadata'].get('hermes.llm.phase') == 'authorized_invocation']
    assert len(managed) == len(physical) == 2
    assert len({e['metadata']['hermes.llm.attempt_id'] for e in managed}) == 2
    for outer, inner in zip(managed, physical):
        assert inner['metadata']['hermes.llm.attempt_id'] == outer['metadata']['hermes.llm.attempt_id']
        assert inner['metadata']['api_request_id'] == 'actual-request'
        assert inner['metadata']['retry_count'] == outer['metadata']['retry_count']
        assert 'authorized' in json.dumps(inner['data'])
        if api_mode == 'anthropic_messages':
            assert 'instructions' not in json.dumps(inner['data'])
        assert inner['data']['content'] == {k: v for k, v in calls[physical.index(inner)].items()
                                          if k not in {'timeout', 'extra_headers'}}


@pytest.mark.parametrize('tools', [None, [{'type': 'function', 'name': 'native_tool',
    'parameters': {'type': 'object'}, 'strict': True}]])
def test_physical_capture_preserves_native_responses_request(capture, tools):
    events, lease, _ = capture
    request = {'model': 'synthetic', 'input': 'hello', 'tools': tools,
               'timeout': 10, 'extra_headers': {'x-test': 'header-only'}}
    sent = []
    invocation.execute(request, lambda body: sent.append(body) or {'output': []},
        metadata={'api_mode': 'codex_responses'}, name='openai',
        model_name='synthetic', session_id=lease.session_id)
    starts = [e for e in flush(events, 'openai.responses') if e['scope_category'] == 'start']
    assert len(starts) == 1
    assert starts[0]['data']['content'] == {k: v for k, v in sent[0].items()
                                          if k not in {'timeout', 'extra_headers'}}
    assert sent[0] is request


class Accumulator:
    def __init__(self):
        self.chunks = []
    def collect(self, chunk):
        self.chunks.append(chunk)
    def finalize(self):
        return {'chunks': list(self.chunks)}


class Raw:
    def __init__(self, error=None, close_error=None, iter_error=None):
        self.error, self.close_error, self.iter_error = error, close_error, iter_error
        self.closed = 0
        self.sent = False
    def __iter__(self):
        if self.iter_error:
            raise self.iter_error
        return self
    def __next__(self):
        if not self.sent:
            self.sent = True
            return {'partial': 'consumer-visible'}
        if self.error:
            raise self.error
        raise StopIteration
    def close(self):
        self.closed += 1
        if self.close_error:
            raise self.close_error


@pytest.mark.parametrize('error', [ValueError('provider'), InterruptedError('cancel')])
@pytest.mark.parametrize('cleanup', ['raw', 'capture'])
def test_original_error_survives_cleanup(error, cleanup):
    record = {}
    exits = []
    class Context:
        def __exit__(self, *args):
            exits.append(dict(record))
            if cleanup == 'capture':
                raise RuntimeError('observation exit')
    raw = Raw(error=error, close_error=RuntimeError('close') if cleanup == 'raw' else None)
    stream = invocation._PhysicalStream(raw, Context(), record, Accumulator())
    next(stream)
    with pytest.raises(type(error)) as raised:
        next(stream)
    assert raised.value is error
    assert raw.closed == len(exits) == 1
    assert record['error'] is error
    assert record['response']['chunks'] == [{'partial': 'consumer-visible'}]
    stream.close()
    assert raw.closed == 1


def test_consumer_failure_captures_partial_and_preserves_error():
    record = {}
    class Context:
        def __exit__(self, *args):
            pass
    raw = Raw(close_error=RuntimeError('close'))
    stream = invocation._PhysicalStream(raw, Context(), record, Accumulator())
    failure = TimeoutError('consumer deadline')
    with pytest.raises(TimeoutError) as raised:
        try:
            next(stream)
            raise failure
        finally:
            stream.close()
    assert raised.value is failure
    assert record['error'] is failure
    assert record['outcome'] == 'failed'
    assert record['response']['chunks'] == [{'partial': 'consumer-visible'}]
    assert raw.closed == 1


def test_consumer_close_with_raising_provider_close_stays_cancelled():
    """A clean consumer-initiated close is a cancellation even when the provider's own
    close() raises during teardown; the teardown error is recorded and re-raised, but it
    must not relabel the invocation as failed."""
    record = {}
    class Context:
        def __exit__(self, *args):
            pass
    teardown = ValueError('wire failed on close')
    raw = Raw(close_error=teardown)
    stream = invocation._PhysicalStream(raw, Context(), record, Accumulator())
    next(stream)
    with pytest.raises(ValueError) as raised:
        stream.close()
    assert raised.value is teardown
    assert record['outcome'] == 'cancelled'
    assert record['error'] is teardown
    assert record['response']['chunks'] == [{'partial': 'consumer-visible'}]
    assert raw.closed == 1


def test_natural_exhaustion_with_raising_provider_close_is_failed():
    """Natural EOF followed by a raising provider close(): the teardown failure is the first
    provider error for this call, so the invocation is failed (not success) and the error
    surfaces to the consumer."""
    record = {}
    class Context:
        def __exit__(self, *args):
            pass
    teardown = ValueError('wire failed on close')
    raw = Raw(close_error=teardown)
    stream = invocation._PhysicalStream(raw, Context(), record, Accumulator())
    next(stream)
    with pytest.raises(ValueError) as raised:
        next(stream)  # provider exhausted -> close(outcome='success') -> teardown raises
    assert raised.value is teardown
    assert record['outcome'] == 'failed'
    assert record['error'] is teardown
    assert raw.closed == 1


def test_managed_consumer_failure_does_not_relabel_completed_invocation(capture):
    events, lease, _ = capture
    failure = TimeoutError('managed consumer deadline')
    metadata = invocation.attempt_metadata({'api_mode': 'codex_responses', 'api_request_id': 'consumer'})
    identity = dict(metadata=metadata, name='openai', model_name='synthetic', session_id=lease.session_id)
    def factory(request):
        def chunks():
            yield {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'delta': 'consumer-partial'}
            yield {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'delta': 'not-consumed'}
        return chunks()
    stream = invocation.ManagedStream({'model': 'synthetic', 'input': 'hello'},
        lambda request: invocation.stream(request, factory, **identity),
        **identity, finalizer=lambda: None, defer_logical_completion=True)
    with pytest.raises(TimeoutError) as raised:
        try:
            list(stream)
            raise failure
        finally:
            stream.close()
    assert raised.value is failure
    ends = [e for e in flush(events, 'openai.responses') if e['scope_category'] == 'end'
            and e['metadata'].get('hermes.llm.phase') == 'authorized_invocation']
    assert len(ends) == 1
    assert 'consumer-partial' in json.dumps(ends[0]['data'])
    # Explicitly exhaust the native pipeline before failing the consumer. A
    # completed physical invocation must not be relabeled by later processing.
    assert 'not-consumed' in json.dumps(ends[0]['data'])
    assert ends[0]['metadata']['outcome'] == 'success'
    assert 'observed_error' not in ends[0]['metadata']


def test_iterator_setup_failure_closes_immediately(monkeypatch):
    error = ValueError('iter setup')
    raw = Raw(iter_error=error, close_error=RuntimeError('close'))
    exits = []
    @contextmanager
    def capture_context(*a, **kw):
        try:
            yield {}
        finally:
            exits.append(True)
    monkeypatch.setattr(invocation, 'capture', capture_context)
    monkeypatch.setattr(invocation.relay_runtime, 'get_runtime', lambda **kw: SimpleNamespace(managed_execution_enabled=lambda: True))
    with pytest.raises(ValueError) as raised:
        invocation.stream({}, lambda request: raw, metadata={'api_mode': 'codex_responses'})
    assert raised.value is error
    assert raw.closed == len(exits) == 1


@pytest.mark.parametrize('stage', ['enter', 'exit'])
def test_provider_manager_failure_closes_capture_immediately(monkeypatch, stage):
    failure = ValueError(stage)
    record = {}
    exits = []
    @contextmanager
    def capture_context(*a, **kw):
        try:
            yield record
        finally:
            exits.append(True)
    class Manager:
        def __enter__(self):
            if stage == 'enter':
                raise failure
            return iter([{'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'delta': 'partial'}])
        def __exit__(self, *args):
            raise failure
    def factory(request):
        with Manager() as stream:
            yield from stream
    monkeypatch.setattr(invocation, 'capture', capture_context)
    monkeypatch.setattr(invocation.relay_runtime, 'get_runtime', lambda **kw: SimpleNamespace(managed_execution_enabled=lambda: True))
    stream = invocation.stream({}, factory, metadata={'api_mode': 'codex_responses'})
    with pytest.raises(ValueError) as raised:
        list(stream)
    assert raised.value is failure
    assert exits == [True]
    assert record['error'] is failure
    if stage == 'exit':
        assert 'partial' in json.dumps(record['response'])
