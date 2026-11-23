"""Collect dated business facts and deliver explicitly dated daily reviews."""

from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from hashlib import sha256
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, or_, exists
from sqlalchemy.orm import Session, sessionmaker

from database.models import ApprovalRequest, BusinessEvent, Organization, PurchaseOrder as StoredOrder
from schemas import PurchaseOrder
from .contracts import ToolError, ToolErrorCode, ToolResult
from .external import MessageDelivery, MockMessageChannel, _send_message
from .queries import _read_only

__all__ = ["get_daily_activity", "send_review", "DailyActivity", "ReviewDelivery"]


class DailyActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    organization_id: str
    report_date: date
    timezone: str
    events: list[dict[str, Any]]
    orders: list[PurchaseOrder]
    approvals: list[dict[str, Any]]
    pending_approvals_current: list[dict[str, Any]]
    external_results: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    risks: list[dict[str, Any]]
    undated_event_ids: list[str]
    undated_events: list[dict[str, Any]]
    unattributed_order_ids: list[str]
    # Legacy orders/approvals fields are explicitly current observations.
    current_state_only: bool = True
    order_transitions: list[dict[str, Any]]
    approval_transitions: list[dict[str, Any]]
    completeness: Literal["unknown", "incomplete"]
    facts_digest: str
    complete: bool
    limitations: list[str]


class ReviewDelivery(MessageDelivery):
    report_date: date
    timezone: str
    deadline: datetime
    on_time: bool | None


def _error(code, message):
    return ToolResult(ok=False, error=ToolError(code=code, message=message, operation_outcome="not_started"))


def _order_refs(details):
    refs = set()
    if isinstance(details.get("order_id"), str):
        refs.add(details["order_id"])
    for key in ("order", "result"):
        value = details.get(key)
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            refs.add(value["id"])
    terms = details.get("terms")
    if isinstance(terms, dict) and isinstance(terms.get("order"), dict):
        if isinstance(terms["order"].get("id"), str):
            refs.add(terms["order"]["id"])
    return refs


