"""Explicit tool allowlist with trusted dependency injection and no retries."""

import copy
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from inspect import signature, Parameter
import json
from threading import RLock
from time import perf_counter
from weakref import WeakKeyDictionary

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from typing import get_type_hints

from pydantic import BaseModel, ConfigDict, create_model

from database.database import session_scope
from database.runtime import event_time
from tools import queries, planning, actions, external, review
from tools.contracts import ToolResult, ToolError, ToolErrorCode as E
from tools.runtime import trusted_tool_responses
from .contracts import StepLimitExceeded, RunDeadlineExceeded
from .trace import json_value, safe_value


@dataclass
class MockServices:
    order_sender: external.MockOrderSender = field(default_factory=external.MockOrderSender)
    message_channel: external.MockMessageChannel = field(default_factory=external.MockMessageChannel)
    supplier_inbox: external.MockSupplierInbox = field(default_factory=external.MockSupplierInbox)


# Manager decision journaling and raw verifier-based offer writes are not LLM tools.
SESSION_TOOLS = {fn.__name__: fn for fn in (
    queries.get_inventory, queries.get_appointments, queries.get_supplier_offers,
    queries.get_pending_orders, queries.get_organization_policy, queries.get_consumption_rules,
    planning.calc_material_need, planning.compare_suppliers,
    actions.prepare_draft, actions.create_order, actions.request_approval,
    external.get_supplier_reply, external.receive_supplier_reply, external.process_supplier_reply,
    review.get_daily_activity,
)}
EXTERNAL_TOOLS = {fn.__name__: fn for fn in (
    actions.dispatch_order, external.send_supplier_message, review.send_review,
)}

TOOL_KINDS = {name: "local_write" for name in SESSION_TOOLS}
TOOL_KINDS.update({name: "read" for name in (
    "get_inventory", "get_appointments", "get_supplier_offers", "get_pending_orders",
    "get_organization_policy", "get_consumption_rules", "get_daily_activity", "get_supplier_reply")})
TOOL_KINDS.update({name: "compute" for name in ("calc_material_need", "compare_suppliers")})
TOOL_KINDS.update({name: "external" for name in EXTERNAL_TOOLS})

_TRUSTED_STATE = WeakKeyDictionary()
_TRUSTED_STATE_LOCK = RLock()


@dataclass
class ScenarioBudget:
    calls: int = 0
    steps: int = 0

TRUSTED = {"session", "factory", "organization_id", "now", "sender", "channel", "inbox",
           "verify_source", "source", "verify_submission"}


def _argument_model(fn):
    hints = get_type_hints(fn)
    fields = {name: (hints[name], ... if p.default is Parameter.empty else p.default)
              for name, p in signature(fn).parameters.items() if name not in TRUSTED}

    return create_model(fn.__name__ + "Arguments", __config__=ConfigDict(extra="forbid", strict=True), **fields)

ARGUMENTS = {name: _argument_model(fn) for name, fn in (SESSION_TOOLS | EXTERNAL_TOOLS).items()}

def failure(code, message, outcome="not_started"):
    return ToolResult(ok=False, error=ToolError(code=code, message=message, operation_outcome=outcome))

