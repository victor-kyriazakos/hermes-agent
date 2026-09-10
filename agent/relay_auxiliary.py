"""Finite Relay ownership for auxiliary work outside a conversation turn."""
import contextlib

from agent import relay_runtime


def call_with_stream_lifetime(scope, callback):
    """Detach a returned stream's scope from the caller's ContextVar stack."""
    import contextvars
    context = contextvars.copy_context()
    context.run(scope.__enter__)
    try:
        result = context.run(callback)
    except BaseException as exc:
        context.run(scope.__exit__, type(exc), exc, exc.__traceback__)
        raise
    turn = context.run(relay_runtime.current_turn)
    if (turn is not None and turn.relay_enabled and not turn.closed
            and hasattr(result, "__next__")):
        return _ScopedStream(result, scope, context)
    context.run(scope.__exit__, None, None, None)
    return result


class _ScopedStream:
    """Keep standalone ownership until exhaustion, failure, or explicit close."""
    def __init__(self, stream, scope, context):
        self._stream, self._scope, self._context = stream, scope, context
        self._closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise StopIteration
        try:
            return self._context.run(next, self._stream)
        except StopIteration:
            self._finish()
            raise
        except BaseException as exc:
            self._finish(exc)
            raise

    def _finish(self, error=None):
        if self._closed:
            return
        self._closed = True
        self._context.run(self._scope.__exit__,
                          type(error) if error is not None else None,
                          error, error.__traceback__ if error is not None else None)

    def close(self):
        if self._closed:
            return
        try:
            close = getattr(self._stream, "close", None)
            if close is not None:
                self._context.run(close)
        except BaseException as exc:
            self._finish(exc)
            raise
        else:
            self._finish(GeneratorExit())

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __getattr__(self, name):
        return getattr(self._stream, name)


@contextlib.contextmanager
def standalone_context(request_id):
    # Never replace a disabled/closed inherited turn (including overlap exclusion),
    # nor initialize process-global exporters just because an auxiliary call ran.
    runtime = relay_runtime.get_runtime(create=False)
    if (relay_runtime.current_turn() is not None or runtime is None
            or not runtime.managed_execution_enabled()):
        yield
        return
    coordinator = relay_runtime.SESSION_COORDINATOR
    lease = coordinator.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(), session_id=request_id, platform="auxiliary")
    turn = coordinator.begin_turn(lease, turn_id=request_id, task_id=request_id)
    outcome = "success"
    try:
        yield
    except BaseException as exc:
        from agent.relay_llm import _is_cancellation
        outcome = "cancelled" if _is_cancellation(exc) else "failed"
        raise
    finally:
        coordinator.end_turn(turn, outcome=outcome)
        coordinator.finalize_conversation(profile_key=lease.profile_key, session_id=lease.session_id)
        coordinator.release_conversation(lease)
