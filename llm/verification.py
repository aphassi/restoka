"""Сверка утверждений модели со снимком состояния и результатами инструментов."""
from __future__ import annotations

from decimal import Decimal
import re
from typing import Any

from harness.trace import json_value
from .context import _rows


def _scalar(value: Any) -> str:
    """Сравнивает значения, не смешивая логические значения с числами."""
    if isinstance(value, bool):
        return "boolean:" + str(value)
    if isinstance(value, (int, float, Decimal)) or (isinstance(value, str) and value.strip()):
        try:
            return "number:" + str(Decimal(str(value)).normalize())
        except (ValueError, ArithmeticError):
            pass
    return "text:" + str(value).strip().casefold()


def _needs_fresh_supplier_comparison(args: dict[str, Any], snapshot: dict[str, Any],
                                      successful_tools: list[tuple[str, Any]]) -> bool:
    material = args.get("material")
    changes = [(row.get("details") or {}).get("result") or {}
               for row in _rows(snapshot, "business_events") if row.get("type") == "supplier_offer_updated"]
    if not material or not any((change.get("data", change) or {}).get("material") == material
                               for change in changes):
        return False
    scope = ("material", "quantity", "as_of", "required_by")
    return not any(name == "compare_suppliers" and isinstance(data, dict)
                   and all(_scalar(data.get(key)) == _scalar(args.get(key)) for key in scope)
                   for name, data in successful_tools)


def _fact_fields(obj: Any, group: str | None = None):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, (dict, list)):
                yield from _fact_fields(value, str(key))
            else:
                yield group, obj, str(key), value
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                yield from _fact_fields(item, group)
            elif group is not None:
                yield group, {}, group, item


def _flat_pairs(obj: Any):
    return ((key, value) for _, _, key, value in _fact_fields(obj))


# Разные названия одного расчётного показателя в финальном JSON.
# Алиасы применяются только после успешного calc_material_need, а не к
# статусам заказов, согласованиям или операциям с внешним эффектом.
CALC_FACT_ALIASES = {
    "need": "required_quantity",
    "needed": "required_quantity",
    "demand": "required_quantity",
    "total_need": "required_quantity",
    "required": "required_quantity",
    "stock": "quantity",
    "stock_quantity": "quantity",
    "inventory_quantity": "quantity",
    "available_quantity": "quantity",
    "available": "quantity",
    "shortage": "deficit",
    "shortage_quantity": "deficit",
    "deficit_quantity": "deficit",
    "shortfall": "deficit",
    "shortage_date": "first_shortage_date",
    "period_start": "date_from",
    "period_end": "date_to",
    "from": "date_from",
    "to": "date_to",
}

ORDER_FACT_ALIASES = {
    "order_eta": "eta",
    "order_quantity": "quantity",
    "order_status": "status",
    "order_total_rub": "total_rub",
}
_ORDER_RECORDS = {"order", "orders", "purchase_orders", "draft_order", "draft", "incoming_orders"}
_ORDER_FIELDS = {"id", "material", "supplier_id", "quantity", "eta", "status", "total_rub",
                 "unit_price_rub", "delivery_fee_rub", "operation_id", "idempotency_key"}
_ID_FIELDS = {
    "purchase_orders": "order_id",
    "supplier_offers": "offer_id",
    "approval_requests": "approval_id",
}
_DIAGNOSTIC_LISTS = {"missing_data", "missing_fields"}
_APPROVAL_STATES = {"pending", "approved", "rejected"}

APPROVAL_FACT_ALIASES = {
    "approval_request_id": "id",
    "approval_status": "status",
    "approval_decision": "decision",
}


