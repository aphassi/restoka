"""Fixed business workflows composed exclusively through ToolGateway calls."""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any

from harness.contracts import RunResult
from tools.contracts import ToolErrorCode
from .parsing import (NormalizedRequest, ParsedCommand, local_date, match_materials,
                      parse_dates, parse_delivery_date, parse_supplier_message)
from .templates import (clarify_material, render_daily_review,
                        supplier_clarification, unsupported_action)


def stable_id(context, event_id: str, purpose: str, *parts: str) -> str:
    identity = json.dumps([context.organization_id, event_id, purpose, *parts],
                          ensure_ascii=False, separators=(",", ":"))
    return "baseline:" + purpose + ":" + sha256(identity.encode()).hexdigest()


def tool_error_result(error, *, waiting: bool = False) -> RunResult:
    code = error.code
    facts = {"error": error.model_dump(mode="json")}
    if code in (ToolErrorCode.INVALID_ARGUMENTS, ToolErrorCode.MISSING_DATA,
                ToolErrorCode.NOT_FOUND):
        return RunResult(action="clarify", process_status="waiting" if waiting else "completed",
                         text=error.message, facts=facts)
    if code == ToolErrorCode.APPROVAL_REQUIRED:
        return RunResult(action="escalate", process_status="waiting",
                         text="Для продолжения требуется доверенное решение управляющего.", facts=facts)
    if code == ToolErrorCode.OPERATION_OUTCOME_UNKNOWN:
        return RunResult(action="escalate", process_status="waiting",
                         text="Результат внешней операции неизвестен; выполняется сверка без повторной отправки.",
                         facts=facts)
    if code in (ToolErrorCode.DATABASE_ERROR, ToolErrorCode.TIMEOUT,
                ToolErrorCode.EXTERNAL_SERVICE_ERROR, ToolErrorCode.INTERNAL_ERROR):
        return RunResult(action="escalate", technical_status="error", process_status="error",
                         text="Техническая ошибка не позволяет безопасно продолжить операцию.", facts=facts)
    if code == ToolErrorCode.DUPLICATE_OPERATION:
        return RunResult(action="escalate", process_status="waiting",
                         text="Обнаружена существующая операция; требуется сверить её состояние.", facts=facts)
    return RunResult(action="escalate", text=error.message, facts=facts)


def _material_mentioned(material: str, text: str) -> bool:
    """Match the exact name or Russian case inflection of a known material."""
    material = material.casefold().strip()
    text = text.casefold()
    if not material:
        return False
    if material in text:
        return True
    # E.g. the inventory item "шампунь" is referred to as "шампуня".
    # Avoid broad substring guessing for short materials and phrases.
    if len(material) >= 5 and material.endswith("ь"):
        import re
        return bool(re.search(r"\b" + re.escape(material[:-1]) + r"(?:я|ю|ем|е|и|ей)\b", text))
    return False


