"""Observe the authorized, prepared provider boundary without replaying policy."""
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Mapping, Any
import sys
from uuid import uuid4

_CURRENT_ATTEMPT: ContextVar[Mapping[str, Any] | None] = ContextVar('relay_invocation_attempt', default=None)

from agent import relay_llm, relay_runtime
from agent.relay_passive import capture_llm


def attempt_metadata(metadata):
    return {**metadata, 'hermes.llm.phase': 'managed_attempt',
            'hermes.llm.attempt_id': str(uuid4())}


def current_attempt_metadata(metadata):
    """Use the outer worker-context identity, never mutable agent retry fields."""
    current = _CURRENT_ATTEMPT.get()
    return {**metadata, **current} if current is not None else attempt_metadata(metadata)


@contextmanager
def capture(request, *, metadata, name, model_name, session_id=None):
    # Construct explicitly: the outer managed callback's duplicate-capture guard
    # intentionally suppresses resolve(), but this is a distinct physical scope.
    session_id = session_id or relay_llm._current_session_id()
    if not session_id:
        yield {}
        return
    runtime, session, parent = relay_runtime.resolve_capture_context(session_id)
    if runtime is None or session is None or not runtime.managed_execution_enabled():
        yield {}
        return
    metadata = {**metadata, 'hermes.llm.phase': 'authorized_invocation',
                'hermes.llm.invocation_id': str(uuid4())}
    attempt = relay_llm._ManagedAttempt(runtime, session, parent, request, metadata,
                                       name=name, model_name=model_name)
    # This is observation of the final native request, not a codec input.
    # Preserve provider tool shapes (including explicit null) while keeping SDK
    # controls out of content and headers in the sanitizer-aware channel.
    body = relay_llm._jsonable_dict(request)
    body.pop('timeout', None)
    body.pop('extra_headers', None)
    attempt.relay_request = runtime.relay.LLMRequest(
        dict(request.get('extra_headers') or {}), body)
    with capture_llm(attempt, True) as record:
        yield record


def execute(request, callback, **identity):
    with capture(request, **identity) as record:
        result = callback(request)
        record['response'] = relay_llm._jsonable(result)
        return result


class _PhysicalStream:
    def __init__(self, raw, context, record, accumulator):
        self.raw, self.context, self.record = raw, context, record
        self.accumulator = accumulator
        self.iterator = iter(raw)
        self.closed = False

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self.iterator)
        except StopIteration:
            self.close(outcome='success')
            raise
        except BaseException as exc:
            self.record['error'] = exc
            self.close(outcome='cancelled' if relay_llm._is_cancellation(exc) else 'failed')
            raise
        relay_runtime._warn_on_error('invocation chunk capture', self.accumulator.collect,
                                     relay_llm._jsonable(chunk))
        return chunk

    def close(self, *, outcome='cancelled'):
        if self.closed:
            return
        self.closed = True
        # A consumer may close us from its finally rather than throwing through
        # __next__. Preserve that failure and the chunks already delivered.
        error = self.record.get('error') or sys.exc_info()[1]
        if isinstance(error, StopIteration):
            error = None
        close_error = None
        try:
            close = getattr(self.raw, 'close', None)
            if callable(close):
                close()
        except BaseException as exc:
            close_error = exc
        if error is not None:
            self.record['error'] = error
            outcome = 'cancelled' if relay_llm._is_cancellation(error) else 'failed'
        elif close_error is not None:
            # Teardown of a cleanly closed stream failed. Record the teardown error but
            # keep the consumer's outcome (cancelled): the provider call itself did not fail.
            self.record['error'] = close_error
        self.record['outcome'] = outcome
        self.record['response'] = relay_llm._jsonable(relay_runtime._warn_on_error(
            'invocation final capture', self.accumulator.finalize))
        relay_runtime._warn_on_error('invocation capture exit', self.context.__exit__, None, None, None)
        # Surface a teardown failure only when the consumer is not already unwinding
        # its own error (that one is re-raised by the consumer and must not be masked).
        if close_error is not None and error is None:
            raise close_error


def stream(request, factory, **identity):
    runtime = relay_runtime.get_runtime(create=False)
    if runtime is None or not runtime.managed_execution_enabled():
        return factory(request)
    from nemo_relay.streaming import ResponsesAccumulator, AnthropicAccumulator
    accumulator = (ResponsesAccumulator() if identity['metadata']['api_mode'] == 'codex_responses'
                   else AnthropicAccumulator())
    context = capture(request, **identity)
    record = context.__enter__()
    raw = None
    try:
        raw = factory(request)
        if hasattr(raw, 'output') and not hasattr(raw, '__iter__'):
            record['response'] = relay_llm._jsonable(raw)
            relay_runtime._warn_on_error('invocation capture exit', context.__exit__, None, None, None)
            return raw
        return _PhysicalStream(raw, context, record, accumulator)
    except BaseException as exc:
        if raw is not None:
            relay_runtime._warn_on_error('invocation setup close', getattr(raw, 'close', lambda: None))
        relay_runtime._warn_on_error('invocation capture exit', context.__exit__, type(exc), exc, exc.__traceback__)
        raise


class ManagedStream(relay_llm.ManagedLlmStream):
    def __next__(self):
        try:
            return super().__next__()
        except RuntimeError:
            # Native intercept chains can wrap a callback error more than once.
            # The callback object is authoritative; never classify by its text.
            if self._callback_error is not None:
                raise self._callback_error
            raise


def managed_execute(request, callback, **identity):
    errors = []
    def invoke(final):
        token = _CURRENT_ATTEMPT.set(MappingProxyType(dict(identity.get('metadata') or {})))
        try:
            return callback(final)
        except BaseException as exc:
            errors.append(exc)
            raise
        finally:
            _CURRENT_ATTEMPT.reset(token)
    try:
        return relay_llm.execute(request, invoke, **identity)
    except RuntimeError:
        if errors:
            raise errors[-1]
        raise
