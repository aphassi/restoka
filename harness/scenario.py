"""Isolated PostgreSQL runs with persisted evidence and no evaluation logic."""

import os
import asyncio
import copy
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from inspect import isawaitable
import json
from types import MappingProxyType
from time import perf_counter
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.schema import CreateSchema, DropSchema

from database.database import make_engine, make_session_factory, session_scope
from database.load_fixture import load_fixture
from database.models import Base
from .contracts import (RunConfig, RunContext, RunResult, EventRequest, StepLimitExceeded,
                        RunDeadlineExceeded, MultiStepScenario, ScenarioEvent)
from .gateway import MockServices, ToolGateway, ScenarioBudget
from .events import (apply_event, processed_event_result, record_event_processed,
                     HistoryAccess)
from .evidence import snapshot, business_changes, StateAccess
from .trace import TraceCollector, json_value, safe_value
from tools.external import MockOrderSender, MockMessageChannel, MockSupplierInbox


class EventReferenceError(ValueError):
    pass


def _created_bindings(before, after):
    """Return trusted IDs created while processing one scenario event."""
    def rows(state, table):
        return {row["id"]: row for row in (state or {}).get(table, {}).get("rows", [])}

    old_approvals, new_approvals = rows(before, "approval_requests"), rows(after, "approval_requests")
    old_events, new_events = rows(before, "business_events"), rows(after, "business_events")
    operations = {"order": [], "message": []}
    for event_id, row in new_events.items():
        if event_id in old_events:
            continue
        service = ("order" if row["type"] == "order_submission" else
                   "message" if row["type"] in ("supplier_message", "daily_review") else None)
        details = row.get("details") or {}
        operation_id = (details.get("request") or {}).get("operation_id")
        if service is not None and isinstance(operation_id, str):
            operations[service].append(operation_id)

    return {
        "approval": sorted(row_id for row_id in new_approvals if row_id not in old_approvals),
        "order": sorted(operations["order"]),
        "message": sorted(operations["message"]),
    }


def _resolve_event_reference(event, bindings):
    """Resolve an explicit prior-event reference, rejecting absent or ambiguous matches."""
    if event.manager and event.manager.approval_from_event:
        reference = event.manager.approval_from_event
        candidates = bindings.get(reference, {}).get("approval", [])
        if len(candidates) != 1:
            raise EventReferenceError(f"Event {reference!r} must have created exactly one approval request")
        manager = event.manager.model_copy(update={"approval_id": candidates[0], "approval_from_event": None})
        return event.model_copy(update={"manager": manager})

    if event.external and event.external.operation_from_event:
        reference = event.external.operation_from_event
        candidates = bindings.get(reference, {}).get(event.external.service, [])
        if len(candidates) != 1:
            raise EventReferenceError(f"Event {reference!r} must have created exactly one {event.external.service} operation")
        external = event.external.model_copy(update={"operation_id": candidates[0], "operation_from_event": None})
        return event.model_copy(update={"external": external})

    return event


