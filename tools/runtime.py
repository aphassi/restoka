"""Trusted per-call tool failure context shared by query and planning tools."""

from contextlib import contextmanager
from contextvars import ContextVar
import copy
from types import MappingProxyType
from typing import Any, Mapping

from .contracts import ToolError, ToolErrorCode, ToolResult


_responses = ContextVar("restoka_trusted_tool_responses", default=MappingProxyType({}))


@contextmanager
def trusted_tool_responses(responses: Mapping[str, Any]):
    """Bind trusted scenario failures for the current synchronous call only."""
    value = MappingProxyType(copy.deepcopy(dict(responses)))
    token = _responses.set(value)
    try:
        yield
    finally:
        _responses.reset(token)


def injected_failure(tool_name: str) -> ToolResult | None:
    spec = _responses.get().get(tool_name)
    if not isinstance(spec, dict) or not spec.get("error"):
        return None

    error = spec["error"]
    remote_unknown = spec.get("remote_status") == "unknown" or error == "unknown"
    code = (ToolErrorCode.OPERATION_OUTCOME_UNKNOWN if remote_unknown else
            ToolErrorCode.TIMEOUT if error == "timeout" else ToolErrorCode.EXTERNAL_SERVICE_ERROR)
    return ToolResult(ok=False, error=ToolError(
        code=code, message="Injected scenario failure",
        operation_outcome="unknown" if remote_unknown else "not_started",
    ))
