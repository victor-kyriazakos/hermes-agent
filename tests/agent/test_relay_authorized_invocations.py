"""Real native lifecycle proof at the final Hermes authorization boundary."""
import json
from types import SimpleNamespace

import pytest

from tests.agent.test_relay_passive_capture import capture, flush, relay
from agent import tool_executor


@pytest.mark.parametrize('outcome', ['success', 'denied', 'failed', 'cancelled', 'repeat', 'pre_cancel', 'short_circuit'])
def test_authorized_invocation_is_not_the_policy_attempt(capture, monkeypatch, outcome):
    events, lease, _ = capture
    import hermes_cli.middleware as middleware
    import nemo_relay._native as native
    assert native.__file__.endswith('.so'), native.__file__
    print('native binding:', native.__file__)
    calls = []
    failure = InterruptedError('cancel-canary') if outcome in ('cancelled', 'pre_cancel') else ValueError('failure-canary')
    agent = SimpleNamespace(session_id=lease.session_id, _current_turn_id='turn',
        _current_api_request_id='logical', platform='test',
        _tool_guardrails=SimpleNamespace(before_call=lambda *a: SimpleNamespace(allows_execution=True)))
    monkeypatch.setattr(middleware, 'apply_tool_request_middleware',
        lambda name, args, **kw: SimpleNamespace(payload={**args, 'value': 'request-middleware'}, trace=[]))
    def execution(name, args, callback, **kw):
        if outcome == 'pre_cancel':
            raise failure
        if outcome == 'short_circuit':
            return 'cached'
        result = callback({**args, 'value': 'execution-middleware'})
        if outcome == 'repeat':
            with pytest.raises(RuntimeError, match='more than once'):
                callback(args)
        return result
    monkeypatch.setattr(middleware, 'run_tool_execution_middleware', execution)
    monkeypatch.setattr(tool_executor, '_pre_tool_block',
        lambda *a: ('denied' if outcome == 'denied' else None, {'value': 'authorized-final'}))
    monkeypatch.setattr(tool_executor, '_begin_tool_execution', lambda *a: None)
    monkeypatch.setattr(tool_executor, '_run_with_activity_heartbeat', lambda agent, name, fn: fn())
    monkeypatch.setattr(tool_executor, '_blocked_tool_result', lambda *a, **kw: 'blocked')
    def dispatch(args):
        calls.append(dict(args))
        if outcome in ('failed', 'cancelled'):
            raise failure
        return {'output': ['observed-result-canary']}
    def run():
        return tool_executor._run_agent_tool_execution_middleware(agent,
            function_name='capture-probe-tool', function_args={'value': 'proposed'},
            effective_task_id='task', tool_call_id='call', execute=dispatch)
    if outcome in ('failed', 'cancelled', 'pre_cancel'):
        with pytest.raises(type(failure)) as raised:
            run()
        assert raised.value is failure
    else:
        run()
    rows = flush(events, 'capture-probe-tool')
    attempts = [e for e in rows if e['metadata'].get('hermes.tool.phase') == 'managed_attempt']
    invocations = [e for e in rows if e['metadata'].get('hermes.tool.phase') == 'authorized_invocation']
    assert len(attempts) == 2
    assert 'proposed' in json.dumps(attempts[0]['data'])
    if outcome in ('denied', 'pre_cancel', 'short_circuit'):
        assert not calls
        assert not invocations
        return
    assert calls == [{'value': 'authorized-final'}]
    assert len(invocations) == 2
    start, end = invocations
    assert start['uuid'] == end['uuid'] != attempts[0]['uuid']
    assert start['metadata']['hermes.tool.attempt_id'] == attempts[0]['metadata']['hermes.tool.attempt_id']
    assert 'authorized-final' in json.dumps(start['data'])
    assert end['metadata']['outcome'] == (outcome if outcome in ('failed', 'cancelled') else 'success')
    if outcome in ('failed', 'cancelled'):
        assert end['metadata']['observed_error']['type'] == type(failure).__name__
        assert end['metadata']['observed_error']['message'] == str(failure)
    if outcome in ('success', 'repeat'):
        assert 'observed-result-canary' in json.dumps(end['data'])


def test_retries_nested_correlation_and_sanitizer_no_replay(capture):
    from agent import relay_tools
    events, lease, _ = capture
    observed = []
    relay.guardrails.register_tool_sanitize_request('invocation.request', 0,
        lambda args, context: {'value': 'redacted'})
    relay.guardrails.register_tool_sanitize_response('invocation.response', 0,
        lambda response, context: None)
    def child(args):
        observed.append(args)
        return 'OUTPUT_SECRET_CANARY'
    def parent(args):
        return relay_tools.execute('nested', args,
            lambda final: relay_tools.invoke_authorized('nested', final, child,
                session_id=lease.session_id), session_id=lease.session_id)[0]
    try:
        for _ in range(2):
            relay_tools.execute('retry', {'value': 'INPUT_SECRET_CANARY'},
                lambda final: relay_tools.invoke_authorized('retry', final, parent,
                    session_id=lease.session_id), session_id=lease.session_id)
        relay.subscribers.flush()
        rows = [e for e in events if e['name'] in ('retry', 'nested')]
        assert len(observed) == 2
        assert all(a == {'value': 'INPUT_SECRET_CANARY'} for a in observed)
        assert 'INPUT_SECRET_CANARY' not in json.dumps(rows)
        assert 'OUTPUT_SECRET_CANARY' not in json.dumps(rows)
        starts = [e for e in rows if e['scope_category'] == 'start'
                  and e['metadata'].get('hermes.tool.phase') == 'authorized_invocation']
        assert len(starts) == 4
        assert len({e['uuid'] for e in starts}) == 4
        parents = [e for e in starts if e['name'] == 'retry']
        children = [e for e in starts if e['name'] == 'nested']
        assert len({e['metadata']['hermes.tool.attempt_id'] for e in parents}) == 2
        assert {e['metadata']['hermes.tool.parent_invocation_id'] for e in children} == {
            e['metadata']['hermes.tool.invocation_id'] for e in parents}
        assert relay_tools._ATTEMPT.get() is None
        assert relay_tools._INVOCATION.get() is None
    finally:
        relay.guardrails.deregister_tool_sanitize_request('invocation.request')
        relay.guardrails.deregister_tool_sanitize_response('invocation.response')