def _referenced_order(facts: dict[str, Any], snapshot: dict[str, Any],
                      successful_tools: list[tuple[str, Any]]) -> dict[str, Any] | None:
    evidence_ids = set()
    for name, data in successful_tools:
        if name in ("get_pending_orders", "get_daily_activity"):
            records = data.get("orders", []) if isinstance(data, dict) else data
        elif name in ("dispatch_order", "prepare_draft"):
            records = [data]
        elif name == "create_order" and isinstance(data, dict):
            records = [data.get("order")]
        else:
            continue
        if isinstance(records, list):
            evidence_ids.update(str(row["id"]) for row in records
                                if isinstance(row, dict) and row.get("id") is not None)
    claimed_ids = {str(value) for key, value in _flat_pairs(facts)
                   if key == "order_id" and value is not None}
    candidates = [row for row in _rows(snapshot, "purchase_orders")
                  if str(row.get("id")) in evidence_ids
                  and (not claimed_ids or str(row.get("id")) in claimed_ids)]
    if len(candidates) == 1 and len(claimed_ids) <= 1:
        return candidates[0]
    return None


def _has_recorded_order(facts: dict[str, Any], initial_snapshot: dict[str, Any],
                        successful_tools: list[tuple[str, Any]]) -> bool:
    reads = [(name, data) for name, data in successful_tools if name == "get_pending_orders"]
    return _referenced_order(facts, initial_snapshot, reads) is not None


def _referenced_offer(facts: dict[str, Any], order: dict[str, Any] | None,
                      successful_tools: list[tuple[str, Any]]) -> dict[str, Any] | None:
    offers = {}
    for name, data in successful_tools:
        if name == "get_supplier_offers" and isinstance(data, list):
            records = data
        elif name == "process_supplier_reply":
            records = [data]
        else:
            continue
        offers.update((offer["id"], offer) for offer in records
                      if isinstance(offer, dict) and offer.get("id"))
    offer_id = facts.get("offer_id")
    if isinstance(offer_id, str):
        return offers.get(offer_id)
    if order:
        offers = {key: offer for key, offer in offers.items()
                  if all(offer.get(field) == order.get(field) for field in ("material", "supplier_id"))}
    return next(iter(offers.values())) if len(offers) == 1 else None


def _offer_source_ids(successful_tools: list[tuple[str, Any]]) -> set[str]:
    identifiers = set()
    for name, data in successful_tools:
        if name == "get_supplier_offers" and isinstance(data, list):
            offers = data
        elif name == "compare_suppliers" and isinstance(data, dict):
            offers = [item.get("offer") for item in data.get("offers", []) if isinstance(item, dict)]
        else:
            continue
        identifiers.update(offer["id"] for offer in offers if isinstance(offer, dict)
                           and isinstance(offer.get("id"), str))
    return identifiers


def _include_offer_sources(facts: dict[str, Any], offer_source_ids: set[str]) -> None:
    if not offer_source_ids or "source_ids" in facts:
        return
    cited = {value for group, _, key, value in _fact_fields(facts)
             if isinstance(value, str) and (key in {"offer_id", "recommended_offer_id"}
                 or key == "id" and group in {"offer", "offers", "supplier_offers"})}
    if sources := cited & offer_source_ids:
        facts["source_ids"] = sorted(sources)


def _reconciled_local_submission(snapshot: dict[str, Any], args: Any) -> dict[str, Any] | None:
    keys = ("order_id", "offer_id", "operation_id", "idempotency_key", "as_of", "required_by")
    if not isinstance(args, dict) or not all(isinstance(args.get(key), str) and args[key] for key in keys):
        return None
    order = next((row for row in _rows(snapshot, "purchase_orders") if row.get("id") == args["order_id"]), None)
    if not order or order.get("status") not in {"draft", "pending_approval"}:
        return None
    for row in _rows(snapshot, "business_events"):
        details = row.get("details") or {}
        request = details.get("request") or {}
        if (row.get("type") == "order_submission" and details.get("state") == "prepared"
                and details.get("order_id") == order["id"] and row.get("id")
                and all(request.get(key) == args[key] for key in keys)):
            return {"order_id": order["id"], "submission_id": row["id"],
                    "operation_id": args["operation_id"], "order_status": order["status"]}
    return None