class ScenarioRunner:
    def __init__(self, database_url=None, *, configure_mocks=None, configure_adapters=None,
                 runner_factory=None):
        self._database_url = database_url
        self._configure_mocks = configure_mocks
        self._configure_adapters = configure_adapters
        self._runner_factory = runner_factory

    def run(self, scenario, config: RunConfig, runner):
        """Synchronous entry point; use arun inside an existing event loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.arun(scenario, config, runner))
    
        raise RuntimeError("Use await ScenarioRunner.arun in an event loop")

    async def arun(self, scenario, config: RunConfig, runner):
        # Keep the old execution-only projection, even if the envelope contains gold data.
        legacy = not isinstance(scenario, MultiStepScenario) and "events" not in scenario
        if legacy:
            scenario_id = str(scenario["id"])
            fixture = copy.deepcopy(scenario["fixture"])
            events = [ScenarioEvent(event_id="initial", at=datetime.fromisoformat(fixture["now"]),
                                    kind="user_message", input=copy.deepcopy(scenario["input"]))]
        else:
            definition = scenario if isinstance(scenario, MultiStepScenario) else MultiStepScenario.model_validate({
                "scenario_id": scenario.get("scenario_id", scenario.get("id")),
                "fixture": scenario["fixture"], "events": scenario["events"]})
        
            scenario_id, fixture, events = definition.scenario_id, copy.deepcopy(definition.fixture), definition.events
        
        fixture_hash = sha256(json.dumps(json_value(fixture), sort_keys=True,
                                        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        run_id = uuid4().hex
    
        directory = config.output_dir / run_id
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        directory.chmod(0o700)
    
        result_path, evaluator_path = directory / "result.json", directory / "evaluator.json"
        schema = "restoka_run_" + uuid4().hex
    
        engine = scoped = gateway = None
        created = False
        errors, state, initial = [], None, None
        step_records, private_results, all_spans = [], [], []
        budget = ScenarioBudget()
        started = perf_counter()
        result = RunResult(technical_status="error", process_status="error")
    
        try:
            engine = make_engine(self._database_url, connect_args={"connect_timeout": 5})

            with engine.begin() as conn:
                conn.execute(CreateSchema(schema))

            created = True
            scoped = engine.execution_options(schema_translate_map={None: schema})

            Base.metadata.create_all(scoped)
            factory = make_session_factory(scoped)

            with session_scope(factory) as session:
                runtime = load_fixture(session, fixture)

            mocks = MockServices()
            if self._configure_mocks:
                self._configure_mocks(mocks, runtime)

            if not (isinstance(mocks.order_sender, MockOrderSender)
                    and isinstance(mocks.message_channel, MockMessageChannel)
                    and isinstance(mocks.supplier_inbox, MockSupplierInbox)):
                raise TypeError("Only local mock services are supported")
        
            initial = state = snapshot(scoped)
            responses = copy.deepcopy(runtime.tool_responses)
            bindings = {}
        
            for index, event in enumerate(events):
                trace = TraceCollector(run_id=run_id, trace_id=run_id if legacy else uuid4().hex,
                    scenario_id=scenario_id, event_id=event.event_id,
                    process_id=event.process_id, conversation_id=event.conversation_id, free_text=config.trace_free_text)
                trace_path = directory / ("trace.json" if legacy else f"trace_{index:04d}.json")
                before = state
                gateway = None
                step_errors = []
                step_started = perf_counter()
                result = RunResult(technical_status="error", process_status="error")
                duplicate = replayed = runner_finished = False
                trace.event("event_received", event_id=event.event_id, event_type=event.kind,
                            scenario_time=event.at, step_id=index)
            
                with trace.span("runner.run", kind="run") as root:
                    try:
                        event = _resolve_event_reference(event, bindings)
                        if not legacy:
                            duplicate = apply_event(factory, runtime.organization_id, event, mocks)
                            trace.event("event_applied", duplicate=duplicate, event_type=event.kind)
                            if not duplicate and event.mocks and event.mocks.tool_responses is not None:
                                responses = copy.deepcopy(event.mocks.tool_responses)
                            if duplicate:
                                stored = processed_event_result(factory, runtime.organization_id, event)
                                if stored is not None:
                                    result = stored
                                    replayed = True
                                    trace.event("event_deduplicated", processing_state="processed")
                                else:
                                    trace.event("event_recovery", processing_state="applied")

                        if not replayed:
                            if self._configure_adapters:
                                allowed = {name: copy.deepcopy(responses[name]) for name in
                                           ("llm_structured_output", "supplier_callback") if name in responses}
                                self._configure_adapters(mocks, MappingProxyType(allowed))
                            step_runtime = replace(runtime, now=event.at, tool_responses=copy.deepcopy(responses))
                            gateway = ToolGateway(factory, step_runtime, mocks, trace, config, budget=budget)
                            gateway.parent_span_id = root["span_id"]

                            history = HistoryAccess(factory, runtime.organization_id, event, trace) if event.conversation_id else None
                            context = RunContext(runtime.organization_id, event.at, runtime.timezone, gateway, trace,
                                conversation_id=event.conversation_id, process_id=event.process_id,
                                event_id=event.event_id, step_id=index, state=StateAccess(scoped, trace),
                                history=history, scenario_settings={})

                            event_data = copy.deepcopy(event.input)
                            if not isinstance(event_data, dict):
                                event_data = {"value": event_data}
                            if legacy and isinstance(event.input, dict) and runtime.proposed_order is not None:
                                event_data.setdefault("proposed_order", copy.deepcopy(runtime.proposed_order))
                            if (legacy and runtime.supplier_message and isinstance(event_data.get("text"), str)
                                    and runtime.supplier_message in event_data["text"]):
                                event_data.setdefault("supplier_statement", runtime.supplier_message)
                            if event.manager:
                                event_data.update(approval_id=event.manager.approval_id)
                            if event.supplier:
                                event_data.update(supplier_id=event.supplier.supplier_id,
                                                  offer_id=event.supplier.offer_id,
                                                  message_id=event.supplier.message_id,
                                                  revision=event.supplier.revision)
                            if event.external:
                                event_data.update(service=event.external.service,
                                                  operation_id=event.external.operation_id)

                            normalized = EventRequest(kind=event.kind, event_id=event.event_id,
                                                      data=event_data).model_dump()
                            request = normalized
                            if legacy and config.legacy_single_step_request:
                                request = event_data if isinstance(event.input, dict) else copy.deepcopy(event.input)

                            executor = self._runner_factory() if self._runner_factory else runner

                            returned = executor.run(request, context)
                            if isawaitable(returned):
                                remaining = None if config.request_timeout_seconds is None else max(
                                    0.0, config.request_timeout_seconds - (perf_counter() - gateway.started))
                                returned = await asyncio.wait_for(returned, remaining) if remaining is not None else await returned

                            result = RunResult.model_validate(returned.model_dump() if isinstance(returned, RunResult) else returned)
                            result.model_dump_json()
                            runner_finished = True

                            if config.request_timeout_seconds is not None and perf_counter() - gateway.started > config.request_timeout_seconds:
                                gateway.timed_out = True
                        
                    except EventReferenceError:
                        result = RunResult(technical_status="error", process_status="error")
                        step_errors.append({"stage": "event_binding", "code": "unresolved_or_ambiguous_reference"})
                    except StepLimitExceeded:
                        result = RunResult(technical_status="limit_exceeded", process_status="error")
                    except (RunDeadlineExceeded, TimeoutError):
                        result = RunResult(technical_status="timeout", process_status="error")
                        step_errors.append({"stage": "runner", "code": "deadline_exceeded"})
                    except asyncio.CancelledError:
                        result = RunResult(technical_status="cancelled", process_status="error")
                        step_errors.append({"stage": "runner", "code": "cancelled"})
                    except Exception:
                        result = RunResult(technical_status="error", process_status="error")
                        step_errors.append({"stage": "runner_or_event", "code": "execution_error"})
                    finally:
                        if gateway is not None:
                            if gateway.limit_exceeded or gateway.timed_out:
                                result = result.model_copy(update={"technical_status": "limit_exceeded" if gateway.limit_exceeded else "timeout",
                                                                   "process_status": "error"})
                            gateway.close()

                    trace.event("decision", action=result.action, available=result.action is not None,
                                process_status=result.process_status)
                
                    if result.process_status == "waiting":
                        trace.event("process_waiting", action=result.action)
                    
                    root.update(status=result.technical_status, result=result.model_dump(mode="json"))
                
                try:
                    state = snapshot(scoped)
                except Exception:
                    state = None
                    step_errors.append({"stage": "snapshot", "code": "snapshot_error"})
                    result = result.model_copy(update={"technical_status": "error", "process_status": "error"})

                if not legacy and runner_finished:
                    try:
                        processed_id, processed_details = record_event_processed(
                            factory, runtime.organization_id, event, result)
                        if state is not None:
                            for row in state["business_events"]["rows"]:
                                if row["id"] == processed_id:
                                    row["details"] = processed_details
                                    break
                        trace.event("event_processed", replayed=False)
                    except Exception:
                        result = result.model_copy(update={"technical_status": "error", "process_status": "error"})
                        step_errors.append({"stage": "event_processing_evidence", "code": "persistence_error"})

                root.update(status=result.technical_status, result=result.model_dump(mode="json"))
                trace.event("final_status", technical_status=result.technical_status, process_status=result.process_status)
                trace.save(trace_path)
            
                all_spans.extend(trace.spans)
                changes = business_changes(before, state) if state is not None else None
                if state is not None:
                    bindings[event.event_id] = _created_bindings(before, state)

                step_record = dict(step_id=index, event_id=event.event_id, event_type=event.kind, at=event.at,
                    process_id=event.process_id, conversation_id=event.conversation_id, event_configuration=event,
                    trace_id=trace.trace_id, trace_path=str(trace_path), result=result,
                    technical_errors=step_errors, changes=changes,
                    duration_seconds=perf_counter()-step_started,
                    tool_responses=responses, mock_config=dict(order_outcome=mocks.order_sender.outcome,
                        message_status=mocks.message_channel.status, delivered_at=mocks.message_channel.delivered_at))
            
                step_record["budget_state"] = dict(
                    scenario_steps=budget.steps, scenario_tool_calls=budget.calls,
                    limit_exceeded=(gateway.limit_exceeded if gateway is not None else False))
                step_record["mock_state"] = dict(
                    orders=[dict(operation_key=key[1], receipt=receipt.model_dump(mode="json"))
                            for key, receipt in mocks.order_sender.records.items()],
                    messages=[dict(operation_key=key[1], receipt=receipt.model_dump(mode="json"))
                              for key, receipt in mocks.message_channel.records.items()],
                    order_send_attempts=mocks.order_sender.send_attempts,
                    message_send_attempts=mocks.message_channel.send_attempts)
                private_record = safe_value(step_record)
                private_record["result"] = safe_value(result, redact_free_text=False)
                private_record["result"]["text"] = result.text
                if state is not None:
                    private_record["daily_review_records"] = safe_value([
                        row for row in state["business_events"]["rows"] if row.get("type") == "daily_review"
                    ], redact_free_text=False)
            
                private_results.append(private_record)

                step_records.append(safe_value(step_record))

                snapshot_path = directory / f"state_{index:04d}.json"
                snapshot_path.write_text(json.dumps(safe_value(state), ensure_ascii=False, indent=2))

                step_records[-1]["snapshot_path"] = str(snapshot_path)
                errors.extend(dict(e, step_id=index) for e in step_errors)

                if result.technical_status == "cancelled":
                    break
        except Exception:
            errors.append({"stage": "setup", "code": "setup_error"})

            fallback = TraceCollector(run_id=run_id, scenario_id=scenario_id, event_id="setup")
            fallback.event("event_received", phase="setup")
            fallback.event("decision", action=None, available=False)
            fallback.event("final_status", technical_status="error")
            fallback_path = directory / "trace.json"
            fallback.save(fallback_path)

            if not step_records:
                step_records.append(dict(trace_id=fallback.trace_id, trace_path=str(fallback_path),
                                         result=json_value(result)))
                
        finally:
            if created:
                try:
                    # Only the random schema created by this invocation is ever removed.
                    with engine.begin() as conn:
                        conn.execute(DropSchema(schema, cascade=True))
                except Exception:
                    errors.append({"stage": "cleanup", "code": "cleanup_error", "schema": schema})
            if engine is not None:
                engine.dispose()

        failed = errors or any(s["result"]["technical_status"] != "completed" for s in step_records)
        record = dict(format_version=2, run_id=run_id, scenario_id=scenario_id,
            runner=config.runner_name, eval_version=config.eval_version, fixture_sha256=fixture_hash,
            config=json_value(config), result=json_value(result), technical_errors=errors,
            scenario_status="error" if failed else result.process_status,
            step_results=step_records, changes=business_changes(initial,state) if state is not None else None,
            tool_errors=[dict(span_id=s["span_id"], operation=s["operation"], code=s["error_code"])
                         for s in all_spans if s["kind"]=="tool" and s["error_code"]],
            duration_seconds=perf_counter()-started, tool_calls=budget.calls, steps=budget.steps,
            llm_usage=[s for s in all_spans if s["kind"]=="llm"], database_state=state,
            trace_path=step_records[-1]["trace_path"], result_path=str(result_path), evaluator_path=str(evaluator_path))
        
        # The evaluator artifact is separate and owner-only. It retains the actual
        # answer; the diagnostic result and traces remain redacted by default.
        
        with os.fdopen(os.open(evaluator_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
            json.dump(dict(run_id=run_id, scenario_id=scenario_id, fixture_sha256=fixture_hash,
                           steps=private_results), out, ensure_ascii=False, indent=2, allow_nan=False)
        
        record = safe_value(record)

        if config.capture_response_text:
            record["result"]["text"] = safe_value(result.text)

        result_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False))
        
        return record
