"""Оркестратор Restoka: работа с моделью, инструментами и сохранённым состоянием."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
from time import perf_counter
from typing import Any, Literal, TypedDict
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from database.history import ChatMessage, ToolCall
from harness.contracts import RunResult, StepLimitExceeded, RunDeadlineExceeded
from harness.trace import json_value
from tools.contracts import ToolError, ToolErrorCode, ToolResult
from .clients import ModelClient, ModelTimeoutError
from .context import (
    _request_payload,
    _rows,
    approved_draft_context,
    build_model_messages,
    business_context,
    fit_model_context,
    order_offer_discrepancies,
    partial_order_coverage,
)
from .offer_table import compare_offer_table
from .permissions import ToolPermissions, may_have_written, tool_permissions
from .prompts import build_system_prompt
from .tool_descriptions import model_tools
from .verification import (
    _confirmed_submission_facts,
    _include_offer_sources,
    _include_single_material_quantities,
    _needs_fresh_supplier_comparison,
    _offer_source_ids,
    _pending_approval_reason,
    _proposed_purchase_restriction,
    _reconciled_local_submission,
    _recommended_supplier,
    _referenced_order,
    _unsafe_action_claim,
    _unverified_fact_fields,
    _unverified_text_amount,
)

PROMPT_VERSION = "restoka-llm-instructions-45"

_PROPOSAL_RESTRICTION_TEXT = {
    "auto_limit_exceeded": "Указанная сумма превышает лимит автоматической закупки. Требуется согласование.",
    "supplier_not_approved": "Указанный поставщик не одобрен. Требуется согласование.",
    "approval_required_by_policy": (
        "Для закупки требуется одобрение менеджера. Уточните количество, чтобы подготовить заявку на согласование."
    ),
}

_COMPARISON_SCOPE_FIELDS = {
    "chosen_supplier", "recommended_offer_id", "source_ids", "material", "quantity", "required_by",
    "offer_id", "supplier_id",
}

_OFFER_TERM_LABELS = {
    "unit_price_rub": "цену за единицу",
    "delivery_fee_rub": "стоимость доставки",
    "delivery_date": "дату доставки",
}

class FinalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["act", "clarify", "escalate", "refuse"]
    text: str = Field(min_length=1)
    facts: dict[str, Any] = Field(default_factory=dict)
    process_status: Literal["active", "waiting", "completed", "error"] = "completed"


class SupplierStatementFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    material: str | None = Field(None, min_length=1)
    quantity: float | None = Field(None, gt=0, allow_inf_nan=False)
    unit: str | None = Field(None, min_length=1)
    unit_price_rub: float | None = Field(None, ge=0, allow_inf_nan=False)
    currency: str | None = Field(None, pattern=r"^[A-Z]{3}$")
    delivery_fee_rub: float | None = Field(None, ge=0, allow_inf_nan=False)
    delivery_date: date | None = None
    available: bool | None = None
    remaining_confirmed: bool | None = None
    delivery_confirmed: bool | None = None
    fully_confirmed: bool | None = None


def _supplier_statement(event: dict[str, Any]) -> str | None:
    data = event.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        return None
    if event["kind"] == "supplier_message" and not any(
            data.get(key) for key in ("supplier_id", "offer_id", "message_id")):
        return data["text"] or None
    statement = data.get("supplier_statement")
    if event["kind"] == "user_message" and isinstance(statement, str) and statement in data["text"]:
        return statement or None
    return None


def _request_final_repair(messages: list[dict[str, Any]], content: str | None,
                          reason: str, unsupported_fields: list[str]) -> None:
    if content:
        messages.append({"role": "assistant", "content": content})
    messages.append({"role": "user", "content": json.dumps({
        "type": "final_answer_verification_failed",
        "reason": reason,
        "unverified_fact_fields": unsupported_fields,
        "instruction": ("Исправь только итоговый ответ. Верни один JSON без Markdown с полями "
                        "action, text, facts, process_status. Удали каждое поле из списка "
                        "unverified_fact_fields целиком из facts, в том числе из вложенных объектов. "
                        "Сохрани подтверждённые расчёты и условия под именами из текущих результатов "
                        "инструментов. Сообщения пользователя и поставщика передавай в text "
                        "с указанием источника. Если данных для действия не хватает, задай "
                        "конкретный вопрос в text. "
                        "Пустой объект facts допустим, когда подтверждённых фактов нет. "
                        "Утверждай, что действие выполнено, только при наличии подтверждения. "
                        "Если нужно уточнение, напиши, какое поле отсутствует и что нужно выяснить. "
                        "Не перечисляй операции, которые не выполнялись. "
                        "Инструменты больше недоступны.")
    }, ensure_ascii=False)})


def _supplier_update_result(event: dict[str, Any], snapshot: dict[str, Any],
                            successful_tools: list[tuple[str, Any]]) -> RunResult | None:
    """Возвращает факты подтверждённого обновления, если модель не смогла их оформить."""
    if event["kind"] != "supplier_reply" or not isinstance(event["data"], dict):
        return None
    offer_id = event["data"].get("offer_id")
    if not offer_id:
        return None
    if any(name in {"prepare_draft", "create_order", "dispatch_order", "request_approval",
                    "send_supplier_message", "send_review"} for name, _ in successful_tools):
        return None
    update = next((data for name, data in reversed(successful_tools)
                   if name == "process_supplier_reply" and isinstance(data, dict)
                   and data.get("id") == offer_id), None)
    offer = next((row for row in _rows(snapshot, "supplier_offers") if row.get("id") == offer_id), None)
    if update is None or offer is None or not all(
        offer.get(key) == update.get(key) for key in ("supplier_id", "delivery_date", "confirmed")
    ):
        return None
    facts = {"offer": update}
    text = "Обновление предложения поставщика сохранено."
    if update.get("delivery_date"):
        text += f" Срок в предложении — {update['delivery_date']}."
    discrepancies = [item for item in order_offer_discrepancies(snapshot)
                     if item["offer_id"] == offer_id]
    if discrepancies:
        facts["order_offer_discrepancies"] = discrepancies
        text += " Срок в сохранённом заказе не изменился. Уточните фактический срок у поставщика."
        return RunResult(action="escalate", text=text, facts=facts, process_status="waiting",
                         metadata={"recovery": "verified_supplier_update"})
    return RunResult(action="act", text=text, facts=facts,
                     metadata={"recovery": "verified_supplier_update"})


def _tool_message_result(result: Any, *, coverage: dict[str, Any] | None = None) -> str:
    payload = json_value(result)
    if coverage is not None:
        payload["data"]["order_coverage"] = coverage
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def _record_blocked_tool(trace: Any, name: str, stage: str, *, critical: bool = False) -> None:
    if hasattr(trace, "event"):
        trace.event("tool_call_blocked", tool=name, stage=stage, critical=critical)


def _record_rejected_batch(trace: Any, calls: Any, stage: str) -> None:
    if not isinstance(calls, list):
        return
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        name = name if isinstance(name, str) and name else "invalid_tool"
        _record_blocked_tool(trace, name, stage, critical=stage == "final_verification" and may_have_written(name))


def _review_receipt_facts(successful_tools: list[tuple[str, Any]]) -> dict[str, Any]:
    for name, data in reversed(successful_tools):
        if name == "send_review" and isinstance(data, dict) and data.get("status") in {"sent", "delivered"}:
            return {key: data[key] for key in ("report_date", "event_id", "status")}
    return {}


def _verified_effect_result(event: dict[str, Any], snapshot: dict[str, Any],
                            successful_tools: list[tuple[str, Any]], submission_facts: dict[str, Any],
                            approval_reason: str | None) -> RunResult | None:
    if submission_facts:
        return RunResult(action="act", text="Заказ отправлен поставщику.", facts=submission_facts,
                         metadata={"recovery": "verified_submission"})
    receipt = _review_receipt_facts(successful_tools)
    if receipt:
        facts = receipt
        if event["kind"] == "scheduled_review":
            for source, activity in reversed(successful_tools):
                if source == "get_daily_activity" and activity["report_date"] == facts["report_date"]:
                    facts["event_ids"] = [item["id"] for item in activity["events"]]
                    break
        text = ("Ежедневный отчёт доставлен." if facts["status"] == "delivered"
                else "Ежедневный отчёт отправлен.")
        return RunResult(action="act", text=text, facts=facts, metadata={"recovery": "verified_review"})
    if approval_reason and not any(name in {"create_order", "dispatch_order", "send_review", "send_supplier_message"}
                                   for name, _ in successful_tools):
        recorded_ids = {data.get("id") for name, data in successful_tools
                        if name == "request_approval" and isinstance(data, dict) and data.get("id")}
        approvals = [row for row in _rows(snapshot, "approval_requests")
                     if row.get("id") in recorded_ids and row.get("status") == "pending"]
        if len(approvals) == 1:
            approval = approvals[0]
            return RunResult(action="escalate", process_status="waiting",
                             text="Заявка на согласование создана. Ожидается решение управляющего.",
                             facts={"approval_id": approval["id"], "order_id": approval["order_id"],
                                    "approval_status": approval["status"], "reason": approval_reason},
                             metadata={"recovery": "verified_approval"})
    if not any(name in {"create_order", "dispatch_order", "request_approval", "send_review", "send_supplier_message"}
               for name, _ in successful_tools):
        prepared_ids = {data.get("id") for name, data in successful_tools
                        if name == "prepare_draft" and isinstance(data, dict) and data.get("id")}
        drafts = [row for row in _rows(snapshot, "purchase_orders")
                  if row.get("id") in prepared_ids and row.get("status") == "draft"]
        if len(drafts) == 1:
            draft = drafts[0]
            return RunResult(action="act", text="Черновик закупки подготовлен.",
                             facts={"order_id": draft["id"], "order_status": draft["status"]},
                             metadata={"recovery": "verified_draft"})
    return _supplier_update_result(event, snapshot, successful_tools)


def _verified_read_result(facts: dict[str, Any], snapshot: dict[str, Any],
                          successful_tools: list[tuple[str, Any]]) -> RunResult | None:
    """Возвращает подтверждённый результат чтения без утверждений о новых действиях."""
    order_reads = [(name, data) for name, data in successful_tools if name == "get_pending_orders"]
    order = _referenced_order(facts, snapshot, order_reads)
    if order is None or not order.get("status"):
        return _verified_comparison_result(successful_tools)
    status = {"order_id": order["id"], "order_status": order["status"]}
    text = f"Статус сохранённого заказа {order['id']} по данным системы — {order['status']}."
    if order.get("eta"):
        status["order_eta"] = order["eta"]
        text += f" Ожидаемая дата поставки — {order['eta']}."
    approvals = [row for row in _rows(snapshot, "approval_requests")
                 if row.get("order_id") == order["id"] and row.get("status") == "pending"]
    if len(approvals) == 1:
        status.update(approval_id=approvals[0]["id"], approval_status="pending")
        text += " Согласование ожидает решения управляющего."
    return RunResult(action="act", text=text, facts=status, metadata={"recovery": "verified_order_status"})


def _current_supplier_comparison(successful_tools: list[tuple[str, Any]]) -> dict[str, Any] | None:
    comparisons = [data for name, data in successful_tools if name == "compare_suppliers" and isinstance(data, dict)]
    scopes = {(data.get("material"), data.get("quantity"), data.get("required_by")) for data in comparisons}
    return comparisons[-1] if len(scopes) == 1 else None


def _verified_comparison_result(successful_tools: list[tuple[str, Any]]) -> RunResult | None:
    comparison = _current_supplier_comparison(successful_tools)
    if comparison is None:
        return None
    offer_id = comparison.get("recommended_offer_id")
    supplier = _recommended_supplier(successful_tools, offer_id) if offer_id else None
    if not supplier:
        return None
    verified = {key: comparison[key] for key in ("material", "quantity", "required_by") if key in comparison}
    verified.update(chosen_supplier=supplier, recommended_offer_id=offer_id)
    offers = []
    for entry in comparison.get("offers", []):
        offer = entry["offer"]
        offers.append({"offer_id": offer["id"], "supplier_id": offer["supplier_id"],
                       "total_rub": entry.get("total_rub"), "delivery_date": offer.get("delivery_date"),
                       "suitability": entry.get("suitability")})
    verified["offers"] = offers
    verified["source_ids"] = [offer["offer_id"] for offer in offers]
    _include_single_material_quantities(verified, successful_tools)
    return RunResult(action="act", text=f"Сравнил предложения. Рекомендованный поставщик — {supplier}.",
                     facts=verified, metadata={"recovery": "verified_supplier_comparison"})


def _verified_offer_clarification(facts: dict[str, Any],
                                  successful_tools: list[tuple[str, Any]]) -> RunResult | None:
    comparison = _current_supplier_comparison(successful_tools)
    if comparison is None:
        return None
    candidates = []
    requested = facts.get("missing_fields")
    for entry in comparison.get("offers", []):
        offer = entry["offer"]
        if entry.get("commercial_suitability") != "incomplete" or (
                facts.get("offer_id") and facts["offer_id"] != offer["id"]):
            continue
        missing = [
            field for field in _OFFER_TERM_LABELS
            if f"unknown_{field}" in entry.get("reasons", []) and offer.get(field) is None
            and (not isinstance(requested, list) or not requested or field in requested)
        ]
        if missing:
            candidates.append((offer, missing))
    if len(candidates) != 1:
        return None
    offer, missing = candidates[0]
    terms = ", ".join(_OFFER_TERM_LABELS[field] for field in missing)
    return RunResult(
        action="clarify", process_status="waiting",
        text=f"Уточните {terms} по предложению {offer['id']} от поставщика {offer['supplier_id']}.",
        facts={"offer_id": offer["id"], "supplier_id": offer["supplier_id"], "missing_fields": missing},
        metadata={"recovery": "verified_offer_clarification"},
    )


def _scheduled_review_arguments(arguments: Any, successful_tools: list[tuple[str, Any]]) -> Any:
    """Формирует структурированное ревью по подтверждённому журналу отчётного дня."""
    if not isinstance(arguments, dict):
        return arguments
    for name, activity in reversed(successful_tools):
        if (name != "get_daily_activity" or not isinstance(activity, dict)
                or not activity.get("facts_digest")
                or activity["facts_digest"] != arguments.get("facts_digest")
                or activity.get("report_date") != arguments.get("report_date")):
            continue
        events = [{field: event[field] for field in ("id", "type", "date")} for event in activity["events"]]
        body = {"report_date": activity["report_date"], "events": events}
        return {**arguments, "text": json.dumps(body, ensure_ascii=False)}
    return arguments


_UNAVAILABLE_CODES = {ToolErrorCode.EXTERNAL_SERVICE_ERROR, ToolErrorCode.TIMEOUT}
_REQUIRED_TOOL_FIELDS = {
    "get_inventory": "inventory.quantity",
    "get_consumption_rules": "consumption_rules.units_per_appointment",
}


def _blocking_tool_result(name: str, error: ToolError, inventory_failures: int) -> RunResult | None:
    """Эскалирует сбой обязательного шага по коду инструмента."""
    reason_and_text = None
    if name in ("get_inventory", "calc_material_need") and error.code in _UNAVAILABLE_CODES:
        if inventory_failures >= 2:
            reason_and_text = ("inventory_unavailable",
                               "Не удалось получить актуальные остатки. Нужна проверка источника данных.")
    elif name == "create_order" and error.code == ToolErrorCode.TIMEOUT:
        reason_and_text = ("order_creation_timeout",
                           "Истекло время ожидания оформления заказа. Нужна сверка сохранённой операции.")
    elif name == "send_supplier_message" and error.code in _UNAVAILABLE_CODES:
        reason_and_text = ("supplier_message_unavailable",
                           "Сервис сообщений не подтвердил отправку. Нужна проверка перед повторной попыткой.")
    if reason_and_text is None:
        return None
    reason, text = reason_and_text
    return RunResult(action="escalate", text=text, facts={"reason": reason}, process_status="waiting")


@dataclass(frozen=True)
class LLMRunner:
    client: ModelClient
    model: str
    model_version: str
    temperature: float = 0.0
    prompt_version: str = PROMPT_VERSION
    max_rounds: int = 12
    max_request_chars: int = 60000
    max_verification_retries: int = 1
    input_price_per_million: Decimal | None = None
    output_price_per_million: Decimal | None = None

    def __post_init__(self):
        if not self.model or not self.model_version or not self.prompt_version or self.max_rounds < 1 or self.max_request_chars < 1000:
            raise ValueError("Model, version, prompt version and positive max_rounds are required")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if type(self.max_verification_retries) is not int or self.max_verification_retries < 0:
            raise ValueError("max_verification_retries must be a nonnegative integer")
        if any(rate is not None and (not isinstance(rate, Decimal) or rate < 0)
               for rate in (self.input_price_per_million, self.output_price_per_million)):
            raise ValueError("Cost rates must be nonnegative Decimal values or None")

    def _record_completion(self, context: Any, response: dict[str, Any], duration: float, error_code=None):
        usage = response.get("usage") or {}
        usage_reported = "prompt_tokens" in usage and "completion_tokens" in usage
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        if type(prompt_tokens) is not int or prompt_tokens < 0 or type(completion_tokens) is not int or completion_tokens < 0:
            raise ValueError("Invalid model token usage")
        cost = None
        if usage_reported and self.input_price_per_million is not None and self.output_price_per_million is not None:
            cost = (self.input_price_per_million * prompt_tokens +
                    self.output_price_per_million * completion_tokens) / Decimal(1_000_000)
        context.trace.record_llm(model=self.model, model_version=self.model_version,
            prompt_version=self.prompt_version, input_tokens=prompt_tokens,
            output_tokens=completion_tokens, cost=cost, duration_seconds=duration,
            error_code=error_code)

    @staticmethod
    def _record_history(context: Any, msg: ChatMessage):
        if context.history is None:
            return
        stored = context.history.add(msg)
        if not stored.ok:
            raise RuntimeError("Could not persist conversation history")

    def _recover_incomplete_calls(self, context: Any, pending: list[ToolCall]) -> bool:
        """Закрывает оборванную группу и отмечает возможные незавершённые записи."""
        recovering_writes = False
        for call in pending:
            may_have_effect = may_have_written(call.function.name)
            recovering_writes |= may_have_effect
            self._record_history(context, ChatMessage(role="tool", tool_call_id=call.id,
                content=json.dumps({"ok": False, "error": (
                    "interrupted_operation_requires_reconciliation" if may_have_effect else "interrupted_read"),
                    "operation_outcome": "unknown" if may_have_effect else "failed"}, ensure_ascii=False)))
        return recovering_writes

    def run(self, request: Any, context: Any) -> RunResult:
        try:
            event_run = _EventRun(self, request, context)
            result = _WORKFLOW.invoke(
                {}, context=event_run, config={"recursion_limit": 2 * self.max_rounds + 2})
            return result["result"]
        except _RunComplete as completed:
            return completed.result
        except StepLimitExceeded:
            return RunResult(technical_status="limit_exceeded", process_status="error",
                             text="Harness step limit reached")
        except RunDeadlineExceeded:
            return RunResult(technical_status="timeout", process_status="error",
                             text="Harness time limit reached")
        except Exception:
            return RunResult(technical_status="error", process_status="error",
                             text="Runner persistence or orchestration error")


class _RunComplete(Exception):
    def __init__(self, result: RunResult):
        self.result = result


class _EventRun:
    """Per-event transcript, permissions and evidence shared by the graph's nodes."""

    def __init__(self, runner: LLMRunner, request: Any, context: Any):
        self.runner = runner
        self.context = context
        self.snapshot = context.state.load() if context.state is not None else None
        if self.snapshot is None:
            raise _RunComplete(RunResult(technical_status="error", process_status="error",
                             text="No trusted business state source"))
        self.view = business_context(self.snapshot)
        previous = []
        self.history_limited = False
        recovering_writes = False
        if context.history is not None:
            recovered = context.history.recent()
            if not recovered.ok:
                raise _RunComplete(RunResult(technical_status="error", process_status="error", text="History unavailable"))
            if recovered.data.pending_tool_calls:
                recovering_writes = self.runner._recover_incomplete_calls(context, recovered.data.pending_tool_calls)
                recovered = context.history.recent()
                if not recovered.ok or recovered.data.pending_tool_calls:
                    raise _RunComplete(RunResult(technical_status="error", process_status="error",
                                     text="Could not recover conversation history"))
            previous = [m.to_api() for m in recovered.data.messages]
            self.history_limited = recovered.data.omitted_partial_group
        self.event = _request_payload(request, context)
        self.scheduled_wakeup = self.event["kind"] == "scheduled_wakeup"
        model_event = {**self.event, "data": {}} if self.scheduled_wakeup else self.event
        self.event_text = json.dumps(json_value(model_event), ensure_ascii=False, allow_nan=False)
        self.runner._record_history(context, ChatMessage(role="user", content=self.event_text))
        self.supplier_statement = _supplier_statement(self.event)
        self.table_comparison = compare_offer_table(self.event, self.snapshot)
        if self.table_comparison:
            self.view["attached_offer_comparison"] = self.table_comparison
        self.proposal_restriction = None if recovering_writes else _proposed_purchase_restriction(self.event, self.snapshot)
        if self.proposal_restriction:
            self.view["proposed_order_policy_reason"] = self.proposal_restriction
        self.permissions = tool_permissions(self.event, self.snapshot, recovering_writes=recovering_writes)
        if self.proposal_restriction:
            self.permissions = ToolPermissions(frozenset(), frozenset())
        if self.permissions.approved_order:
            draft = approved_draft_context(self.snapshot, self.permissions.approved_order)
            if draft is not None:
                self.view["approved_draft"] = draft
        system_prompt = build_system_prompt(
            self.event,
            supplier_statement=bool(self.supplier_statement),
            table_comparison=bool(self.table_comparison),
            order_delivery_discrepancy=bool(self.view["order_offer_discrepancies"]),
        )
        model_event_text = self.event_text
        if self.supplier_statement:
            # This extraction measures the terms stated in the current quotation.
            self.view, previous, self.history_limited = {}, [], False
            if self.event["kind"] != "supplier_message":
                model_event_text = json.dumps(
                    {**self.event, "data": {"supplier_statement": self.supplier_statement}},
                    ensure_ascii=False, allow_nan=False)
        self.messages, self.history_end = build_model_messages(
            system_prompt, context, self.event["kind"], model_event_text, self.view, previous, self.history_limited)
        self.specifications = model_tools(context.tools.schemas)
        self.staged_submission_ids: set[str] = set()
        self.uncertain_effects = set()
        self.successful_tools: list[tuple[str, Any]] = []
        self.delivery_review: dict[str, Any] | None = None
        self.delivery_review_comparison_start = 0
        self.tool_error_codes: set[str] = set()
        self.missing_tool_fields: set[str] = set()
        self.inventory_failures = 0
        self.verification_retries = 0
        self.model_requests = 0
        self.envelope_retry_used = False

    def request_model(self) -> Literal["ask_model", "execute_tools", "finalize"]:
        context = self.context
        if self.model_requests == self.runner.max_rounds:
            raise _RunComplete(RunResult(technical_status="limit_exceeded", process_status="error",
                                         text="LLM round limit reached"))
        self.model_requests += 1
        review = self._delivery_review_candidate()
        available_tools = [] if self.verification_retries or self.supplier_statement or self.table_comparison else [
            spec for spec in self.specifications
            if self.permissions.expose(spec["function"]["name"], self.staged_submission_ids)
            or (review and spec["function"]["name"] == "request_approval")
        ]
        history_window = fit_model_context(
            self.messages, available_tools, view=self.view, history_end=self.history_end,
            history_limited=self.history_limited, max_request_chars=self.runner.max_request_chars)
        if history_window is None:
            raise _RunComplete(RunResult(technical_status="limit_exceeded", process_status="error",
                             text="Model context exceeds configured size limit"))
        self.history_end, self.history_limited = history_window
        context.step()  # Общий предел шагов; это не требование NUM=1.
        started = perf_counter()
        try:
            # После неподтверждённого финала разрешаем только исправить
            # ответ. Дополнительные вызовы бизнес-инструментов запрещены.
            response = self.runner.client.complete(model=self.runner.model, messages=self.messages,
                tools=available_tools, temperature=self.runner.temperature)
        except Exception as error:
            timed_out = isinstance(error, ModelTimeoutError)
            self.runner._record_completion(context, {}, perf_counter() - started,
                                           error_code="provider_timeout" if timed_out else "provider_error")
            persisted_tools = {"dispatch_order", "request_approval", "send_review", "process_supplier_reply"}
            if not self.uncertain_effects and any(name in persisted_tools for name, _ in self.successful_tools):
                snapshot = context.state.load()
                recovered = _verified_effect_result(
                    self.event, snapshot, self.successful_tools,
                    _confirmed_submission_facts(snapshot, self.successful_tools),
                    _pending_approval_reason(snapshot, self.successful_tools))
                if recovered is not None and recovered.metadata.get("recovery") in {
                        "verified_submission", "verified_review", "verified_approval", "verified_supplier_update"}:
                    self.runner._record_history(context, ChatMessage(role="assistant", content=recovered.text))
                    raise _RunComplete(recovered)
            raise _RunComplete(RunResult(technical_status="timeout" if timed_out else "error",
                             process_status="error", text="Model provider timeout" if timed_out else "Model provider failure"))
        duration = perf_counter() - started
        metrics = response.get("_restoka_metrics") if isinstance(response, dict) else None
        if isinstance(metrics, dict) and metrics.get("streamed") and hasattr(context.trace, "event"):
            context.trace.event("llm.stream", ttft_seconds=metrics.get("ttft_seconds"),
                                model=self.runner.model)
        try:
            assistant = response["choices"][0]["message"]
            if not isinstance(assistant, dict):
                raise ValueError("Invalid assistant message")
            if response["choices"][0].get("finish_reason") in ("length", "max_tokens"):
                _record_rejected_batch(context.trace, assistant.get("tool_calls"), "truncated_response")
                raise ValueError("Truncated model response")
        except (KeyError, IndexError, TypeError, ValueError):
            self.runner._record_completion(context, response, duration, error_code="invalid_response_envelope")
            if not self.envelope_retry_used and self.model_requests < self.runner.max_rounds:
                self.envelope_retry_used = True
                self.messages.append({"role": "user", "content":
                    "Предыдущий ответ оборвался или имел неверный формат. "
                    "Продолжи с последнего подтверждённого шага. Ответь кратко."})
                return "ask_model"
            raise _RunComplete(RunResult(technical_status="error", process_status="error",
                             text="Invalid or truncated model response"))
        calls = assistant.get("tool_calls") or []
        if calls and self.verification_retries:
            _record_rejected_batch(context.trace, calls, "final_verification")
            self.runner._record_completion(context, response, duration, error_code="unexpected_tool_call_during_verification")
            raise _RunComplete(RunResult(technical_status="error", process_status="error",
                             text="Модель попыталась вызвать инструмент при исправлении ответа"))
        if calls:
            try:
                parsed_calls = [ToolCall.model_validate(call) for call in calls]
                if len({call.id for call in parsed_calls}) != len(parsed_calls):
                    raise ValueError("Duplicate tool call ID")
            except (ValidationError, ValueError):
                _record_rejected_batch(context.trace, calls, "invalid_tool_call")
                self.runner._record_completion(context, response, duration, error_code="invalid_tool_call")
                raise _RunComplete(RunResult(technical_status="error", process_status="error",
                                 text="Invalid model tool call envelope"))
            self.runner._record_completion(context, response, duration)
            stored = ChatMessage(role="assistant", content=assistant.get("content"), tool_calls=parsed_calls)
            self.runner._record_history(context, stored)  # Сохраняем вызов до выполнения действий.
            self.messages.append(stored.to_api())
            self.pending_calls = parsed_calls
            return "execute_tools"
        self.last_response = response
        self.last_duration = duration
        self.last_content = assistant.get("content")
        return "finalize"

    def _wakeup_order_allowed(self, name: str, args: Any) -> bool:
        today = self.context.now.astimezone(ZoneInfo(self.context.timezone)).date().isoformat()
        if not isinstance(args, dict) or args.get("as_of") != today:
            return False
        if name == "prepare_draft":
            # need_from makes the purchase tools recheck actual uncovered demand.
            return args.get("need_from") == today
        return any(tool == "prepare_draft" and draft.get("id") == args.get("order_id")
                   for tool, draft in self.successful_tools)

    def _delivery_review_candidate(self) -> dict[str, Any] | None:
        if self.delivery_review is None:
            return None
        comparison = _current_supplier_comparison(self.successful_tools[self.delivery_review_comparison_start:])
        coverage = self.delivery_review
        if (not comparison or comparison.get("material") != coverage["material"]
                or comparison.get("required_by") != coverage["required_by"]
                or comparison.get("quantity") is None
                or Decimal(str(comparison["quantity"])) != Decimal(coverage["unconfirmed_order_quantity"])):
            return None
        if any(entry.get("suitability") in {"eligible", "requires_approval"}
               and (supplier := (entry.get("offer") or {}).get("supplier_id"))
               and supplier != coverage["supplier_id"]
               for entry in comparison.get("offers", [])):
            return coverage
        return None

    def execute_tools(self) -> RunResult | None:
        context = self.context
        blocked_result = None
        review = self._delivery_review_candidate()
        for call in self.pending_calls:
            try:
                args = json.loads(call.function.arguments)
            except (TypeError, ValueError):
                args = call.function.arguments  # Gateway фиксирует некорректные попытки.
            name = call.function.name
            reviewing_delivery = (name == "request_approval" and review is not None and isinstance(args, dict)
                                  and "order_id" in args and args["order_id"] is None
                                  and args.get("request_id") == review["review_request_id"])
            coverage = None
            operation_key = (name, str(args.get("submission_id") or args.get("operation_id") or "")
                             if isinstance(args, dict) else "")
            if blocked_result is not None:
                _record_blocked_tool(context.trace, name, "failed_previous_tool")
                tool_result = ToolResult(ok=False, error=ToolError(
                    code=ToolErrorCode.BUSINESS_RULE_VIOLATION,
                    message="Workflow stopped after a failed required operation",
                    operation_outcome="not_started"))
            elif self.supplier_statement or self.table_comparison or not self.permissions.allows(
                    name, args, self.staged_submission_ids) and not reviewing_delivery:
                _record_blocked_tool(context.trace, name, "permission_denied", critical=may_have_written(name))
                tool_result = ToolResult(ok=False, error=ToolError(
                    code=ToolErrorCode.BUSINESS_RULE_VIOLATION,
                    message="This tool operation is not available in the current workflow state",
                    operation_outcome="not_started"))
            elif (self.scheduled_wakeup and name in {"prepare_draft", "create_order"}
                  and not self._wakeup_order_allowed(name, args)):
                _record_blocked_tool(context.trace, name, "wakeup_scope_required", critical=True)
                tool_result = ToolResult(ok=False, error=ToolError(
                    code=ToolErrorCode.BUSINESS_RULE_VIOLATION,
                    message="Scheduled wakeup requires today's need_from and as_of on the draft, "
                            "and create_order must use the draft prepared in this wakeup.",
                    operation_outcome="not_started"))
            elif (name == "prepare_draft" and isinstance(args, dict)
                  and _needs_fresh_supplier_comparison(args, self.snapshot, self.successful_tools)):
                _record_blocked_tool(context.trace, name, "supplier_comparison_required")
                tool_result = ToolResult(ok=False, error=ToolError(
                    code=ToolErrorCode.MISSING_DATA,
                    message="Supplier terms changed. Run compare_suppliers for this material, "
                            "quantity, as_of and required_by before preparing the draft.",
                    operation_outcome="not_started"))
            else:
                if name == "send_review" and self.event["kind"] == "scheduled_review":
                    args = _scheduled_review_arguments(args, self.successful_tools)
                tool_result = context.tools.call(name, args)
                required_field = _REQUIRED_TOOL_FIELDS.get(name)
                if tool_result.ok:
                    if required_field is not None:
                        self.missing_tool_fields.discard(required_field)
                    self.uncertain_effects.discard(operation_key)
                    data = json_value(tool_result.data)
                    if name == "process_supplier_reply" and isinstance(data, dict):
                        state = context.state.load()
                        coverage = partial_order_coverage(state, data)
                        if coverage is not None:
                            if self.event["kind"] == "supplier_reply" and data.get("id") == self.event["data"].get("offer_id"):
                                review_id = "delivery-review-" + sha256(coverage["order_id"].encode()).hexdigest()
                                if not any(row.get("id") == review_id for row in _rows(state, "approval_requests")):
                                    coverage["review_request_id"] = review_id
                                    self.delivery_review = {**coverage, "material": data["material"],
                                                            "supplier_id": data["supplier_id"]}
                                    self.delivery_review_comparison_start = len(self.successful_tools) + 1
                            data = {**data, "order_coverage": coverage}
                    if reviewing_delivery:
                        self.delivery_review = None
                        review = None
                    self.successful_tools.append((name, data))
                    if name == "create_order" and isinstance(data, dict):
                        submission_id = data.get("submission_id")
                        if isinstance(submission_id, str):
                            self.staged_submission_ids.add(submission_id)
                    if name in ("get_inventory", "calc_material_need"):
                        self.inventory_failures = 0
                elif tool_result.error:
                    error = tool_result.error
                    self.tool_error_codes.add(error.code.value)
                    if required_field is not None and error.code == ToolErrorCode.MISSING_DATA:
                        self.missing_tool_fields.add(required_field)
                    if error.operation_outcome == "unknown":
                        self.uncertain_effects.add(operation_key)
                    if error.code in _UNAVAILABLE_CODES and (name == "get_inventory"
                            or name == "calc_material_need" and self.inventory_failures):
                        self.inventory_failures += 1
                    blocked_result = _blocking_tool_result(name, error, self.inventory_failures)
                    if (name == "create_order" and error.code == ToolErrorCode.TIMEOUT
                            and self.uncertain_effects == {operation_key}):
                        recovered = _reconciled_local_submission(context.state.load(), args)
                        if recovered is not None:
                            if hasattr(context.trace, "event"):
                                context.trace.event("local_submission_reconciled", **recovered)
                            blocked_result = RunResult(
                                action="act", process_status="waiting",
                                text="Проверил запись в базе. Намерение оформить заказ сохранено. Отправка поставщику не выполнялась.",
                                facts={**recovered, "reason": "order_creation_timeout"},
                                metadata={"recovery": "reconciled_local_submission"})
            content = _tool_message_result(tool_result, coverage=coverage)
            tool_msg = ChatMessage(role="tool", tool_call_id=call.id, content=content)
            self.runner._record_history(context, tool_msg)
            self.messages.append(tool_msg.to_api())
        if blocked_result is not None:
            self.runner._record_history(context, ChatMessage(role="assistant", content=blocked_result.text))
            return blocked_result
        return None

    def finalize(self) -> RunResult | None:
        context = self.context
        response, duration = self.last_response, self.last_duration
        if self.uncertain_effects:
            self.runner._record_completion(context, response, duration)
            text = ("Исход операции неизвестен. Требуется сверка по исходному "
                    "идентификатору; повторная отправка запрещена.")
            self.runner._record_history(context, ChatMessage(role="assistant", content=text))
            return RunResult(action="escalate", text=text, process_status="waiting",
                             metadata={"model": self.runner.model, "prompt_version": self.runner.prompt_version,
                                       "recovery": "unknown_external_outcome"})

        content = self.last_content
        if self.proposal_restriction:
            self.runner._record_completion(context, response, duration)
            text = _PROPOSAL_RESTRICTION_TEXT[self.proposal_restriction]
            self.runner._record_history(context, ChatMessage(role="assistant", content=text))
            facts = {"reason": self.proposal_restriction}
            if self.proposal_restriction == "approval_required_by_policy":
                facts["missing_fields"] = ["quantity"]
            return RunResult(action="escalate", process_status="waiting", text=text, facts=facts)
        can_repair = (self.verification_retries < self.runner.max_verification_retries
                      and self.model_requests < self.runner.max_rounds)
        final_content = content if isinstance(content, str) else None
        try:
            decision = FinalDecision.model_validate_json(final_content) if final_content is not None else None
            if decision is not None and self.supplier_statement:
                statement_facts = SupplierStatementFacts.model_validate_json(json.dumps(decision.facts))
        except ValidationError:
            decision = None
        if decision is None:
            error_code = "missing_final_decision" if final_content is None else "invalid_final_json"
            self.runner._record_completion(context, response, duration, error_code=error_code)
            if can_repair:
                self.verification_retries += 1
                _request_final_repair(self.messages, final_content, error_code, [])
                return None
            current_snapshot = context.state.load()
            submission_facts = _confirmed_submission_facts(current_snapshot, self.successful_tools)
            approval_reason = _pending_approval_reason(current_snapshot, self.successful_tools)
            update_result = _verified_effect_result(
                self.event, current_snapshot, self.successful_tools, submission_facts, approval_reason)
            if update_result is not None:
                self.runner._record_history(context, ChatMessage(role="assistant", content=update_result.text))
                return update_result
            if final_content is not None:
                text = "Модель не вернула ответ в ожидаемом формате. Требуется проверка."
                self.runner._record_history(context, ChatMessage(role="assistant", content=text))
                return RunResult(action="escalate", process_status="waiting", text=text,
                                 facts={"reason": "invalid_model_json"})
            return RunResult(technical_status="error", process_status="error",
                             text="Missing final decision")
        current_snapshot = context.state.load()
        if self.supplier_statement and not _unsafe_action_claim(decision.text, {}, [], current_snapshot):
            self.runner._record_completion(context, response, duration)
            text = f"По сообщению поставщика: {decision.text}"
            self.runner._record_history(context, ChatMessage(role="assistant", content=text))
            return RunResult(action=decision.action, process_status=decision.process_status, text=text,
                             facts=statement_facts.model_dump(mode="json", exclude_none=True),
                             metadata={"facts_source": "unverified_supplier_statement"})
        facts = json_value(decision.facts)
        if self.table_comparison and decision.action == "act" and not _unsafe_action_claim(
                decision.text, {}, [], current_snapshot):
            self.runner._record_completion(context, response, duration)
            self.runner._record_history(context, ChatMessage(role="assistant", content=decision.text))
            return RunResult(action="act", process_status=decision.process_status, text=decision.text,
                             facts=self.table_comparison, metadata={"facts_source": "attached_offer_table"})
        submission_facts = _confirmed_submission_facts(current_snapshot, self.successful_tools)
        facts.update(submission_facts)
        _include_single_material_quantities(facts, self.successful_tools)
        offer_sources = _offer_source_ids(self.successful_tools)
        _include_offer_sources(facts, offer_sources)
        recommended_supplier = _recommended_supplier(self.successful_tools, facts.get("recommended_offer_id"))
        if decision.action == "act" and recommended_supplier is not None:
            facts.setdefault("chosen_supplier", recommended_supplier)
        approval_reason = _pending_approval_reason(current_snapshot, self.successful_tools)
        if approval_reason is None and decision.action == "escalate" and self.event["kind"] == "user_message":
            data = self.event["data"]
            proposal = data.get("proposed_order") if isinstance(data, dict) else None
            policies = _rows(current_snapshot, "organization_policies")
            if (isinstance(proposal, dict) and proposal.get("material") and len(policies) == 1
                    and policies[0].get("mandatory_human_approval") is True):
                approval_reason = "approval_required_by_policy"
        if approval_reason is not None:
            facts["reason"] = approval_reason
        if decision.action in {"clarify", "escalate"} and self.missing_tool_fields:
            fields = facts.get("missing_fields", [])
            fields = fields if isinstance(fields, list) else []
            facts["missing_fields"] = fields + sorted(self.missing_tool_fields - {
                field for field in fields if isinstance(field, str)
            })
        unsupported_fields = _unverified_fact_fields(
            facts, current_snapshot, self.successful_tools, initial_snapshot=self.snapshot,
            approval_reason=approval_reason, tool_error_codes=self.tool_error_codes,
            offer_source_ids=offer_sources, observed_missing_fields=self.missing_tool_fields,
            action=decision.action)
        missing_action_evidence = decision.action == "act" and not self.successful_tools
        unsafe_action = _unsafe_action_claim(decision.text, facts, self.successful_tools, current_snapshot)
        unverified_amount = _unverified_text_amount(
            decision.text, facts, current_snapshot, self.successful_tools)
        if missing_action_evidence or unsupported_fields or unsafe_action or unverified_amount:
            self.runner._record_completion(context, response, duration, error_code="ungrounded_final_claim")
            reason = ("no_successful_tool_calls" if missing_action_evidence else
                      "unverified_fact_fields" if unsupported_fields else
                      "unverified_action_claim" if unsafe_action else "unverified_text_amount")
            if can_repair:
                self.verification_retries += 1
                _request_final_repair(self.messages, final_content, reason, unsupported_fields)
                return None
            update_result = _verified_effect_result(
                self.event, current_snapshot, self.successful_tools, submission_facts, approval_reason)
            if (update_result is None and self.event["kind"] == "user_message"
                    and not any(may_have_written(name) for name, _ in self.successful_tools)):
                if decision.action == "act" and reason == "unverified_action_claim" and not unverified_amount:
                    update_result = _verified_read_result(facts, current_snapshot, self.successful_tools)
                elif decision.action in {"act", "escalate"} and reason == "unverified_fact_fields":
                    if decision.action == "act" and _COMPARISON_SCOPE_FIELDS.isdisjoint(unsupported_fields):
                        update_result = _verified_comparison_result(self.successful_tools)
                    if (update_result is None and facts.get("order_id")
                            and {"order_id", "order_status", "approval_status"}.isdisjoint(unsupported_fields)
                            and any(name == "get_pending_orders" for name, _ in self.successful_tools)):
                        status = _verified_read_result(facts, current_snapshot, self.successful_tools)
                        if status is not None and (decision.action == "act"
                                                   or status.facts.get("approval_status") == "pending"):
                            update_result = status
                elif decision.action == "clarify" and reason == "unverified_fact_fields":
                    update_result = _verified_offer_clarification(facts, self.successful_tools)
            if update_result is not None:
                self.runner._record_history(context, ChatMessage(role="assistant", content=update_result.text))
                return update_result
            return RunResult(technical_status="error", process_status="error",
                             text="Невозможно подтвердить заявленный результат по данным системы.",
                             metadata={"verification_reason": reason, "proposed_action": decision.action,
                                       "unverified_fact_fields": unsupported_fields,
                                       "verification_retries": self.verification_retries})
        self.runner._record_completion(context, response, duration)
        if self.event["kind"] in {"time_tick", "scheduled_review"}:
            receipt = _review_receipt_facts(self.successful_tools)
            if receipt:
                facts["review_delivery"] = receipt
        discrepancies = business_context(current_snapshot)["order_offer_discrepancies"]
        if discrepancies:
            facts["order_offer_discrepancies"] = discrepancies
        text = decision.text
        if discrepancies and any(s in self.event_text.casefold() for s in ("статус", "срок", "достав", "заказ")):
            text += (" Важно: дата текущего предложения отличается от даты в сохранённом заказе; "
                     "изменение самого заказа не подтверждено.")
        final = RunResult(action="escalate" if approval_reason else decision.action,
                          text=text, facts=facts,
                          process_status="waiting" if approval_reason else decision.process_status,
                          metadata={"model": self.runner.model, "prompt_version": self.runner.prompt_version,
                                    "facts_source": "model_claims_checked_against_db_and_tools"})
        # Сохраняем проверенный текст ответа, а не сырой JSON модели.
        self.runner._record_history(context, ChatMessage(role="assistant", content=text))
        return final


class _GraphState(TypedDict, total=False):
    result: RunResult


def _ask_model(_state: _GraphState, runtime: Runtime[_EventRun]) -> Command[Literal["ask_model", "execute_tools", "finalize"]]:
    return Command(goto=runtime.context.request_model())


def _execute_tools(_state: _GraphState, runtime: Runtime[_EventRun]) -> Command[Literal["ask_model", "__end__"]]:
    result = runtime.context.execute_tools()
    if result is not None:
        return Command(update={"result": result}, goto=END)
    return Command(goto="ask_model")


def _finalize(_state: _GraphState, runtime: Runtime[_EventRun]) -> Command[Literal["ask_model", "__end__"]]:
    result = runtime.context.finalize()
    if result is not None:
        return Command(update={"result": result}, goto=END)
    return Command(goto="ask_model")


# PostgreSQL and ScenarioRunner own persistence and idempotency. The graph coordinates a single event.
_WORKFLOW = (
    StateGraph(_GraphState, context_schema=_EventRun)
    .add_node("ask_model", _ask_model)
    .add_node("execute_tools", _execute_tools)
    .add_node("finalize", _finalize)
    .add_edge(START, "ask_model")
    .compile()
)