def _confirmed_submission_facts(snapshot: dict[str, Any],
                                successful_tools: list[tuple[str, Any]]) -> dict[str, Any]:
    """Связывает результат отправки с сохранённым намерением и заказом."""
    if any(name in {"request_approval", "send_supplier_message", "send_review", "process_supplier_reply"}
           for name, _ in successful_tools):
        return {}
    staged = {data.get("submission_id") for name, data in successful_tools
              if name == "create_order" and isinstance(data, dict)}
    dispatched = {data.get("id") for name, data in successful_tools
                  if name == "dispatch_order" and isinstance(data, dict)
                  and data.get("status") in {"submitted", "confirmed"}}
    submissions = [row for row in _rows(snapshot, "business_events")
                   if row.get("type") == "order_submission" and row.get("id") in staged
                   and row["details"].get("state") == "sent"
                   and row["details"].get("order_id") in dispatched]
    if len(submissions) != 1:
        return {}
    submission = submissions[0]
    details = submission["details"]
    order = next((row for row in _rows(snapshot, "purchase_orders")
                  if row.get("id") == details.get("order_id")), {})
    if order.get("status") not in {"submitted", "confirmed"}:
        return {}
    return {key: value for key, value in {
        "order_id": order.get("id"), "submission_id": submission["id"],
        "operation_id": details.get("request", {}).get("operation_id"),
        "order_status": order.get("status"), "order_total_rub": order.get("total_rub"),
    }.items() if value is not None}


