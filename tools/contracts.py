"""Common result and error models for Restoka tools.

All tools return a ToolResult containing a success status, optional data,
or a structured error. ToolError provides standardized error codes,
descriptions, and identifiers for tracking business operations.

For failed operations, operation_outcome indicates whether the action
was not started, definitely failed, or may have already taken effect.
This is important for preventing duplicate actions after timeouts.

These models validate and serialize tool results. They do not execute
operations, check business rules, or handle retries.
"""

from enum import Enum
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["ToolErrorCode", "ToolError", "ToolResult"]

_Message = Annotated[str, Field(min_length=1, pattern=r"\S")]
_OperationId = Annotated[_Message, Field(max_length=255)]
T = TypeVar("T")


class ToolErrorCode(str, Enum):
    INTERNAL_ERROR = "internal_error"
    INVALID_ARGUMENTS = "invalid_arguments"
    MISSING_DATA = "missing_data"
    NOT_FOUND = "not_found"
    DATABASE_ERROR = "database_error"
    TIMEOUT = "timeout"
    EXTERNAL_SERVICE_ERROR = "external_service_error"
    BUSINESS_RULE_VIOLATION = "business_rule_violation"
    APPROVAL_REQUIRED = "approval_required"
    DUPLICATE_OPERATION = "duplicate_operation"
    OPERATION_OUTCOME_UNKNOWN = "operation_outcome_unknown"


class ToolError(BaseModel):
    """Structured error returned by a tool.

    Includes an error code, a safe description, and optional operation
    identifiers. The operation outcome indicates whether side effects
    may have occurred and whether the result needs verification.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    code: ToolErrorCode
    message: _Message
    operation_id: _OperationId | None = None
    idempotency_key: _OperationId | None = None
    operation_outcome: Literal["not_started", "failed", "unknown"] = "unknown"


class ToolResult(BaseModel, Generic[T]):
    """Standard result returned by a tool.

    A successful result contains optional data and no error.
    A failed result contains an error and no data.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    ok: bool
    data: T | None = None
    error: ToolError | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "ToolResult[T]":
        if self.ok and self.error is not None:
            raise ValueError("Successful result must not contain an error")

        if not self.ok and (self.error is None or self.data is not None):
            raise ValueError("Failed result must contain an error and no data")

        return self
