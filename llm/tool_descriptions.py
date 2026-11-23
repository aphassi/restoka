"""Описания инструментов, передаваемые модели вместе с JSON Schema.

Доступ определяется ToolGateway: наличие описания не даёт дополнительных прав.
"""
from __future__ import annotations

from typing import Any

DESCRIPTIONS = {
    "get_inventory": "Read current inventory quantities and stock observation dates. Read-only; use before planning.",
    "get_appointments": "Read scheduled appointments filtered by dates, service or ID. Read-only.",
    "get_supplier_offers": "Read actual current supplier offers and their confirmed status, dates and prices. Read-only.",
    "get_pending_orders": "Read actual purchase orders and statuses, including existing submitted orders. Read-only; use for status and duplicate checks.",
    "get_organization_policy": "Read trusted order limits, available budget and human approval requirements. Read-only.",
    "get_consumption_rules": "Read material consumption rates for services. Read-only.",
    "calc_material_need": "Calculate material deficits from stock, appointments, consumption rules and pending orders. Read/compute; do not estimate manually.",
    "compare_suppliers": "Compare actual offers for material, quantity, deadline and full cost, with eligibility checks. Read/compute.",
    "prepare_draft": "CREATE LOCAL DRAFT only, no external submission. Re-check need/terms/duplicate risks; use stable operation_id and idempotency_key.",
    "request_approval": "CREATE A REQUEST for a manager to decide; never grants approval. Requires an existing order and reason.",
    "create_order": "CREATE LOCAL SUBMISSION INTENT for an existing draft after checks; does NOT send. An already approved snapshot may be required. Never equate with dispatch.",
    "dispatch_order": "EXTERNAL SIDE EFFECT: submit an existing committed order intent by submission_id; may return UNKNOWN. Reconcile with SAME submission_id, never a new send after ambiguity.",
    "get_supplier_reply": "Retrieve an authenticated supplier message registered by the trusted event adapter. Read-only.",
    "receive_supplier_reply": "Record authenticated supplier message receipt; does NOT change offer conditions. Local write.",
    "process_supplier_reply": (
        "Apply authenticated, revision-checked changes using the full current offer as expected. "
        "Patch only fields whose terms changed and explicit confirmation. Include confirmed=true "
        "when the supplier explicitly confirms revised terms, even if the old offer was confirmed. "
        "For a confirmed subset, set confirmed_quantity and preserve the original offered quantity. "
        "A changed offer does NOT modify an already submitted order."
    ),
    "get_daily_activity": "Read historical business-event facts for an explicit report_date; may report incomplete history.",
    "send_supplier_message": "EXTERNAL SIDE EFFECT: send a supplier question through a mock channel with stable operation_id; no payment.",
    "send_review": "EXTERNAL SIDE EFFECT: send a daily review based on verified activity and facts_digest; require delivery receipt for on-time assertion.",
}


def model_tools(schemas: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    missing = set(schemas) - set(DESCRIPTIONS)
    if missing:
        raise ValueError(f"Missing LLM tool descriptions: {sorted(missing)}")
    return [
        {"type": "function", "function": {
            "name": name, "description": DESCRIPTIONS[name], "parameters": schema,
        }}
        for name, schema in sorted(schemas.items())
    ]