def _unverified_fact_fields(facts: dict[str, Any], snapshot: dict[str, Any],
                            successful_tools: list[tuple[str, Any]],
                            initial_snapshot: dict[str, Any] | None = None, *,
                            approval_reason: str | None = None,
                            tool_error_codes: set[str] | frozenset[str] = frozenset(),
                            offer_source_ids: set[str] | frozenset[str] = frozenset(),
                            observed_missing_fields: set[str] | frozenset[str] = frozenset(),
                            action: str | None = None) -> list[str]:
    """Возвращает названия полей, для которых нет подтверждения в данных.

    approval_reason вычисляется по тому же снимку и результатам инструментов.
    """
    if not facts:
        return []
    pairs = {("error_code", _scalar(code)) for code in tool_error_codes}
    pairs.update(("missing_fields", _scalar(field)) for field in observed_missing_fields)
    if approval_reason is not None:
        pairs.add(("reason", _scalar(approval_reason)))
    if action == "refuse" and not successful_tools:
        pairs.add(("reason", _scalar("out_of_domain")))
    if not successful_tools:
        return sorted({key for group, record, key, value in _fact_fields(facts)
                       if (key, _scalar(value)) not in pairs
                       and not (action in {"clarify", "escalate"} and group == key
                                and not record and key in _DIAGNOSTIC_LISTS and isinstance(value, str))})
    pairs.update(("source_ids", _scalar(offer_id)) for offer_id in offer_source_ids)
    for table, body in snapshot.items():
        if isinstance(body, dict) and isinstance(body.get("rows"), list):
            for row in body["rows"]:
                if not isinstance(row, dict):
                    continue
                pairs.update((key.casefold(), _scalar(value)) for key, value in _flat_pairs(row)
                             if value is not None and not isinstance(value, (dict, list)))
                id_field = _ID_FIELDS.get(table)
                if table == "business_events" and row.get("type") == "order_submission" and row.get("id"):
                    id_field = "submission_id"
                if id_field and row.get("id"):
                    pairs.update(((id_field, _scalar(row["id"])), (id_field + "s", _scalar(row["id"]))))
    for name, result in successful_tools:
        for group, record, key, value in _fact_fields(json_value(result)):
            if value is None:
                continue
            normalized = _scalar(value)
            field = key.casefold()
            pairs.add((field, normalized))
            pairs.add((f"{name}.{field}", normalized))
            if name == "calc_material_need" and group == "incoming_orders" and key == "id":
                pairs.add(("incoming_orders", normalized))
            if name == "get_daily_activity" and group == "events" and key == "id":
                pairs.add(("event_ids", normalized))
            if (name == "get_inventory" and key == "material" and record.get("quantity") is not None
                    and Decimal(str(record["quantity"])) > 0):
                pairs.add(("available_materials", normalized))
            if name == "get_supplier_offers" and key == "supplier_id" and record.get("supplier_approved") is True:
                pairs.add(("approved_suppliers_with_offers", normalized))

    # inventory_as_of — имя поля итогового ответа, а в базе и инструментах
    # дата остатков называется as_of. Проверяем именно источник остатков,
    # не произвольное поле as_of у другой сущности.
    inventory_dates = {
        _scalar(row["as_of"])
        for row in _rows(snapshot, "inventory")
        if isinstance(row, dict) and row.get("as_of") is not None
    }
    for name, data in successful_tools:
        if name == "get_inventory" and isinstance(data, list):
            inventory_dates.update(_scalar(row["as_of"]) for row in data
                                   if isinstance(row, dict) and row.get("as_of") is not None)
        if name == "calc_material_need" and isinstance(data, dict):
            for material in data.get("materials", []):
                if not isinstance(material, dict):
                    continue
                inventory = material.get("inventory")
                if isinstance(inventory, dict) and inventory.get("as_of") is not None:
                    inventory_dates.add(_scalar(inventory["as_of"]))

    # Полное состояние таблицы purchase_orders — надёжное доказательство
    # отсутствия любых ожидающих заказов. Но пустой orders_to_review
    # из расчёта сам по себе таким доказательством не является.
    order_table = snapshot.get("purchase_orders")
    orders_by_id = {str(row["id"]): row for row in _rows(snapshot, "purchase_orders")
                    if isinstance(row, dict) and row.get("id") is not None}
    no_orders = (isinstance(order_table, dict)
                 and order_table.get("count") == 0
                 and order_table.get("rows") == [])

    # Число созданных за этот запуск заказов проверяем по идентификаторам
    # до/после, а не по произвольной цифре в ответе модели. Нельзя
    # считать число достоверным, если снимок таблицы неполон.
    created_orders_count = None
    if initial_snapshot is not None:
        before = initial_snapshot.get("purchase_orders")
        after = snapshot.get("purchase_orders")
        if (isinstance(before, dict) and isinstance(after, dict)
                and isinstance(before.get("rows"), list)
                and isinstance(after.get("rows"), list)
                and before.get("count") == len(before["rows"])
                and after.get("count") == len(after["rows"])
                and all(isinstance(row, dict) and row.get("id") is not None
                        for row in before["rows"] + after["rows"])):
            old_ids = {str(row["id"]) for row in before["rows"]}
            new_ids = {str(row["id"]) for row in after["rows"]}
            if len(old_ids) == len(before["rows"]) and len(new_ids) == len(after["rows"]):
                created_orders_count = len(new_ids - old_ids)

    has_calculation = any(name == "calc_material_need" for name, _ in successful_tools)
    recommended_supplier = _recommended_supplier(successful_tools, facts.get("recommended_offer_id"))
    referenced_order = _referenced_order(facts, snapshot, successful_tools)
    referenced_offer = _referenced_offer(facts, referenced_order, successful_tools)
    approval_id = facts.get("approval_id") or facts.get("approval_request_id")
    approval_rows = _rows(snapshot, "approval_requests")
    approvals_by_id = {str(row["id"]): row for row in approval_rows if row.get("id") is not None}
    submissions_by_id = {str(row["id"]): row["details"] for row in _rows(snapshot, "business_events")
                         if row.get("type") == "order_submission" and isinstance(row.get("details"), dict)}
    linked_approvals = [row for row in approval_rows
                        if referenced_order and row.get("order_id") == referenced_order.get("id")
                        and (approval_id is None or row.get("id") == approval_id)]
    referenced_approval = linked_approvals[0] if len(linked_approvals) == 1 else {}
    unsupported = set()
    for group, record, key, value in _fact_fields(facts):
        if value is None or isinstance(value, (dict, list)):
            continue
        normalized_key = key.casefold()
        if not record and group == key and normalized_key in _DIAGNOSTIC_LISTS and isinstance(value, str):
            continue  # Поля, которые модель просит уточнить у пользователя.
        supported = (normalized_key, _scalar(value)) in pairs
        if not supported and has_calculation:
            canonical_key = CALC_FACT_ALIASES.get(normalized_key)
            supported = bool(canonical_key and (canonical_key, _scalar(value)) in pairs)
        if not supported and normalized_key == "inventory_as_of":
            supported = _scalar(value) in inventory_dates
        if not supported and normalized_key == "pending_orders_count" and no_orders:
            supported = _scalar(value) == _scalar(0)
        if normalized_key == "orders_created":
            # Это действие, а не произвольный числовой факт: любое
            # совпадение с чужим count в данных не считается доказательством.
            supported = (created_orders_count is not None
                         and _scalar(value) == _scalar(created_orders_count))
        if normalized_key == "chosen_supplier":
            supported = recommended_supplier is not None and value == recommended_supplier
        if normalized_key == "already_recorded":
            supported = value is True and _has_recorded_order(facts, initial_snapshot or {}, successful_tools)
        if normalized_key == "reason" and value in (
                "approval_required_by_policy", "auto_limit_exceeded", "supplier_not_approved"):
            supported = value == approval_reason
        order_id = record.get("order_id")
        if order_id is None and (group in _ORDER_RECORDS or (
                group is None and "status" in record and str(record.get("id")) in orders_by_id)):
            order_id = record.get("id")
        if normalized_key in ORDER_FACT_ALIASES:
            order = orders_by_id.get(str(order_id)) if order_id is not None and group else referenced_order
            stored = order.get(ORDER_FACT_ALIASES[normalized_key]) if order else None
            supported = stored is not None and _scalar(value) == _scalar(stored)
        if order_id is not None and normalized_key in _ORDER_FIELDS:
            stored = orders_by_id.get(str(order_id), {}).get(normalized_key)
            approval = approvals_by_id.get(str(record.get("id")))
            if normalized_key == "operation_id" and record.get("submission_id") is not None:
                submission = submissions_by_id.get(str(record["submission_id"]), {})
                stored = (submission.get("request", {}).get("operation_id")
                          if submission.get("order_id") == order_id else None)
            elif approval and approval.get("order_id") == order_id and normalized_key in {"id", "status"}:
                stored = approval.get(normalized_key)
            elif normalized_key == "status" and isinstance(value, str) and value in _APPROVAL_STATES:
                explicit_approval = approvals_by_id.get(str(
                    record.get("approval_id") or record.get("approval_request_id")))
                if explicit_approval and explicit_approval.get("order_id") == order_id:
                    stored = explicit_approval.get("status")
            supported = stored is not None and _scalar(value) == _scalar(stored)
        if order_id is not None and normalized_key == "approval_id":
            supported = approvals_by_id.get(str(value), {}).get("order_id") == order_id
        if normalized_key in APPROVAL_FACT_ALIASES:
            stored = referenced_approval.get(APPROVAL_FACT_ALIASES[normalized_key])
            supported = stored is not None and _scalar(value) == _scalar(stored)
        if normalized_key == "offer_confirmed":
            supported = bool(referenced_offer and type(value) is bool
                             and value is referenced_offer.get("confirmed"))
        if not supported:
            unsupported.add(key)
    return sorted(unsupported)


