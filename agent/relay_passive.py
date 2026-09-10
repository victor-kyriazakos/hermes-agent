"""Sanitizer-aware observation for callbacks that cannot re-enter Relay execution.

Manual calls enqueue publication without invoking policy/execution middleware. Only
use this lane where the host already bypasses managed execution; it must never be
a recovery path around a rejected managed call.
"""
from __future__ import annotations

import contextlib
from typing import Any

from agent import relay_runtime


@contextlib.contextmanager
def _capture(runtime, session, start, end, args, kwargs, *, tool=False):
    from agent.relay_llm import _is_cancellation

    record: dict[str, Any] = {"response": None, "outcome": "success"}
    lease = relay_runtime._warn_on_error("passive lifetime acquisition", runtime.acquire_operation_lease)
    if lease is None:
        yield record
        return
    handle = None
    try:
        handle = relay_runtime._warn_on_error(
            "passive capture start", lease.run_in_session, session, start, *args,
            timeout=relay_runtime._SCOPE_OP_TIMEOUT, **kwargs,
        )
        try:
            yield record
        except BaseException as exc:
            record["outcome"] = "cancelled" if _is_cancellation(exc) else "failed"
            record["error"] = exc
            raise
        finally:
            if handle is not None:
                response = record["response"]
                if tool:
                    response = runtime.relay.ToolExecutionResult(response)
                metadata = {"outcome": record["outcome"], "otel.status_code": {
                    "success": "OK", "failed": "ERROR", "cancelled": "UNSET",
                }[record["outcome"]]}
                error = record.get("error")
                if error is not None:
                    # Event metadata passes through the END event sanitizer; do
                    # not stringify or replace the exception in application state.
                    message = relay_runtime._warn_on_error("passive error description", str, error)
                    metadata["observed_error"] = {
                        "type": type(error).__name__, "message": message,
                        "classification": "unavailable",
                    }
                relay_runtime._warn_on_error(
                    "passive capture end", lease.run_in_session, session, end, handle, response,
                    metadata=metadata, timeout=relay_runtime._SCOPE_OP_TIMEOUT,
                )
    finally:
        lease.release()


@contextlib.contextmanager
def capture_llm(attempt, defer_logical_completion):
    from agent.relay_llm import _complete_logical

    runtime = attempt.runtime
    kwargs = {key: value for key, value in attempt.relay_kwargs.items()
              if key not in {"codec", "response_codec"}}
    record = None
    try:
        with _capture(runtime, attempt.session, runtime.relay.llm.call, runtime.relay.llm.call_end,
                      (attempt.operation, attempt.relay_request), kwargs) as record:
            yield record
    finally:
        if record is not None and not defer_logical_completion:
            _complete_logical(attempt.logical, outcome=record["outcome"])


def capture_tool(runtime, session, parent, name, args, tool_call_id, metadata):
    from agent.relay_llm import _jsonable

    return _capture(runtime, session, runtime.relay.tools.call, runtime.relay.tools.call_end,
                    (name, _jsonable(args)), {"handle": parent, "tool_call_id": tool_call_id,
                                            "metadata": _jsonable(metadata or {})}, tool=True)
