"""Native regressions for observed errors and passive async teardown."""
import asyncio
import json

import pytest

from tests.agent.test_relay_passive_capture import capture, flush, relay
from agent import relay_llm, relay_runtime, relay_tools


@pytest.mark.parametrize('lane', ['tool', 'llm'])
def test_observed_error_is_event_sanitized(capture, lane):
    import nemo_relay._native as native
    assert native.__file__.endswith('.so')
    events, lease, _ = capture
    error = ConnectionError('transport ERROR_PRIVATE_CANARY')
    def sanitize(event, fields):
        metadata = fields.get('metadata', {})
        if 'observed_error' in metadata:
            metadata['observed_error']['message'] = 'redacted transport'
        return fields
    relay.guardrails.register_scope_sanitize_end('error-redaction', 0, sanitize)
    def fail(_):
        raise error
    try:
        with pytest.raises(ConnectionError) as raised:
            if lane == 'tool':
                relay_tools.invoke_authorized('error-tool', {}, fail, session_id=lease.session_id)
            else:
                with relay_runtime.managed_callback_guard():
                    relay_llm.execute({}, fail, name='error-llm', model_name='synthetic')
        assert raised.value is error
        rows = flush(events, 'error-' + lane)
        assert len(rows) == 2
        end = next(row for row in rows if row['scope_category'] == 'end')
        assert end['metadata']['outcome'] == 'failed'
        assert end['metadata']['observed_error'] == {
            'type': 'ConnectionError', 'message': 'redacted transport',
            'classification': 'unavailable'}
        assert 'ERROR_PRIVATE_CANARY' not in json.dumps(rows)
        assert str(error) == 'transport ERROR_PRIVATE_CANARY'
    finally:
        relay.guardrails.deregister_scope_sanitize_end('error-redaction')


@pytest.mark.parametrize('stage', ['collector', 'provider', 'factory', 'cleanup'])
@pytest.mark.parametrize('cancel', [False, True])
def test_async_passive_failure_closes_immediately(capture, stage, cancel):
    events, lease, _ = capture
    error = asyncio.CancelledError('observed cancellation') if cancel else ValueError('observed failure')
    secondary = RuntimeError('secondary close failure')
    seen = []
    class Resource:
        count = 0
        closes = 0
        def __aiter__(self):
            return self
        async def __anext__(self):
            self.count += 1
            if self.count == 1:
                return {'delta': 'partial'}
            if stage == 'provider':
                raise error
            if self.count == 2 and stage == 'collector':
                return {'delta': 'tail'}
            raise StopAsyncIteration
        async def aclose(self):
            self.closes += 1
            raise error if stage == 'cleanup' else secondary
    resource = Resource()
    async def factory(_):
        if stage == 'factory':
            raise error
        return resource
    def collect(chunk):
        if seen and stage == 'collector':
            raise error
        seen.append(chunk)
    async def run():
        baseline = lease.host._active_operations
        with relay_runtime.managed_callback_guard():
            with pytest.raises(type(error)) as raised:
                stream = await relay_llm.stream_current_async({}, factory,
                    name='async-error', model_name='synthetic', on_chunk=collect,
                    finalizer=lambda: {'chunks': seen})
                assert await anext(stream) == {'delta': 'partial'}
                await anext(stream)
            assert raised.value is error
            assert lease.host._active_operations == baseline
            if stage != 'factory':
                assert resource.closes == 1
                await stream.aclose()
                assert resource.closes == 1
        await relay.subscribers.flush_async()
    asyncio.run(run())
    rows = flush(events, 'async-error')
    assert len(rows) == 2
    end = next(row for row in rows if row['scope_category'] == 'end')
    assert end['metadata']['outcome'] == ('cancelled' if cancel else 'failed')
    assert end['metadata']['observed_error']['type'] == type(error).__name__
    assert end['metadata']['observed_error']['message'] == str(error)
    if stage != 'factory':
        assert 'partial' in json.dumps(end['data'])