class ToolGateway:
    def __init__(self, factory, runtime, mocks, trace, config, *, budget=None):
        self._factory = factory
        self._organization_id, self._now = runtime.organization_id, runtime.now
        self.trace, self.config = trace, config
        self.calls = self.steps = 0
        self.budget = budget or ScenarioBudget()
        self.started = perf_counter()
        self.timed_out = False
        self.limit_exceeded = False
        self.closed = False
        self.parent_span_id = None
        self._lock = RLock()
        with _TRUSTED_STATE_LOCK:
            _TRUSTED_STATE[self] = (copy.deepcopy(runtime.tool_responses), mocks)

    def close(self):
        with self._lock:
            self.closed = True
        with _TRUSTED_STATE_LOCK:
            _TRUSTED_STATE.pop(self, None)

    @property
    def schemas(self):
        return {name: model.model_json_schema() for name, model in ARGUMENTS.items()}

    def step(self):
        with self._lock:
            if self.closed:
                raise RuntimeError("Run is closed")
            
            if self.timed_out or (self.config.request_timeout_seconds is not None and
                                  perf_counter() - self.started >= self.config.request_timeout_seconds):
                self.timed_out = True
                raise RunDeadlineExceeded("Request deadline reached")
            
            if self.steps >= self.config.max_steps or self.budget.steps >= self.config.scenario_max_steps:
                self.limit_exceeded = True
                raise StepLimitExceeded("Step limit reached")
            
            self.steps += 1
            self.budget.steps += 1

    def call(self, name, arguments=None):
        # Synchronous calls finish before returning, including from async runners.
        # No timeout wrapper abandons an in-flight side effect.

        with self._lock, self.trace.span(name if isinstance(name, str) else "invalid_tool",
                                        arguments=arguments, parent_span_id=self.parent_span_id) as span:
            started = perf_counter()
            self.executed = False
            result = self._call(name, arguments)
            elapsed = perf_counter() - started
            kind = TOOL_KINDS.get(name, "forbidden") if isinstance(name, str) else "forbidden"
            limit = self.config.external_timeout_seconds if kind == "external" else self.config.read_timeout_seconds if kind in ("read", "compute") else None

            overrun = limit is not None and elapsed > limit
            if overrun:
                self.timed_out = True

                if kind in ("read", "compute"):
                    result = failure(E.TIMEOUT, "Read/compute deadline exceeded", "failed")
                # An acknowledged external result remains evidence. Never abandon
                # its transaction or turn a known sent receipt into a safe retry.

            span.update(tool_kind=kind, executed=self.executed, deadline_exceeded=overrun)
            if not result.ok:
                if result.error.code == E.APPROVAL_REQUIRED:
                    self.trace.event("approval_wait", tool=name)
                elif result.error.code in (E.EXTERNAL_SERVICE_ERROR, E.TIMEOUT, E.OPERATION_OUTCOME_UNKNOWN):
                    self.trace.event("external_error", tool=name, error_code=result.error.code.value,
                                     operation_outcome=result.error.operation_outcome)
                    
            span.update(result=safe_value(result, redact_free_text=not self.trace.free_text), status="ok" if result.ok else "error",
                        error_code=None if result.ok else result.error.code.value)
            
            if isinstance(arguments, dict):
                span["business_ids"] = {k: safe_value(arguments[k]) for k in
                    ("operation_id", "idempotency_key", "submission_id", "order_id", "message_id") if k in arguments}
                
            return result

    def _call(self, name, arguments):
        if self.closed:
            return failure(E.INVALID_ARGUMENTS, "Run is closed")
        
        if self.calls >= self.config.max_tool_calls or self.budget.calls >= self.config.scenario_max_tool_calls:
            self.limit_exceeded = True
            return failure(E.BUSINESS_RULE_VIOLATION, "Harness tool call limit reached")
        
        self.calls += 1
        self.budget.calls += 1

        try:
            self.step()

        except RunDeadlineExceeded:
            return failure(E.TIMEOUT, "Request deadline reached")
        except StepLimitExceeded:
            return failure(E.BUSINESS_RULE_VIOLATION, "Harness step limit reached")
        
        if not isinstance(name, str) or name not in ARGUMENTS:
            return failure(E.INVALID_ARGUMENTS, "Tool is not allowed")
        
        try:
            if arguments is None:
                arguments = {}

            if not isinstance(arguments, dict) or any(k in TRUSTED for k in arguments):
                raise ValueError("Untrusted dependency injection")
            
            # Serialize only supported caller values; arbitrary objects are never injected.
            def validate(v):
                if isinstance(v, float):
                    raise ValueError("Use decimal strings, not binary floats")
                if isinstance(v, BaseModel):
                    validate(v.model_dump(mode="json"))
                elif isinstance(v, dict):
                    if any(not isinstance(key, str) for key in v):
                        raise ValueError("Argument keys must be strings")
                    for item in v.values(): validate(item)
                elif isinstance(v, (tuple, list)):
                    for item in v: validate(item)
                elif v is not None and not isinstance(v, (str, bool, int, Decimal, date, datetime, Enum)):
                    raise ValueError("Unsupported argument value")
                
            validate(arguments)
            parsed = ARGUMENTS[name].model_validate_json(json.dumps(json_value(arguments), allow_nan=False))
            kwargs = {key: getattr(parsed, key) for key in type(parsed).model_fields}

        except (ValueError, TypeError):
            return failure(E.INVALID_ARGUMENTS, "Arguments do not match the tool schema")
        
        fn = (SESSION_TOOLS | EXTERNAL_TOOLS)[name]

        kind = TOOL_KINDS[name]
        with _TRUSTED_STATE_LOCK:
            responses, mocks = _TRUSTED_STATE.get(self, ({}, None))
            responses = copy.deepcopy(responses)
        if mocks is None:
            return failure(E.INVALID_ARGUMENTS, "Run is closed")
        if "inbox" in signature(fn).parameters:
            kwargs["inbox"] = mocks.supplier_inbox
        if name == "dispatch_order":
            kwargs["sender"] = mocks.order_sender
        if "channel" in signature(fn).parameters:
            kwargs.update(channel=mocks.message_channel, now=self._now)

        spec = responses.get(name, {})

        if not isinstance(spec, dict):
            return failure(E.INVALID_ARGUMENTS, "Invalid trusted tool response configuration")
        
        error = spec.get("error")
        timeout_after_commit = name == "create_order" and error == "timeout" and spec.get("after_commit") is True
        remote_unknown = spec.get("remote_status") == "unknown" or error == "unknown"
        uncertain = remote_unknown or error == "timeout"
        sender_old = mocks.order_sender.outcome
        channel_old = (mocks.message_channel.status, mocks.message_channel.delivered_at)

        if error and kind != "external" and not timeout_after_commit:
            code = E.OPERATION_OUTCOME_UNKNOWN if remote_unknown else E.TIMEOUT if error == "timeout" else E.EXTERNAL_SERVICE_ERROR
            return failure(code, "Injected scenario failure", "unknown" if remote_unknown else "not_started")
        
        if error and kind == "external" and not uncertain:
            return failure(E.EXTERNAL_SERVICE_ERROR, "Injected service unavailability")
        
        try:
            if name == "dispatch_order" and (uncertain or "outcome" in spec):
                outcome = "unknown" if uncertain else spec["outcome"]
                actions.SubmissionReceipt(submission_id="configuration", outcome=outcome)
                mocks.order_sender.outcome = outcome

            if name in ("send_supplier_message", "send_review") and (uncertain or "status" in spec):
                status = "unknown" if uncertain else spec["status"]
                delivered = datetime.fromisoformat(spec["delivered_at"]) if spec.get("delivered_at") else None
                checked = external.MockMessageChannel(status, delivered_at=delivered)
                mocks.message_channel.status, mocks.message_channel.delivered_at = checked.status, checked.delivered_at

            with event_time(self._now), trusted_tool_responses(responses):
                self.executed = True
                if name in EXTERNAL_TOOLS:
                    result = fn(self._factory, self._organization_id, **kwargs)
                else:
                    with session_scope(self._factory) as session:
                        if kind in ("read", "compute") and self.config.read_timeout_seconds is not None:
                            milliseconds = max(1, int(self.config.read_timeout_seconds * 1000))
                            session.execute(text("SELECT set_config('statement_timeout', :value, true)"), {"value": str(milliseconds)})
                        result = fn(session, self._organization_id, **kwargs)
                    
                if not isinstance(result, ToolResult):
                    raise TypeError("Invalid tool result")
                if timeout_after_commit and result.ok:
                    return failure(E.TIMEOUT, "Response lost after local submission transaction", "unknown")
            
                return result
            
        except Exception as exc:
            if kind in ("read", "compute"):
                return failure(E.DATABASE_ERROR if isinstance(exc, SQLAlchemyError) else E.INTERNAL_ERROR,
                               "Read-only operation failed", "failed")
            
            if kind == "local_write":
                return failure(E.DATABASE_ERROR, "Local transaction requires database reconciliation", "unknown")
            
            return failure(E.OPERATION_OUTCOME_UNKNOWN, "External operation requires reconciliation", "unknown")
        
        finally:
            mocks.order_sender.outcome = sender_old
            mocks.message_channel.status, mocks.message_channel.delivered_at = channel_old