_RUBLE_AMOUNT = re.compile(
    r"(?<![\w.,])(?P<amount>[+-]?\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)?)"
    r"\s*(?:₽|руб(?:ль|ля|лей)?\b|rub\b)", re.IGNORECASE
)
_ORDER_TOTAL_LABEL = re.compile(
    r"\b(?:(?:итоговая|общая|полная)\s+(?:сумма|стоимость)(?:\s+заказа)?|"
    r"(?:сумма|стоимость)\s+заказа)\s*(?:[—–:=]\s*)?$", re.IGNORECASE
)


def _unverified_text_amount(text: str, facts: dict[str, Any], snapshot: dict[str, Any],
                            successful_tools: list[tuple[str, Any]]) -> bool:
    """Сверяет прямо названную итоговую сумму с записью конкретного заказа."""
    matches = [match for match in _RUBLE_AMOUNT.finditer(text)
               if _ORDER_TOTAL_LABEL.search(text[max(0, match.start() - 55):match.start()])]
    if not matches:
        return False
    order_ids = {str(value) for key, value in _flat_pairs(facts) if key in {"order_id", "order_ids"}}
    orders = [row for row in _rows(snapshot, "purchase_orders") if str(row.get("id")) in order_ids]
    if not orders:
        order = _referenced_order(facts, snapshot, successful_tools)
        orders = [order] if order else []
    totals = {_scalar(order["total_rub"]) for order in orders if order.get("total_rub") is not None}
    return bool(totals) and any(
        _scalar(re.sub(r"\s", "", match["amount"]).replace(",", ".")) not in totals
        for match in matches
    )


