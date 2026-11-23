"""Runner-neutral contracts; no evaluator data or model SDK dependencies."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["act", "clarify", "escalate", "refuse"] | None = None
    text: str = ""
    facts: dict[str, Any] = Field(default_factory=dict)
    technical_status: Literal["completed", "error", "limit_exceeded", "cancelled", "timeout"] = "completed"
    metadata: dict[str, Any] = Field(default_factory=dict)
    process_status: Literal["active", "waiting", "completed", "error"] = "completed"

    @model_validator(mode="after")
    def completed_decision(self):
        if self.technical_status == "completed" and self.action is None:
            raise ValueError("Completed execution requires a business decision")
        return self


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    eval_version: str = Field(min_length=1)
    runner_name: str = Field(min_length=1)
    output_dir: Path = Path("harness_runs")
    max_tool_calls: int = Field(default=30, gt=0)
    max_steps: int = Field(default=50, gt=0)
    model_version: str | None = None
    prompt_version: str | None = None
    mock_setup_version: str | None = None
    capture_response_text: bool = False
    runner_version: str = "unspecified"
    temperature: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    seed: int | None = None
    seed_support: Literal["not_applicable", "unsupported", "supported", "unknown"] = "not_applicable"
    generation_parameters: dict[str, Any] = Field(default_factory=dict)
    scenario_max_tool_calls: int = Field(default=300, gt=0)
    scenario_max_steps: int = Field(default=500, gt=0)
    read_timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    external_timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    request_timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    trace_free_text: bool = False
    legacy_single_step_request: bool = True

    @model_validator(mode="after")
    def seed_configuration(self):
        if self.seed is not None and self.seed_support != "supported":
            raise ValueError("A configured seed requires explicit model support")
        return self


class StepLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class RunContext:
    organization_id: str
    now: datetime
    timezone: str
    tools: Any
    trace: Any
    conversation_id: str | None = None
    process_id: str | None = None
    event_id: str | None = None
    step_id: int | None = None
    state: Any = None
    history: Any = None
    scenario_settings: Any = None

    def step(self):
        """Declare a non-tool reasoning step; tool calls also consume one step."""
        self.tools.step()


class Runner(Protocol):
    def run(self, request: Any, context: RunContext) -> RunResult | Awaitable[RunResult]: ...


EventKind = Literal[
    "user_message", "manager_decision", "supplier_reply", "external_result", "time_tick", "scheduled_wakeup",
]


class EventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: EventKind
    event_id: str = Field(min_length=1, max_length=100)
    data: Any = Field(default_factory=dict)


class ManagerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    approval_id: str | None = Field(default=None, min_length=1, max_length=100)
    approval_from_event: str | None = Field(default=None, min_length=1, max_length=100)
    decision: Literal["approved", "rejected"]
    decided_by: str = Field(min_length=1)

    @model_validator(mode="after")
    def approval_reference(self):
        if (self.approval_id is None) == (self.approval_from_event is None):
            raise ValueError("Provide exactly one of approval_id or approval_from_event")
        return self


class SupplierReplyEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    supplier_id: str
    offer_id: str
    message_id: str
    revision: int = Field(gt=0)
    text: str = Field(min_length=1)
    # Validated with the existing tool schemas at the trusted adapter boundary.
    changes: dict[str, Any]
    confirmed_terms: dict[str, Any] | None = None


class ExternalResultEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    service: Literal["order", "message"]
    operation_id: str | None = Field(default=None, min_length=1)
    operation_from_event: str | None = Field(default=None, min_length=1, max_length=100)
    outcome: Literal["sent", "delivered", "failed", "rejected", "unknown"]

    @model_validator(mode="after")
    def operation_reference(self):
        if (self.operation_id is None) == (self.operation_from_event is None):
            raise ValueError("Provide exactly one of operation_id or operation_from_event")
        return self


class MockOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    order_outcome: Literal["sent", "failed", "rejected", "unknown"] | None = None
    message_status: Literal["sent", "delivered", "failed", "rejected", "unknown"] | None = None
    delivered_at: datetime | None = None
    tool_responses: dict[str, Any] | None = None


class ScenarioEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: str = Field(min_length=1, max_length=100)
    at: datetime
    occurred_at: datetime | None = None
    kind: EventKind
    input: Any = Field(default_factory=dict)
    conversation_id: str | None = Field(default=None, max_length=100)
    user_id: str | None = Field(default=None, max_length=255)
    process_id: str | None = None
    manager: ManagerDecision | None = None
    supplier: SupplierReplyEvent | None = None
    external: ExternalResultEvent | None = None
    mocks: MockOverrides | None = None

    @model_validator(mode="after")
    def validate_event(self):
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise ValueError("Events require aware scenario timestamps")
    
        if self.occurred_at is not None and (self.occurred_at.tzinfo is None or self.occurred_at > self.at):
            raise ValueError("Occurrence time must be aware and no later than delivery")
    
        for field, kind in (("manager", "manager_decision"), ("supplier", "supplier_reply"), ("external", "external_result")):
            if (getattr(self, field) is not None) != (self.kind == kind):
                raise ValueError("Trusted payload must match event kind")
        
        if (self.conversation_id is None) != (self.user_id is None):
            raise ValueError("Conversation identity requires its trusted user")
    
        return self


class MultiStepScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scenario_id: str = Field(min_length=1)
    fixture: dict[str, Any]
    events: list[ScenarioEvent] = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_events(self):
        start = datetime.fromisoformat(self.fixture["now"])
        previous_ids = set()
        for event in self.events:
            if event.at < start:
                raise ValueError("Event delivery times must be chronological and not precede fixture.now")
            reference = (event.manager.approval_from_event if event.manager else
                         event.external.operation_from_event if event.external else None)
            if reference is not None and reference not in previous_ids:
                raise ValueError("Dynamic event references must name an earlier event")
            if event.event_id in previous_ids:
                raise ValueError("Scenario event IDs must be unique")
            previous_ids.add(event.event_id)
            start = event.at
        return self


class RunDeadlineExceeded(RuntimeError):
    pass