class BaselineWorkflows:
    def __init__(self, context, request: NormalizedRequest, *, horizon_days: int = 14):
        self.context = context
        self.request = request
        self.horizon_days = horizon_days

    def inventory(self) -> RunResult:
        inventory = self.context.tools.call("get_inventory")
        if not inventory.ok:
            return tool_error_result(inventory.error)

        matches = match_materials(self.request.text, [row["material"] for row in inventory.data])
        if len(matches) > 1:
            return RunResult(action="clarify", text=clarify_material())
        rows = [row for row in inventory.data if not matches or row["material"] == matches[0]]
        return RunResult(action="act", text="Остатки получены.", facts={"inventory": rows})

    def material_need(self, parsed: ParsedCommand) -> RunResult:
        scope = self._scope(parsed, require_material=False)
        if isinstance(scope, RunResult):
            return scope
        material, date_from, date_to, as_of = scope

        result = self.context.tools.call("calc_material_need", {
            "material": material, "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(), "as_of": as_of.isoformat(),
        })
        if not result.ok:
            return tool_error_result(result.error)
        return RunResult(action="act", text="Потребность и дефицит рассчитаны.",
                         facts=_material_need_facts(result.data, material))

    def compare_suppliers(self, parsed: ParsedCommand) -> RunResult:
        scope = self._scope(parsed, require_material=True)
        if isinstance(scope, RunResult):
            return scope
        material, _, required_by, as_of = scope
        quantity = parsed.quantity
        if quantity is None and self.request.data.get("quantity") is not None:
            try:
                quantity = Decimal(str(self.request.data["quantity"]))
            except (InvalidOperation, ValueError):
                quantity = None
        if quantity is None:
            return RunResult(action="clarify", text="Уточните требуемое количество материала.")

        result = self.context.tools.call("compare_suppliers", {
            "material": material, "quantity": str(quantity), "as_of": as_of.isoformat(),
            "required_by": required_by.isoformat(),
        })
        if not result.ok:
            return tool_error_result(result.error)
        action = "act" if result.data.recommended_offer_id else "escalate"
        text = ("Предложения сравнены." if action == "act"
                else "Подходящего подтверждённого предложения нет; требуется ручное решение.")
        
        return RunResult(action=action, text=text, facts=_comparison_facts(result.data))

    def purchase(self, parsed: ParsedCommand) -> RunResult:
        scope = self._scope(parsed, require_material=True)
        if isinstance(scope, RunResult):
            return scope
        material, date_from, required_by, as_of = scope

        need_result = self.context.tools.call("calc_material_need", {
            "material": material, "date_from": date_from.isoformat(),
            "date_to": required_by.isoformat(), "as_of": as_of.isoformat(),
        })
        if not need_result.ok:
            return tool_error_result(need_result.error)
        need = next((item for item in need_result.data.materials if item.material == material), None)
        if need is None or need.deficit <= 0:
            return RunResult(action="act", text="Дефицита нет, закупка не требуется.",
                             facts=_material_need_facts(need_result.data, material))
        if need.orders_to_review:
            return RunResult(action="escalate", process_status="waiting",
                             text="Есть незавершённые заказы по материалу; сначала нужно проверить их.",
                             facts={"orders_to_review": need.orders_to_review,
                                    "material": material, "deficit": need.deficit})

        comparison = self.context.tools.call("compare_suppliers", {
            "material": material, "quantity": str(need.deficit), "as_of": as_of.isoformat(),
            "required_by": required_by.isoformat(),
        })
        if not comparison.ok:
            return tool_error_result(comparison.error)
        if comparison.data.recommended_offer_id is None:
            return self._handle_unusable_offers(comparison.data, material)

        candidate = next(item for item in comparison.data.offers
                         if item.offer["id"] == comparison.data.recommended_offer_id)
        identifiers = self._purchase_ids(material, candidate.offer["id"])
        draft = self.context.tools.call("prepare_draft", {
            "offer_id": candidate.offer["id"], "material": material,
            "quantity": str(need.deficit), "as_of": as_of.isoformat(),
            "required_by": required_by.isoformat(), "need_from": date_from.isoformat(),
            "operation_id": identifiers["draft"], "idempotency_key": identifiers["draft_key"],
        })
        if not draft.ok:
            return tool_error_result(draft.error)

        needs_approval = (candidate.approval_required is not False
                          or candidate.automatic_ordering_check != "not_blocked")
        if needs_approval:
            reasons = candidate.approval_reasons + candidate.automatic_ordering_reasons
            approval = self.context.tools.call("request_approval", {
                "request_id": identifiers["approval"], "order_id": draft.data.id,
                "reason": ", ".join(sorted(set(reasons))) or "Manual purchase authorization",
            })
            if not approval.ok:
                return tool_error_result(approval.error, waiting=True)
            return RunResult(action="escalate", process_status="waiting",
                             text="Черновик создан и передан управляющему на согласование.",
                             facts={"order": draft.data, "approval": approval.data,
                                    "material": material, "quantity": need.deficit,
                                    "deficit": need.deficit})

        submit_id, submit_key = self._submission_ids(identifiers["draft"])
        return self._submit(draft.data.id, candidate.offer["id"], as_of, required_by,
                            submit_id, submit_key, facts={"material": material,
                            "quantity": need.deficit, "deficit": need.deficit})

    def manager_decision(self) -> RunResult:
        state = self._state()
        approval_id = self.request.data.get("approval_id")
        approvals = state.get("approval_requests", {}).get("rows", [])
        approval = next((row for row in approvals if row["id"] == approval_id), None)
        if approval is None:
            return RunResult(action="clarify", text="Связанная заявка на согласование не найдена.")
        if approval["status"] == "rejected":
            return RunResult(action="refuse", text="Управляющий отклонил закупку.",
                             facts={"approval_id": approval_id, "order_id": approval.get("order_id")})
        if approval["status"] != "approved" or not approval.get("order_id"):
            return RunResult(action="escalate", process_status="waiting",
                             text="Доверенное решение по заявке ещё не завершено.")

        event = self._draft_event(state, approval["order_id"])
        if event is None:
            return RunResult(action="escalate", text="Не удалось восстановить исходные условия черновика.")
        request = event["details"]["request"]
        submit_id, submit_key = self._submission_ids(request["operation_id"])
        current_date = local_date(self.context.now, self.context.timezone)
        return self._submit(approval["order_id"], request["offer_id"],
                            current_date, date.fromisoformat(request["required_by"]),
                            submit_id, submit_key)

    def external_result(self) -> RunResult:
        state = self._state()
        operation_id = self.request.data.get("operation_id")
        service = self.request.data.get("service")
        rows = state.get("business_events", {}).get("rows", [])
        matches = [row for row in rows if row["type"] in (
            "order_submission", "supplier_message", "daily_review")
            and row.get("details", {}).get("request", {}).get("operation_id") == operation_id]
        if len(matches) != 1:
            return RunResult(action="escalate", text="Не удалось однозначно найти внешнюю операцию.")
        event = matches[0]
        if service != "order":
            return RunResult(action="act", text="Результат отправки сообщения зарегистрирован.",
                             facts={"operation_id": operation_id, "state": event["details"].get("state")})

        result = self.context.tools.call("dispatch_order", {
            "submission_id": event["id"], "as_of": local_date(self.context.now, self.context.timezone).isoformat(),
        })
        if not result.ok:
            return tool_error_result(result.error, waiting=True)
        return RunResult(action="act", text="Статус отправки заказа подтверждён.",
                         facts={"order": result.data})

    def order_status(self) -> RunResult:
        # Reconcile a persisted uncertain operation, never stage a second order.
        state = self._state()
        submissions = [r for r in state.get("business_events", {}).get("rows", [])
                       if r.get("type") == "order_submission"
                       and (r.get("details") or {}).get("state") == "unknown"]
        if submissions:
            orders = {str(r.get("id")): r for r in state.get("purchase_orders", {}).get("rows", [])}
            candidates = []
            for sub in submissions:
                order = orders.get(str((sub.get("details") or {}).get("order_id")))
                material = str((order or {}).get("material") or "").casefold()
                if material and _material_mentioned(material, self.request.text):
                    candidates.append(sub)
            if not candidates and len(submissions) == 1:
                candidates = submissions
            if len(candidates) != 1:
                return RunResult(action="escalate", process_status="waiting",
                                 text="Не удалось однозначно определить отправку для сверки; требуется управляющий.")
            submission_id = candidates[0]["id"]
            checked = self.context.tools.call("dispatch_order", {
                "submission_id": submission_id,
                "as_of": local_date(self.context.now, self.context.timezone).isoformat(),
            })
            if not checked.ok:
                if checked.error.code == ToolErrorCode.OPERATION_OUTCOME_UNKNOWN:
                    return RunResult(action="escalate", process_status="waiting",
                                     text="Статус отправки поставщику остаётся неизвестным. "
                                          "Сверка выполнена без повторной отправки; требуется управляющий.",
                                     facts={"submission_id": submission_id, "submission_state": "unknown"})
                return tool_error_result(checked.error, waiting=True)
            return RunResult(action="act", text="Статус исходной отправки проверен.",
                             facts={"submission_id": submission_id, "order": checked.data})

        statuses = ["draft", "pending_approval", "submitted", "confirmed", "delivered"]
        result = self.context.tools.call("get_pending_orders", {"statuses": statuses})
        if not result.ok:
            return tool_error_result(result.error)
        return RunResult(action="act", text="Состояние заказов получено.", facts={"orders": result.data})

    def supplier_reply(self) -> RunResult:
        supplier_id = self.request.data.get("supplier_id")
        message_id = self.request.data.get("message_id")
        trusted = self.request.kind == "supplier_reply" and supplier_id and message_id
        offers = self.context.tools.call("get_supplier_offers")
        if not offers.ok:
            return tool_error_result(offers.error)
        materials = sorted({row["material"] for row in offers.data})

        if trusted:
            reply = self.context.tools.call("get_supplier_reply", {
                "supplier_id": supplier_id, "message_id": message_id,
            })
            if not reply.ok:
                return tool_error_result(reply.error)
            text = reply.data.text
            offer_id = reply.data.offer_id
        else:
            text = self.request.text
            offer_id = self.request.data.get("offer_id")

        parsed = parse_supplier_message(text, materials, self.context.now, self.context.timezone)
        facts = _supplier_facts(parsed)
        if not trusted:
            return RunResult(action="clarify" if parsed.ambiguities else "act",
                             process_status="waiting" if parsed.ambiguities else "completed",
                             text=("Ответ поставщика разобран, но для изменения условий требуется доверенное событие."
                                   if not parsed.ambiguities else "В ответе поставщика есть неоднозначные условия."),
                             facts=facts)

        current = next((row for row in offers.data if row["id"] == offer_id), None)
        if current is None:
            return RunResult(action="escalate", text="Связанное предложение поставщика не найдено.", facts=facts)
        if parsed.ambiguities or parsed.uncertain:
            received = self.context.tools.call("receive_supplier_reply", {
                "supplier_id": supplier_id, "message_id": message_id,
            })
            if not received.ok:
                return tool_error_result(received.error)
            reasons = []
            if "non_final_unit_price" in parsed.ambiguities:
                reasons.append("unit_price_rub")
            if "multiple_delivery_dates" in parsed.ambiguities:
                reasons.append("delivery_date")
            if any(item.startswith("multiple_") or item == "conflicting_confirmation"
                   for item in parsed.ambiguities):
                reasons.append("contradiction")
            if parsed.uncertain and not reasons:
                reasons.append("contradiction")
            return self._send_supplier_clarification(supplier_id, parsed.material, reasons, facts)

        parsed_terms = {
            "unit_price_rub": parsed.unit_price_rub,
            "delivery_fee_rub": parsed.delivery_fee_rub,
            "delivery_date": parsed.delivery_date,
        }
        missing_before_confirmation = [field for field, value in parsed_terms.items()
                                       if value is None and current[field] is None]
        if parsed.confirmed is True and missing_before_confirmation:
            received = self.context.tools.call("receive_supplier_reply", {
                "supplier_id": supplier_id, "message_id": message_id,
            })
            if not received.ok:
                return tool_error_result(received.error)
            return self._send_supplier_clarification(supplier_id, parsed.material,
                                                     missing_before_confirmation, facts)

        patch = _supplier_patch(parsed)
        if patch:
            expected = {
                key: value
                for key, value in current.items()
                if key not in ("organization_id", "supplier_approved")
            }
            for field in ("unit_price_rub", "delivery_fee_rub", "delivery_date"):
                if field in patch and str(patch[field]) == str(expected[field]):
                    del patch[field]
            processed = self.context.tools.call("process_supplier_reply", {
                "supplier_id": supplier_id, "message_id": message_id,
                "expected": expected, "changes": patch,
            })
            if not processed.ok:
                return tool_error_result(processed.error)
            facts["offer"] = processed.data
            current = processed.data
        else:
            received = self.context.tools.call("receive_supplier_reply", {
                "supplier_id": supplier_id, "message_id": message_id,
            })
            if not received.ok:
                return tool_error_result(received.error)

        missing = []
        for field in ("unit_price_rub", "delivery_fee_rub", "delivery_date"):
            value = current[field] if isinstance(current, dict) else getattr(current, field)
            if value is None:
                missing.append(field)
        confirmed = current["confirmed"] if isinstance(current, dict) else current.confirmed
        if not confirmed:
            missing.append("confirmation")
        if missing:
            return self._send_supplier_clarification(supplier_id, parsed.material, missing, facts)
        continued = self._continue_procurement(supplier_id, parsed.material)
        if continued is not None:
            continued.facts["supplier_reply"] = facts
            for key, value in facts.items():
                if key != "supplier_message":
                    continued.facts.setdefault(key, value)
            return continued
        return RunResult(action="act", text="Подтверждённые условия поставщика сохранены.", facts=facts)

    def supplier_delay(self, parsed: ParsedCommand) -> RunResult:
        delay_date = parse_delivery_date(self.request.text, self.context.now,
                                         self.context.timezone)
        if delay_date is None:
            return RunResult(action="clarify", process_status="waiting",
                             text="Уточните однозначную новую дату доставки в уведомлении о задержке.")

        scope = self._scope(ParsedCommand("supplier_delay"), require_material=True)
        if isinstance(scope, RunResult):
            return scope
        material, date_from, date_to, as_of = scope

        offers = self.context.tools.call("get_supplier_offers", {"material": material})
        if not offers.ok:
            return tool_error_result(offers.error)
        orders = self.context.tools.call("get_pending_orders", {
            "material": material,
            "statuses": ["draft", "pending_approval", "submitted", "confirmed"],
        })
        if not orders.ok:
            return tool_error_result(orders.error)

        offer_id = self.request.data.get("offer_id")
        order_id = self.request.data.get("order_id")
        supplier_id = self.request.data.get("supplier_id")
        links = []
        if isinstance(offer_id, str):
            links.extend(("offer", row["id"]) for row in offers.data
                         if row["id"] == offer_id and row["confirmed"]
                         and row["delivery_date"] == delay_date)
        if isinstance(order_id, str):
            links.extend(("order", order.id) for order in orders.data
                         if order.id == order_id and order.status == "confirmed"
                         and order.eta == delay_date)
        if not offer_id and not order_id and isinstance(supplier_id, str):
            links.extend(("offer", row["id"]) for row in offers.data
                         if row["supplier_id"] == supplier_id and row["confirmed"]
                         and row["delivery_date"] == delay_date)

        reflected = len(links) == 1
        delay_facts = {"material": material, "delivery_date": delay_date,
                       "delay_reflected": reflected}
        if not reflected:
            return RunResult(action="escalate", process_status="waiting",
                             text=("Задержка указана только в сообщении и ещё не подтверждена "
                                   "в бизнес-данных; условия поставки не изменены."),
                             facts=delay_facts)
        delay_facts["linked_record_type"], delay_facts["linked_record_id"] = links[0]

        date_to = max(date_to, delay_date)
        result = self.context.tools.call("calc_material_need", {
            "material": material, "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(), "as_of": as_of.isoformat(),
        })
        if not result.ok:
            return tool_error_result(result.error)
        facts = _material_need_facts(result.data, material)
        facts.update(delay_facts)
        return RunResult(action="act", text="Потребность пересчитана по подтверждённым данным.",
                         facts=facts)

    def daily_review(self, parsed: ParsedCommand) -> RunResult:
        today = local_date(self.context.now, self.context.timezone)
        report_date = parsed.date_to
        if report_date is None and isinstance(self.request.data.get("report_date"), str):
            try:
                report_date = date.fromisoformat(self.request.data["report_date"])
            except ValueError:
                return RunResult(action="clarify", text="Дата отчёта должна быть в формате YYYY-MM-DD.")
        report_date = report_date or today - timedelta(days=1)
        facts = self.context.tools.call("get_daily_activity", {"report_date": report_date.isoformat()})
        if not facts.ok:
            return tool_error_result(facts.error)
        text = render_daily_review(facts.data)
        sent = self.context.tools.call("send_review", {
            "report_date": report_date.isoformat(), "text": text,
            "operation_id": stable_id(self.context, self.request.event_id, "daily-review", report_date.isoformat()),
            "facts_digest": facts.data.facts_digest,
        })
        if not sent.ok:
            return tool_error_result(sent.error, waiting=True)
        return RunResult(action="act", text="Ежедневный отчёт сформирован и отправлен.",
                         facts={"activity": facts.data, "delivery": sent.data})

    def unsupported(self) -> RunResult:
        # Explain why cancellation is unsafe without an external receipt.
        import re
        lowered = self.request.text.casefold()
        if re.search(r"\b(?:отмен\w*|аннулир\w*|cancel\w*)\b", lowered) and re.search(
            r"\b(?:заказ\w*|закупк\w*|order)\b", lowered
        ):
            state = self._state()
            submissions = [row for row in state.get("business_events", {}).get("rows", [])
                           if row.get("type") == "order_submission"
                           and (row.get("details") or {}).get("state") == "unknown"]
            orders = {str(row.get("id")): row for row in
                      state.get("purchase_orders", {}).get("rows", [])}
            matches = []
            for sub in submissions:
                order = orders.get(str((sub.get("details") or {}).get("order_id")))
                material = str((order or {}).get("material") or "").casefold()
                if material and _material_mentioned(material, lowered):
                    matches.append(sub)
            if len(matches) == 1:
                return RunResult(action="escalate", process_status="waiting",
                                 text="Отмена заказа не подтверждена. Статус отправки поставщику "
                                      "остаётся неизвестным. Автоматическая отмена не выполнялась; "
                                      "требуется вмешательство управляющего.",
                                 facts={"submission_id": matches[0]["id"],
                                        "submission_state": "unknown", "cancellation_confirmed": False})
        return RunResult(action="refuse", text=unsupported_action())

    def no_purchase(self) -> RunResult:
        return RunResult(action="act", text="Закупка не запущена по явному указанию пользователя.")

    def unsupported_table(self) -> RunResult:
        return RunResult(action="clarify",
                         text="Этот baseline не обрабатывает произвольную таблицу как источник предложений.")

    def _scope(self, parsed: ParsedCommand, *, require_material: bool):
        inventory = self.context.tools.call("get_inventory")
        if not inventory.ok:
            return tool_error_result(inventory.error)
        materials = sorted({row["material"] for row in inventory.data})
        offers = self.context.tools.call("get_supplier_offers")
        if offers.ok:
            materials = sorted(set(materials) | {row["material"] for row in offers.data})
        requested_material = self.request.data.get("material")
        if isinstance(requested_material, str) and requested_material in materials:
            matches = [requested_material]
        else:
            matches = match_materials(self.request.text, materials)
        if len(matches) > 1 or require_material and len(matches) != 1:
            return RunResult(action="clarify", text=clarify_material(), facts={"materials": materials})
        material = matches[0] if matches else None

        today = local_date(self.context.now, self.context.timezone)
        explicit_dates = [value for value in (parsed.date_from, parsed.date_to) if value is not None]
        if len(set(explicit_dates)) >= 2:
            date_from, date_to = min(explicit_dates), max(explicit_dates)
        elif explicit_dates:
            date_from, date_to = today, explicit_dates[0]
        else:
            date_from, date_to = today, today + timedelta(days=self.horizon_days)
        if date_to < date_from:
            return RunResult(action="clarify", text="Указанный срок уже прошёл или период задан неверно.")

        relevant = [row for row in inventory.data if material is None or row["material"] == material]
        if not relevant:
            return RunResult(action="clarify", text="Для материала нет наблюдения об остатках.")
        as_of_values = {row["as_of"] for row in relevant}
        if len(as_of_values) != 1:
            return RunResult(action="escalate", text="Даты наблюдения остатков неоднозначны.")
        as_of = next(iter(as_of_values))
        if as_of > date_from:
            return RunResult(action="clarify", text="Период начинается раньше доступного наблюдения остатков.")
        return material, date_from, date_to, as_of

    def _handle_unusable_offers(self, comparison, material: str) -> RunResult:
        incomplete = [item for item in comparison.offers if item.suitability == "incomplete"]
        identifiable = [item for item in incomplete if item.offer.get("supplier_id")]
        if len(identifiable) == 1:
            item = identifiable[0]
            reasons = []
            if "unknown_delivery_date" in item.reasons:
                reasons.append("delivery_date")
            if "unknown_unit_price_rub" in item.reasons:
                reasons.append("unit_price_rub")
            if "unknown_delivery_fee_rub" in item.reasons:
                reasons.append("delivery_fee_rub")
            if "offer_not_confirmed" in item.reasons:
                reasons.append("confirmation")
            if reasons:
                return self._send_supplier_clarification(item.offer["supplier_id"], material, reasons,
                                                         {"comparison": comparison}, comparison=comparison)
        return RunResult(action="escalate", process_status="waiting",
                         text="Ни один поставщик не подтвердил подходящую поставку в срок.",
                         facts={"comparison": comparison})

    def _send_supplier_clarification(self, supplier_id: str, material: str | None,
                                     reasons: list[str], facts: dict[str, Any], *, comparison=None) -> RunResult:
        quantity = comparison.quantity if comparison is not None else None
        required_by = comparison.required_by if comparison is not None else None
        text = supplier_clarification(reasons, material, quantity=quantity, required_by=required_by)
        purpose = "procurement-clarification" if comparison is not None else "supplier-clarification"
        operation_id = stable_id(self.context, self.request.event_id, purpose,
                                 supplier_id, material or "unknown", ",".join(sorted(reasons)))
        sent = self.context.tools.call("send_supplier_message", {
            "supplier_id": supplier_id, "text": text, "operation_id": operation_id,
        })
        if not sent.ok:
            return tool_error_result(sent.error, waiting=True)
        facts["delivery"] = sent.data
        return RunResult(action="act", process_status="waiting",
                         text="Поставщику отправлен запрос на уточнение условий.", facts=facts)

    def _continue_procurement(self, supplier_id: str, material: str | None) -> RunResult | None:
        if material is None:
            return None
        rows = self._state().get("business_events", {}).get("rows", [])
        requests = []
        for row in rows:
            details = row.get("details", {})
            request = details.get("request", {})
            if (row.get("type") == "supplier_message"
                    and request.get("destination") == supplier_id
                    and str(request.get("operation_id", "")).startswith("baseline:procurement-clarification:")
                    and material.casefold() in str(request.get("body", "")).casefold()):
                requests.append(request)
        if not requests:
            return None

        body = requests[-1]["body"]
        dates = parse_dates(body, self.context.now, self.context.timezone)
        required_by = dates[-1] if dates else None
        resumed = NormalizedRequest("user_message", self.request.event_id,
                                    {"material": material}, f"Закупи {material}")
        parsed = ParsedCommand("purchase", date_to=required_by)
        return BaselineWorkflows(self.context, resumed,
                                 horizon_days=self.horizon_days).purchase(parsed)

    def _submit(self, order_id: str, offer_id: str, as_of: date, required_by: date,
                operation_id: str, idempotency_key: str,
                facts: dict[str, Any] | None = None) -> RunResult:
        staged = self.context.tools.call("create_order", {
            "order_id": order_id, "offer_id": offer_id, "as_of": as_of.isoformat(),
            "required_by": required_by.isoformat(), "operation_id": operation_id,
            "idempotency_key": idempotency_key,
        })
        if not staged.ok:
            return tool_error_result(staged.error, waiting=True)
        dispatched = self.context.tools.call("dispatch_order", {
            "submission_id": staged.data.submission_id, "as_of": as_of.isoformat(),
        })
        if not dispatched.ok:
            return tool_error_result(dispatched.error, waiting=True)
        result_facts = dict(facts or {})
        result_facts.update(order=dispatched.data, submission_id=staged.data.submission_id)
        return RunResult(action="act", text="Заказ проверен и отправлен поставщику.",
                         facts=result_facts)

    def _purchase_ids(self, material: str, offer_id: str) -> dict[str, str]:
        base = stable_id(self.context, self.request.event_id, "purchase", material, offer_id)
        return {
            "draft": base + ":draft", "draft_key": base + ":draft-key",
            "approval": base + ":approval",
        }

    def _submission_ids(self, draft_operation_id: str) -> tuple[str, str]:
        base = stable_id(self.context, draft_operation_id, "submit", draft_operation_id)
        return base, base + ":key"

    def _state(self) -> dict[str, Any]:
        return self.context.state.load() if self.context.state is not None else {}

    @staticmethod
    def _draft_event(state: dict[str, Any], order_id: str):
        for row in state.get("business_events", {}).get("rows", []):
            if row["type"] == "order_draft_prepared" and row.get("details", {}).get("result", {}).get("id") == order_id:
                return row
        return None