@_read_only
def get_daily_activity(session: Session, organization_id: str, *, report_date: date) -> ToolResult[DailyActivity]:
    """Collect dated events and linked current records, stating historical gaps."""
    if type(report_date) is not date:
        return _error(ToolErrorCode.INVALID_ARGUMENTS, "report_date must be an explicit date")
    timezone_name = session.scalar(select(Organization.timezone).where(Organization.id == organization_id))
    if timezone_name is None:
        return _error(ToolErrorCode.NOT_FOUND, "Organization not found")
    try:
        zone = ZoneInfo(timezone_name)
        start = datetime.combine(report_date, time.min, zone)
        end = datetime.combine(report_date + timedelta(days=1), time.min, zone)
    except (ZoneInfoNotFoundError, ValueError, OverflowError):
        return _error(ToolErrorCode.MISSING_DATA, "Organization timezone or report date cannot be used")
    # Fetch the requested day and explicit gaps, not the entire event history.
    rows = session.execute(select(BusinessEvent.id, BusinessEvent.type, BusinessEvent.date, BusinessEvent.details)
        .where(BusinessEvent.organization_id == organization_id,
               or_(BusinessEvent.date == report_date, BusinessEvent.date.is_(None)))
        .order_by(BusinessEvent.id)).mappings().all()
    events, undated_events, linked, transitions = [], [], set(), []
    mutable_gap = False
    for row in rows:
        details = row["details"] if isinstance(row["details"], dict) else {}
        entry = dict(id=row["id"], type=row["type"], date=row["date"].isoformat() if row["date"] else None)
        if row["date"] is None:
            undated_events.append(dict(entry, details=details))
            continue
        if row["type"] in ("order_submission", "daily_review", "supplier_message"):
            snapshot = details.get("initial_snapshot")
            if snapshot is None:
                mutable_gap = True
                # Preserve references only: a mutable legacy outcome is not a daily fact.
                details = {k: details[k] for k in ("request", "order_id", "prepared_at") if k in details}
                details["historical_state_unknown"] = True
            else:
                details = snapshot
        linked.update(_order_refs(details))
        entry["details"] = details
        events.append(entry)
        snapshot = details.get("order")
        if row["type"] == "order_draft_prepared":
            snapshot = details.get("result")
        if isinstance(snapshot, dict) and isinstance(snapshot.get("id"), str):
            transitions.append(dict(event_id=row["id"], event_type=row["type"], occurred_at=details.get("occurred_at"), order=snapshot))
    events.sort(key=lambda e: (e["details"].get("occurred_at", ""), e["id"]))
    phase = {"order_draft_prepared": 0, "approval_requested": 1, "order_submission_started": 2, "order_submission_result": 3}
    transitions.sort(key=lambda e: (e["occurred_at"] or "", phase.get(e["event_type"], 4), e["event_id"]))
    approval_rows = session.execute(select(*ApprovalRequest.__table__.columns).where(
        ApprovalRequest.organization_id == organization_id,
        or_(ApprovalRequest.order_id.in_(linked),
            (ApprovalRequest.created_at >= start) & (ApprovalRequest.created_at < end),
            (ApprovalRequest.decided_at >= start) & (ApprovalRequest.decided_at < end),
            (ApprovalRequest.status == "pending") & (ApprovalRequest.created_at < end)))
        .order_by(ApprovalRequest.id)).mappings().all()
    approvals, pending = [], []
    approval_transitions = [dict(approval_id=e["details"]["approval_id"],
        order_id=e["details"].get("order_id"), occurred_at=e["details"]["occurred_at"],
        state=e["details"]["state"], decided_by=e["details"].get("decided_by"),
        source="immutable_event") for e in events if e["type"] == "approval_decided"]
    journaled_decisions = set(session.scalars(select(BusinessEvent.details["approval_id"].as_string()).where(
        BusinessEvent.organization_id == organization_id, BusinessEvent.type == "approval_decided",
        BusinessEvent.details["approval_id"].as_string().in_([row["id"] for row in approval_rows]))))
    legacy_decisions = False
    for row in approval_rows:
        item = {key: (value.isoformat() if isinstance(value, datetime) else value)
                for key, value in row.items() if key != "organization_id"}
        created = start <= row["created_at"] < end
        decided = row["decided_at"] is not None and start <= row["decided_at"] < end
        if created or decided:
            approvals.append(item)
            if row["order_id"]:
                linked.add(row["order_id"])
        if created:
            approval_transitions.append(dict(approval_id=row["id"], order_id=row["order_id"],
                occurred_at=row["created_at"].isoformat(), state="pending", source="approval_created_at"))
        if decided and row["id"] not in journaled_decisions:
            legacy_decisions = True
            approval_transitions.append(dict(approval_id=row["id"], order_id=row["order_id"],
                occurred_at=row["decided_at"].isoformat(), state=row["decision"],
                decided_by=row["decided_by"], source="trusted_stored_decision"))
        if row["status"] == "pending" and row["created_at"] < end:
            pending.append(item)
    order_rows = session.execute(select(*StoredOrder.__table__.columns).where(
        StoredOrder.organization_id == organization_id, StoredOrder.id.in_(linked))
        .order_by(StoredOrder.id)).mappings().all()
    orders = [PurchaseOrder(**{k: row[k] if k in row else None for k in PurchaseOrder.model_fields}) for row in order_rows]
    # SQL anti-join finds legacy attribution gaps without loading all event payloads.
    d = BusinessEvent.details
    references = or_(d["order_id"].as_string() == StoredOrder.id,
                     d["order"]["id"].as_string() == StoredOrder.id,
                     d["result"]["id"].as_string() == StoredOrder.id,
                     d["terms"]["order"]["id"].as_string() == StoredOrder.id)
    unattributed = list(session.scalars(select(StoredOrder.id).where(StoredOrder.organization_id == organization_id,
        ~exists(select(BusinessEvent.id).where(BusinessEvent.organization_id == organization_id,
                   BusinessEvent.date.is_not(None), references))).order_by(StoredOrder.id)))
    external = [e for e in events if e["type"].startswith(("order_submission", "supplier_message", "daily_review", "supplier_reply"))]
    errors = [e for e in events if e["type"] in ("error", "tool_error", "operation_failed")
              or e["details"].get("error") is not None or e["details"].get("state") in ("failed", "rejected")]
    risks = [e for e in events if e["type"] in ("supplier_delay", "deficit_detected", "risk_detected")
             or e["details"].get("risk") is not None or e["details"].get("state") in ("unknown", "sending")]
    limitations = [
        "No authoritative closed-day execution ledger exists: total coverage cannot be proven.",
        "Only persisted errors and risks are included; rolled-back or unlogged failures are unknown.",
        "orders, approvals and pending_approvals_current contain current observations; use transitions for history.",
    ]
    if undated_events:
        limitations.append("Undated events are excluded from this day's actions; their attribution is unknown.")
    if unattributed:
        limitations.append("Orders without dated references cannot be attributed to a day.")
    if mutable_gap:
        limitations.append("Legacy mutable events lack immutable historical snapshots.")
    if legacy_decisions:
        limitations.append("Some approval decisions have trusted timestamps but no immutable decision event.")
    completeness = "incomplete" if undated_events or unattributed or mutable_gap or legacy_decisions else "unknown"
    # Bind the report to historical evidence, not mutable current-state projections.
    evidence = dict(organization_id=organization_id, report_date=report_date.isoformat(), timezone=timezone_name,
                    events=events, approval_transitions=approval_transitions,
                    undated_events=undated_events, limitations=limitations)
    digest = sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return ToolResult[DailyActivity](ok=True, data=DailyActivity(
        organization_id=organization_id, report_date=report_date, timezone=timezone_name,
        events=events, orders=orders, approvals=approvals, pending_approvals_current=pending,
        order_transitions=transitions, approval_transitions=approval_transitions,
        external_results=external, errors=errors, risks=risks,
        undated_event_ids=[e["id"] for e in undated_events], undated_events=undated_events,
        unattributed_order_ids=unattributed, complete=False, completeness=completeness,
        facts_digest=digest, limitations=limitations,
    ))