def _include_single_material_quantities(facts: dict[str, Any],
                                        successful_tools: list[tuple[str, Any]]) -> None:
    """Дополняет факты потребностью и дефицитом из последнего расчёта одного материала."""
    for name, data in reversed(successful_tools):
        if name != "calc_material_need" or not isinstance(data, dict):
            continue
        materials = data.get("materials")
        if not isinstance(materials, list) or len(materials) != 1:
            return
        material = materials[0]
        if isinstance(material, dict):
            for fact_key, source_key in (("need", "required_quantity"), ("deficit", "deficit")):
                if material.get(source_key) is not None:
                    facts.setdefault(fact_key, material[source_key])
        return


def _recommended_supplier(successful_tools: list[tuple[str, Any]],
                          offer_id: str | None = None) -> str | None:
    """Связывает рекомендованное предложение с ID его поставщика."""
    for name, data in reversed(successful_tools):
        if name != "compare_suppliers" or not isinstance(data, dict):
            continue
        recommended = data.get("recommended_offer_id")
        if offer_id is not None and offer_id != recommended:
            continue
        if recommended is None:
            return None
        for comparison in data.get("offers", []):
            offer = comparison["offer"]
            if offer["id"] == recommended:
                return offer["supplier_id"]
        return None
    return None


def _pending_approval_reason(snapshot: dict[str, Any],
                             successful_tools: list[tuple[str, Any]]) -> str | None:
    policies = _rows(snapshot, "organization_policies")
    if len(policies) != 1:
        return None
    recorded_ids = {data.get("id") for name, data in successful_tools
                    if name == "request_approval" and isinstance(data, dict) and data.get("id")}
    pending_orders = {request["order_id"] for request in _rows(snapshot, "approval_requests")
                      if request.get("id") in recorded_ids and request.get("status") == "pending"
                      and request.get("order_id")}
    if len(pending_orders) != 1:
        return None
    policy = policies[0]
    if policy.get("mandatory_human_approval") is True:
        return "approval_required_by_policy"
    order = next((row for row in _rows(snapshot, "purchase_orders")
                  if row.get("id") in pending_orders), None)
    return _purchase_restriction(snapshot, policy, order) if order else None


def _purchase_restriction(snapshot: dict[str, Any], policy: dict[str, Any],
                          purchase: dict[str, Any]) -> str | None:
    try:
        total = Decimal(str(purchase.get("total_rub")))
        limit = Decimal(str(policy.get("auto_order_limit_rub")))
        over_limit = total.is_finite() and limit.is_finite() and total > limit >= 0
    except ArithmeticError:
        over_limit = False
    if over_limit:
        return "auto_limit_exceeded"
    if policy.get("approval_required_for_new_supplier") is True and any(
        supplier.get("id") == purchase.get("supplier_id") and supplier.get("approved") is False
        for supplier in _rows(snapshot, "suppliers")
    ):
        return "supplier_not_approved"
    return None


def _proposed_purchase_restriction(event: dict[str, Any], snapshot: dict[str, Any]) -> str | None:
    data = event.get("data")
    if event.get("kind") != "user_message" or not isinstance(data, dict):
        return None
    proposal = data.get("proposed_order")
    policies = _rows(snapshot, "organization_policies")
    if not isinstance(proposal, dict) or len(policies) != 1:
        return None
    policy = policies[0]
    restriction = _purchase_restriction(snapshot, policy, proposal)
    if (restriction is None and policy.get("mandatory_human_approval") is True
            and proposal.get("material") and proposal.get("quantity") is None):
        return "approval_required_by_policy"
    return restriction


def _grounded_facts(facts: dict[str, Any], snapshot: dict[str, Any],
                    successful_tools: list[tuple[str, Any]]) -> bool:
    """Сверяет структурированные факты с результатами инструментов и базой."""
    approval_reason = _pending_approval_reason(snapshot, successful_tools)
    return not _unverified_fact_fields(
        facts, snapshot, successful_tools, approval_reason=approval_reason)