def _supplier_patch(parsed) -> dict[str, Any]:
    patch = {}
    for field in ("quantity", "unit_price_rub", "delivery_fee_rub", "delivery_date",
                  "confirmed", "confirmed_quantity"):
        value = getattr(parsed, field)
        if value is not None:
            patch[field] = value.isoformat() if isinstance(value, date) else str(value) if isinstance(value, Decimal) else value
    return patch


def _material_need_facts(result, material: str | None) -> dict[str, Any]:
    facts = {"material_need": result}
    need = next((item for item in result.materials if item.material == material), None)
    if need is None and material is None and len(result.materials) == 1:
        need = result.materials[0]
    if need is not None:
        facts.update(material=need.material, required_quantity=need.required_quantity,
                     deficit=need.deficit, first_shortage_date=need.first_shortage_date)
    elif material is not None and not result.materials:
        facts.update(material=material, deficit=Decimal("0"))
    return facts


def _comparison_facts(comparison) -> dict[str, Any]:
    facts = {"comparison": comparison, "material": comparison.material,
             "quantity": comparison.quantity,
             "recommended_offer_id": comparison.recommended_offer_id}
    recommended = next((item for item in comparison.offers
                        if item.offer["id"] == comparison.recommended_offer_id), None)
    if recommended is not None:
        facts.update(unit_price_rub=recommended.offer["unit_price_rub"],
                     delivery_date=recommended.offer["delivery_date"],
                     total_rub=recommended.total_rub)
    return facts


def _supplier_facts(parsed) -> dict[str, Any]:
    facts = {"supplier_message": _json(parsed)}
    for field in ("material", "quantity", "unit", "unit_price_rub", "delivery_fee_rub",
                  "delivery_date", "available", "confirmed", "confirmed_quantity",
                  "remaining_confirmed"):
        value = getattr(parsed, field)
        if value is not None:
            facts[field] = value
    return facts


def _json(value) -> dict[str, Any]:
    return {key: (item.isoformat() if isinstance(item, date) else str(item) if isinstance(item, Decimal)
                  else list(item) if isinstance(item, tuple) else item)
            for key, item in value.__dict__.items()}