def send_review(
    factory: sessionmaker, organization_id: str, *, report_date: date, text: str,
    operation_id: str, now: datetime, channel: MockMessageChannel, facts_digest: str | None = None,
) -> ToolResult[ReviewDelivery]:
    """Deliver supplied review text and compare receipt time with the local deadline."""
    if type(report_date) is not date:
        return _error(ToolErrorCode.INVALID_ARGUMENTS, "report_date must be an explicit date")
    # Validate the factory/time/organization through the shared delivery path.
    result = _send_message(factory, organization_id, purpose="daily_review", destination="manager",
                           body=text, operation_id=operation_id, now=now, channel=channel,
                           report_date=report_date, facts_digest=facts_digest, facts_reader=get_daily_activity)
    if not result.ok:
        return result
    try:
        with factory() as session:
            name = session.scalar(select(Organization.timezone).where(Organization.id == organization_id))
        deadline = datetime.combine(report_date + timedelta(days=1), time(0, 10), ZoneInfo(name))
        delivery = result.data
        return ToolResult[ReviewDelivery](ok=True, data=ReviewDelivery(
            **delivery.model_dump(), report_date=report_date, timezone=name, deadline=deadline,
            on_time=(delivery.delivered_at <= deadline if delivery.status == "delivered" else None),
        ))
    except Exception:
        # Delivery might already be committed: never suggest a new send on a
        # failure to read the timezone or construct the presentation result.
        return ToolResult(ok=False, error=ToolError(code=ToolErrorCode.OPERATION_OUTCOME_UNKNOWN,
            message="Review delivery requires reconciliation before reporting its deadline",
            operation_outcome="unknown"))