_UNREALIZED_PREDICATE = re.compile(
    r"\b(?:не(?:\s+был(?:[аои])?)?|буд(?:ет|ут)|(?:может|могут|должен|должна|должны|можно)\s+быть|"
    r"not(?:\s+been)?|will\s+be)(?:\s+(?:ещ[её]|уже|успешно|официально|yet|already))?\s+$"
)


def _unrealized_predicate(text: str, start: int) -> bool:
    return _UNREALIZED_PREDICATE.search(text[max(0, start - 56):start]) is not None


def _has_affirmative_claim(text: str, *patterns: str) -> bool:
    return any(not _unrealized_predicate(text, match.start("predicate"))
               for pattern in patterns for match in re.finditer(pattern, text))


def _unsafe_action_claim(text: str, facts: dict[str, Any],
                         tools: list[tuple[str, Any]], snapshot: dict[str, Any]) -> bool:
    """Отклоняет заявления о выполненных действиях без подтверждения.

    Это дополнительная защита, а не полная проверка смысла текста.
    """
    t = text.casefold()
    successful_names = {name for name, _result in tools}
    orders = _rows(snapshot, "purchase_orders")
    approvals = _rows(snapshot, "approval_requests")
    # Оплата не входит в функции Restoka.
    if re.search(r"(?:оплата\s+(?:выполнена|проведена|прошла|успешна)|"
                 r"(?:мы|я)\s+оплатил[аи]?|"
                 r"payment[ _-]?(?:executed|complete|sent))", t):
        return True
    order_claims = re.finditer(
        r"(?:(?:я\s+|мы\s+)?(?P<negated_before>\bне\s+)?(?:успешно\s+)?"
        r"(?P<verb_before>\b(?:отправил[аи]?|оформил[аи]?|подготовил[аи]?|"
        r"отправлен[аоы]?|оформлен[аоы]?|размещ[её]н[аоы]?|создан[аоы]?))\s+"
        r"(?:заказ|заявк[ау](?!\s+на\s+согласовани[ея]\b))\b"
        r"|\b(?:заказ(?:е|а|ы|ов)?|order|заявк[аиу](?!\s+на\s+согласовани[ея]\b))\b[^.!?,;:\n]{0,80}?"
        r"(?P<negated_after>\b(?:не|not)\s+)?(?:успешно\s+)?"
        r"(?P<verb_after>\b(?:отправлен[аоы]?|оформлен[аоы]?|создан[аоы]?|"
        r"размещ[её]н[аоы]?|submitted|sent|dispatched)\b))", t)
    for claim in order_claims:
        verb_group = "verb_before" if claim["verb_before"] else "verb_after"
        if claim["negated_before"] or claim["negated_after"] or _unrealized_predicate(t, claim.start(verb_group)):
            continue
        verb = claim[verb_group]
        is_dispatch = verb.startswith("отправ") or verb in ("submitted", "sent", "dispatched")
        ids = [str(v) for k, v in _flat_pairs(facts) if k == "order_id"]
        # Проверяем ID заказа также в тексте, даже если facts пуст.
        ids.extend(re.findall(r"\border-[a-z0-9_-]+\b", t))
        if is_dispatch and t[claim.end():].lstrip().startswith("на согласование"):
            approval_orders = {str(data["order_id"]).casefold() for name, data in tools
                               if name == "request_approval" and isinstance(data, dict) and data.get("order_id")}
            if not approval_orders or any(order_id.casefold() not in approval_orders for order_id in ids):
                return True
            continue
        if not (successful_names & {"dispatch_order", "get_pending_orders", "create_order", "prepare_draft"}):
            return True
        # Чтение статуса не доказывает, что агент только что выполнил действие.
        if claim["verb_before"] and verb.startswith("отправил"):
            if "dispatch_order" not in successful_names:
                return True
        if ids and any(not any(str(o.get("id")).casefold() == i.casefold() for o in orders) for i in ids):
            return True
        if is_dispatch:
            candidates = [order for order in orders if not ids or any(
                str(order.get("id")).casefold() == i.casefold() for i in ids)]
            sent = [o for o in candidates if o.get("status") in ("submitted", "confirmed")]
            if not sent or (ids and len(sent) != len(candidates)):
                return True
    if _has_affirmative_claim(t,
            r"\b(?:заказ[ауы]?|заявк[аиу])\b[^.!?,;\n]{0,75}?\b(?P<predicate>согласован[аоы]?|одобрен[аоы]?)\b",
            r"\b(?P<predicate>согласовал[аи]?|одобрил[аи]?)\s+заказ\b",
            r"\b(?P<predicate>approved)\s+order\b"):
        if not any((a.get("status") == "approved" or a.get("decision") == "approved") for a in approvals):
            return True
    # Не смешиваем чтение информации с созданием нового внешнего эффекта.
    if re.search(r"(?:я|мы)\s+(?:сейчас\s+)?(?:создал[аи]?|оформил[аи]?|подготовил[аи]?)\s+заказ", t):
        if not (successful_names & {"create_order", "prepare_draft"}):
            return True
    if _has_affirmative_claim(t,
            r"\bчерновик\b[^.!?,;\n]{0,65}?\b(?P<predicate>готов|создан|подготовлен)\b",
            r"\b(?P<predicate>создан|подготовлен|создал[аи]?|подготовил[аи]?)\s+черновик\b",
            r"\bdraft\s+(?P<predicate>prepared|created)\b"):
        if "prepare_draft" not in successful_names:
            existing = _referenced_order(facts, snapshot, tools)
            if (not existing or existing.get("status") not in ("draft", "pending_approval")
                    or re.search(r"\b(?:я|мы)\s+(?:уже\s+|только\s+что\s+)?"
                                 r"(?:создал[аи]?|подготовил[аи]?)\s+черновик", t)):
                return True
    if _has_affirmative_claim(t,
            r"\b(?:запрос|заявка)\s+(?:на\s+)?согласовани[ея][^.!?,;\n]{0,45}?"
            r"\b(?P<predicate>создан[аоы]?|отправлен[аоы]?)\b",
            r"\b(?P<predicate>создана?|отправлена?)\s+(?:запрос|заявка)\s+на\s+согласование\b"):
        if "request_approval" not in successful_names and (
                not ({"get_pending_orders", "get_daily_activity"} & successful_names)
                or not any(request.get("order_id") for request in approvals)
                or re.search(r"\b(?:я|мы|только\s+что|сейчас)\b", t)):
            return True
        if not approvals:
            return True
    if _has_affirmative_claim(t,
            r"\b(?:предложени[ея]|условия)\s+поставщика[^.!?,;\n]{0,55}?"
            r"\b(?P<predicate>обновл[её]н[аоы]?)\b"):
        if "process_supplier_reply" not in successful_names:
            return True
    for claim in re.finditer(r"(?:ревью|отч[её]т)\b[^.!?\n]{0,55}?"
                             r"(?P<negated>\bне\s+)?(?P<verb>\b(?:доставлен|отправлен)[аоы]?)\b", t):
        if claim["negated"] or _unrealized_predicate(t, claim.start("verb")):
            continue
        verified_statuses = ("delivered",) if claim["verb"].startswith("доставлен") else ("sent", "delivered")
        if not any(name == "send_review" and isinstance(data, dict)
                   and data.get("status") in verified_statuses for name, data in tools):
            return True
    if facts.get("status") in ("submitted", "confirmed", "sent", "approved", "delivered"):
        state = facts["status"]
        if state == "approved":
            if not any(a.get("status") == "approved" or a.get("decision") == "approved" for a in approvals):
                return True
        elif state in ("sent", "delivered") and facts.get("order_id") is None:
            if not any(name in ("send_review", "send_supplier_message")
                       and isinstance(data, dict) and data.get("status") == state
                       for name, data in tools):
                return True
        else:
            order_id = facts.get("order_id")
            candidates = [o for o in orders if order_id is None or str(o.get("id")) == str(order_id)]
            if not any(o.get("status") == state for o in candidates):
                return True
    return False
