"""Manage purchase orders, approvals, and verified supplier updates.

Enforce business rules, prevent duplicate operations, and safely
handle order submissions and their outcomes.
"""

from collections.abc import Callable
from datetime import date, datetime
from dataclasses import dataclass
from hashlib import sha256
import json
import logging
from typing import Literal, Protocol
from decimal import Decimal, localcontext
from functools import wraps
from inspect import signature
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from database.database import session_scope
from typing_extensions import TypedDict

from database.runtime import current_event_time, event_date, event_time
from database.models import (
    ApprovalRequest, BusinessEvent, Organization, OrganizationPolicy, PurchaseOrder as StoredOrder,
    Supplier, SupplierOffer as StoredOffer,
)
from schemas import Money, Quantity, PurchaseOrder, SupplierOffer
from .contracts import ToolError, ToolErrorCode, ToolResult
from .planning import calc_material_need, compare_suppliers
from .queries import SupplierOfferRow, get_supplier_offers

__all__ = ["prepare_draft", "create_order", "dispatch_order", "request_approval", "record_approval_decision",
           "update_supplier_offer", "SupplierOfferPatch", "SupplierMessageSource",
           "SubmissionReceipt", "SubmissionPlan", "OrderSender"]


class ApprovalRecord(TypedDict):
    id: str
    order_id: str | None
    reason: str
    status: str
    decision: str | None
    decided_by: str | None
    created_at: datetime
    decided_at: datetime | None


class SupplierOfferPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    quantity: Quantity | None = None
    unit_price_rub: Money | None = None
    delivery_fee_rub: Money | None = None
    delivery_date: date | None = None
    confirmed: bool | None = None
    confirmed_quantity: Quantity | None = None
    clear_fields: frozenset[Literal[
        "unit_price_rub", "delivery_fee_rub", "delivery_date", "confirmed_quantity",
    ]] = frozenset()


@dataclass(frozen=True)
class SupplierMessageSource:
    organization_id: str
    supplier_id: str
    offer_id: str
    message_id: str
    revision: int


class SubmissionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    submission_id: str = Field(min_length=1)
    outcome: Literal["sent", "rejected", "failed", "unknown"]
    reference: str | None = None


class SubmissionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    submission_id: str
    order: PurchaseOrder
    state: str
    persistence: Literal["pending_caller_commit"] = "pending_caller_commit"


class OrderSender(Protocol):
    def send(self, organization_id: str, submission_id: str,
             order: PurchaseOrder) -> SubmissionReceipt: ...

    def lookup(self, organization_id: str, submission_id: str,
               order: PurchaseOrder) -> SubmissionReceipt: ...


def _event_id(kind, identity):
    return kind + ":" + sha256(identity.encode()).hexdigest()


def _event(session, org, kind, identity):
    row = session.scalar(select(BusinessEvent).where(
        BusinessEvent.organization_id == org, BusinessEvent.id == _event_id(kind, identity),
    ).with_for_update().execution_options(populate_existing=True))

    if row is not None and (row.type != kind or not isinstance(row.details, dict)):
        _reject(ToolErrorCode.MISSING_DATA, "Business event has incompatible data")

    return row


def _record(session, org, kind, identity, details):
    try:
        now = current_event_time()
    except ValueError as exc:
        _reject(ToolErrorCode.MISSING_DATA, str(exc))

    details = dict(details, occurred_at=now.isoformat())
    if kind == "order_submission":
        details["initial_snapshot"] = dict(details)

    row = BusinessEvent(organization_id=org, id=_event_id(kind, identity),
                        type=kind, date=event_date(session, org, now), details=details)
    session.add(row)

    return row


def _events(session, org, kind):
    return session.scalars(select(BusinessEvent).where(
        BusinessEvent.organization_id == org, BusinessEvent.type == kind,
    ).with_for_update().execution_options(populate_existing=True)).all()


def _offer_snapshot(offer):
    return SupplierOffer(**{k: getattr(offer, k) for k in SupplierOffer.model_fields}).model_dump(mode="json")


def _draft_event(session, org, order):
    event = _event(session, org, "order_draft_prepared", order.id)
    if event is None:
        _reject(ToolErrorCode.MISSING_DATA, "Historical order lacks its original request and offer snapshot")

    return event.details


def _terms(session, org, order):
    draft = _draft_event(session, org, order)
    offer = _locked(session, StoredOffer, org, draft["request"]["offer_id"])

    return {"order": _order_data(order).model_dump(mode="json") | {"status": None},
            "offer": _offer_snapshot(offer)}


def _submission_for_order(session, org, order_id):
    return [e for e in _events(session, org, "order_submission") if e.details.get("order_id") == order_id]


class _Rejected(Exception):
    def __init__(self, code, message, outcome="not_started"):
        self.error = ToolError(code=code, message=message, operation_outcome=outcome)


def _reject(code, message, outcome="not_started"):
    raise _Rejected(code, message, outcome)


def _text(value, name, limit=100):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _reject(ToolErrorCode.INVALID_ARGUMENTS, f"{name} must be a nonblank string of at most {limit} characters")


def _write(function):
    call_signature = signature(function)

    @wraps(function)
    def wrapped(*args, **kwargs):
        identifiers = {}
        session = None
        releasing = False
        try:
            try:
                bound = call_signature.bind(*args, **kwargs)
            except TypeError:
                _reject(ToolErrorCode.INVALID_ARGUMENTS, "Arguments do not match this action's signature")
    
            bound.apply_defaults()
            values = bound.arguments
            for name in ("operation_id", "idempotency_key"):
                if name in values:
                    _text(values[name], name, 255)
                    identifiers[name] = values[name]
    
            session, org = values["session"], values["organization_id"]
            _text(org, "organization_id")
    
            if not isinstance(session, Session):
                _reject(ToolErrorCode.INVALID_ARGUMENTS, "session must be a SQLAlchemy Session")
        
            # begin_nested flushes pending caller changes even with no_autoflush.
            if session.new or session.dirty or session.deleted:
                _reject(ToolErrorCode.INVALID_ARGUMENTS, "Flush or roll back pending caller changes before invoking an action")
        
            with session.begin_nested(), localcontext() as decimal_context:
                decimal_context.prec = 64
    
                # All these writers lock in the same order. The lock lasts until
                # the caller commits, including across released savepoints.
                organization = session.scalar(select(Organization).where(
                    Organization.id == org).with_for_update())
                if organization is None:
                    _reject(ToolErrorCode.NOT_FOUND, "Organization not found")
    
                result = function(*args, **kwargs)
                session.flush()
                releasing = True
    
                return result
    
        except _Rejected as exc:
            return ToolResult(ok=False, error=exc.error.model_copy(update=identifiers))
        
        except ValidationError:
            return ToolResult(ok=False, error=ToolError(
                code=ToolErrorCode.INVALID_ARGUMENTS, message="Invalid structured arguments",
                operation_outcome="not_started", **identifiers))
        
        except SQLAlchemyError:
            _abort_failed_transaction(session, releasing)
            return ToolResult(ok=False, error=ToolError(
                code=ToolErrorCode.DATABASE_ERROR, message=("Savepoint finalization failed; outer transaction was aborted; start a new session_scope"
                         if releasing else "Database operation failed; local changes rolled back"),
                operation_outcome="failed", **identifiers))
        
        except Exception:
            logging.getLogger(__name__).exception("Unexpected failure in action %s", function.__name__)
            _abort_failed_transaction(session, releasing)
            return ToolResult(ok=False, error=ToolError(
                code=ToolErrorCode.INTERNAL_ERROR, message="Internal action failure; changes rolled back",
                operation_outcome="failed", **identifiers))

    return wrapped



def _abort_failed_transaction(session, releasing):
    # A failed RELEASE can leave the savepoint's writes in the outer transaction
    # even though SessionTransaction is already closed. Never let the caller
    # commit those writes after receiving a failure result.
    if isinstance(session, Session) and (releasing or not session.is_active):
        try:
            session.rollback()
        except Exception:
            session.invalidate()


def _locked(session, model, org, identity):
    _text(identity, "object id")

    row = session.scalar(select(model).where(
        model.organization_id == org, model.id == identity,
    ).with_for_update().execution_options(populate_existing=True))
    if row is None:
        _reject(ToolErrorCode.NOT_FOUND, "Object not found in this organization")
    
    return row


def _order_data(row):
    return PurchaseOrder(
        id=row.id, supplier_id=row.supplier_id, material=row.material,
        quantity=row.quantity, total_rub=row.total_rub, eta=row.eta,
        status=row.status, operation_id=row.operation_id, idempotency_key=row.idempotency_key,
        # Historical per-unit prices are not stored on purchase_orders.
        unit_price_rub=None, delivery_fee_rub=None,
    )


def _checked_offer(session, org, offer_id, material, quantity, as_of, required_by):
    offer = _locked(session, StoredOffer, org, offer_id)
    _locked(session, Supplier, org, offer.supplier_id)

    session.scalar(select(OrganizationPolicy).where(
        OrganizationPolicy.organization_id == org).with_for_update())

    comparison = compare_suppliers(session, org, material=material, quantity=quantity,
                                   as_of=as_of, required_by=required_by)
    if not comparison.ok:
        raise _Rejected(comparison.error.code, comparison.error.message)

    candidate = next((item for item in comparison.data.offers if item.offer["id"] == offer_id), None)
    if candidate is None:
        _reject(ToolErrorCode.BUSINESS_RULE_VIOLATION, "Offer does not match the requested material")
    if candidate.suitability == "rejected":
        _reject(ToolErrorCode.BUSINESS_RULE_VIOLATION, "; ".join(candidate.reasons))
    if candidate.suitability == "incomplete":
        _reject(ToolErrorCode.MISSING_DATA, "; ".join(candidate.reasons + candidate.missing_policy_fields))
    
    return candidate


def _duplicates(session, org, material, exclude=None, *, need_from=None, required_by=None):
    rows = session.scalars(select(StoredOrder).where(
        StoredOrder.organization_id == org,
        or_(StoredOrder.material == material, StoredOrder.material.is_(None)),
        StoredOrder.status.not_in(("cancelled", "canceled")),
    ).with_for_update()).all()

    for row in rows:
        if row.id == exclude:
            continue
    
        history = _event(session, org, "order_draft_prepared", row.id)
        request = history.details.get("request", {}) if history else {}
        old_start, old_end = request.get("need_from"), request.get("required_by")
        if (row.material == material and need_from is not None and old_start and old_end
                and (required_by < date.fromisoformat(old_start)
                     or need_from > date.fromisoformat(old_end))):
            continue
    
        # Delivered historical orders with no window are reflected in observed
        # stock. Known overlapping windows remain protected against repurchase.
        if row.status == "delivered" and not old_start:
            continue
    
        code = ToolErrorCode.DUPLICATE_OPERATION if old_start else ToolErrorCode.MISSING_DATA
        _reject(code, "Existing purchase may cover this need; independent demand is not established")


def _check_need(session, org, material, quantity, as_of, need_from, required_by):
    if need_from is None:
        return

    result = calc_material_need(session, org, material=material, as_of=as_of,
                                date_from=need_from, date_to=required_by)

    if not result.ok:
        raise _Rejected(result.error.code, result.error.message)

    need = next((m for m in result.data.materials if m.material == material), None)
    if need is None or quantity > min(need.deficit, need.required_quantity):
        _reject(ToolErrorCode.BUSINESS_RULE_VIOLATION, "Quantity exceeds the uncovered planned need")


def _same_terms(order, candidate, quantity):
    offer = candidate.offer
    return (order.supplier_id == offer["supplier_id"] and order.material == offer["material"]
            and order.quantity == quantity and order.total_rub == candidate.total_rub
            and order.eta == offer["delivery_date"])


@_write
def prepare_draft(
    session: Session, organization_id: str, *, offer_id: str, material: str,
    quantity: Decimal, as_of: date, required_by: date,
    operation_id: str, idempotency_key: str, need_from: date | None = None,
) -> ToolResult[PurchaseOrder]:
    """Save a checked draft; preparation neither sends nor approves an order."""

    _text(offer_id, "offer_id")
    _text(material, "material", 10000)

    if not isinstance(quantity, Decimal):
        _reject(ToolErrorCode.INVALID_ARGUMENTS, "quantity must be a positive Decimal")
    
    quantity = TypeAdapter(Quantity).validate_python(quantity)
    if quantity <= 0 or type(as_of) is not date or type(required_by) is not date or as_of > required_by:
        _reject(ToolErrorCode.INVALID_ARGUMENTS, "Provide a positive quantity and ordered dates")
    if need_from is not None and (type(need_from) is not date or not as_of <= need_from <= required_by):
        _reject(ToolErrorCode.INVALID_ARGUMENTS, "need_from must lie between as_of and required_by")
    
    request = dict(offer_id=offer_id, material=material, quantity=format(quantity.quantize(Decimal("0.001")), "f"),
                   as_of=as_of.isoformat(), required_by=required_by.isoformat(),
                   need_from=need_from.isoformat() if need_from else None,
                   operation_id=operation_id, idempotency_key=idempotency_key)

    existing = session.scalars(select(StoredOrder).where(
        StoredOrder.organization_id == organization_id,
        or_(StoredOrder.operation_id == operation_id, StoredOrder.idempotency_key == idempotency_key),
    ).with_for_update().execution_options(populate_existing=True)).all()
    if existing:
        if len(existing) != 1 or existing[0].operation_id != operation_id or existing[0].idempotency_key != idempotency_key:
            _reject(ToolErrorCode.DUPLICATE_OPERATION, "Operation identifiers conflict")
        
        saved = _draft_event(session, organization_id, existing[0])
        if saved["request"] != request:
            _reject(ToolErrorCode.DUPLICATE_OPERATION, "Operation identifiers conflict with original arguments")
        
        return ToolResult[PurchaseOrder](ok=True, data=PurchaseOrder.model_validate_json(json.dumps(saved["result"])))

    _check_need(session, organization_id, material, quantity, as_of, need_from, required_by)
    candidate = _checked_offer(session, organization_id, offer_id, material, quantity, as_of, required_by)
    _duplicates(session, organization_id, material, need_from=need_from, required_by=required_by)

    order = StoredOrder(
        organization_id=organization_id, id=str(uuid4()), operation_id=operation_id,
        idempotency_key=idempotency_key, status="draft", supplier_id=candidate.offer["supplier_id"],
        material=material, quantity=quantity.quantize(Decimal("0.001")),
        total_rub=candidate.total_rub.quantize(Decimal("0.01")),
        eta=candidate.offer["delivery_date"],
    )

    session.add(order)
    result = _order_data(order)

    _record(session, organization_id, "order_draft_prepared", order.id,
            {"request": request, "result": result.model_dump(mode="json"),
             "offer": _offer_snapshot(_locked(session, StoredOffer, organization_id, offer_id))})

    return ToolResult[PurchaseOrder](ok=True, data=result)


@_write
def create_order(
    session: Session, organization_id: str, *, order_id: str, offer_id: str,
    as_of: date, required_by: date, operation_id: str, idempotency_key: str,
    verify_submission: Callable[[str, PurchaseOrder], ToolResult[bool]] | None = None,
) -> ToolResult[SubmissionPlan]:
    """Validate a draft and persist the intent to submit it.

    The actual external submission is performed separately by dispatch_order
    after the transaction is committed.
    """

    # This function now stages an intent. The legacy docstring is retained at
    # the owner's request; dispatch_order owns the committed external phase.
    if verify_submission is not None:
        _reject(ToolErrorCode.INVALID_ARGUMENTS, "Use dispatch_order with a sender after committing the intent")
    
    if type(as_of) is not date or type(required_by) is not date or as_of > required_by:
        _reject(ToolErrorCode.INVALID_ARGUMENTS, "Provide ordered as_of and required_by dates")
    
    request = dict(order_id=order_id, offer_id=offer_id, as_of=as_of.isoformat(),
                   required_by=required_by.isoformat(), operation_id=operation_id, idempotency_key=idempotency_key)

    _text(order_id, "order_id")
    _text(offer_id, "offer_id")

    for previous in _events(session, organization_id, "order_submission"):
        old = previous.details["request"]
        if old["operation_id"] == operation_id or old["idempotency_key"] == idempotency_key:
            if old != request:
                _reject(ToolErrorCode.DUPLICATE_OPERATION, "Submission identifiers conflict with original arguments")
            order = _locked(session, StoredOrder, organization_id, order_id)
            return ToolResult[SubmissionPlan](ok=True, data=SubmissionPlan(
                submission_id=previous.id, order=_order_data(order), state=previous.details["state"]))
    
    order = _locked(session, StoredOrder, organization_id, order_id)

    if operation_id == order.operation_id or idempotency_key == order.idempotency_key:
        _reject(ToolErrorCode.DUPLICATE_OPERATION, "Submission must use identifiers distinct from draft preparation")
    if _submission_for_order(session, organization_id, order.id):
        _reject(ToolErrorCode.DUPLICATE_OPERATION, "This order already has a submission attempt; reconcile it")
    
    _validate_submission(session, organization_id, order, offer_id, as_of, required_by)

    intent = _record(session, organization_id, "order_submission", idempotency_key,
                     {"order_id": order.id, "request": request, "state": "prepared",
                      "terms": _terms(session, organization_id, order), "reserved_rub": None})

    return ToolResult[SubmissionPlan](ok=True, data=SubmissionPlan(
        submission_id=intent.id, order=_order_data(order), state="prepared"))


def _validate_submission(session, org, order, offer_id, as_of, required_by):
    if order.status not in ("draft", "pending_approval"):
        _reject(ToolErrorCode.BUSINESS_RULE_VIOLATION, "Only unsent orders can be submitted")
    
    draft = _draft_event(session, org, order)

    if offer_id != draft["request"]["offer_id"] or required_by != date.fromisoformat(draft["request"]["required_by"]):
        _reject(ToolErrorCode.BUSINESS_RULE_VIOLATION, "Submission must match the draft offer and deadline")
    if order.material is None or order.quantity is None:
        _reject(ToolErrorCode.MISSING_DATA, "Order has incomplete purchase terms")
    
    candidate = _checked_offer(session, org, offer_id, order.material, order.quantity, as_of, required_by)
    current = _terms(session, org, order)

    if not _same_terms(order, candidate, order.quantity) or current["offer"] != draft["offer"]:
        _reject(ToolErrorCode.BUSINESS_RULE_VIOLATION, "Offer terms changed; the draft and approval must be reviewed")
    
    start = draft["request"].get("need_from")

    _check_need(session, org, order.material, order.quantity, as_of,
                date.fromisoformat(start) if start else None, required_by)
    _duplicates(session, org, order.material, exclude=order.id,
                need_from=date.fromisoformat(start) if start else None, required_by=required_by)

    approvals = session.scalars(select(ApprovalRequest).where(
        ApprovalRequest.organization_id == org, ApprovalRequest.order_id == order.id,
    ).with_for_update().execution_options(populate_existing=True)).all()

    approved = False
    for approval in approvals:
        if approval.status == "rejected" or approval.decision == "rejected":
            _reject(ToolErrorCode.BUSINESS_RULE_VIOLATION, "Manager rejected this purchase")
        snapshot = _event(session, org, "approval_requested", approval.id)
        if (approval.status == "approved" and approval.decision == "approved"
                and isinstance(approval.decided_by, str) and approval.decided_by.strip()
                and approval.decided_at is not None and approval.decided_at >= approval.created_at
                and snapshot is not None and snapshot.details.get("terms") == current):
            approved = True
        else:
            _reject(ToolErrorCode.APPROVAL_REQUIRED, "Approval is pending, incomplete or not bound to current terms")
    if (candidate.approval_required or order.status == "pending_approval"
            or candidate.automatic_ordering_check != "not_blocked") and not approved:
        _reject(ToolErrorCode.APPROVAL_REQUIRED, "A trusted manager decision on these purchase terms is required")


@_write
def request_approval(
    session: Session, organization_id: str, *, request_id: str,
    order_id: str | None, reason: str,
) -> ToolResult[ApprovalRecord]:
    """Persist a pending request, using its stable ID for idempotent retries."""

    _text(request_id, "request_id")
    _text(reason, "reason", 10000)

    if order_id is not None:
        _text(order_id, "order_id")
    
    existing = session.scalar(select(ApprovalRequest).where(
        ApprovalRequest.organization_id == organization_id, ApprovalRequest.id == request_id,
    ).with_for_update().execution_options(populate_existing=True))
    if existing is not None:
        if existing.order_id != order_id or existing.reason != reason:
            _reject(ToolErrorCode.DUPLICATE_OPERATION, "Request ID conflicts with the stored approval request")
        row = existing
    else:
        if order_id is not None:
            order = _locked(session, StoredOrder, organization_id, order_id)
            if order.status not in ("draft", "pending_approval"):
                _reject(ToolErrorCode.BUSINESS_RULE_VIOLATION, "Only unsent orders may enter approval")
            
            pending = session.scalar(select(ApprovalRequest.id).where(
                ApprovalRequest.organization_id == organization_id,
                ApprovalRequest.order_id == order_id,
            ))
            if pending is not None:
                _reject(ToolErrorCode.DUPLICATE_OPERATION, "This order already has an approval request; review the existing decision")
            
            if _submission_for_order(session, organization_id, order_id):
                _reject(ToolErrorCode.BUSINESS_RULE_VIOLATION, "Submission has already been staged")
            
            terms = _terms(session, organization_id, order)
            draft = _draft_event(session, organization_id, order)
            if terms["offer"] != draft["offer"]:
                _reject(ToolErrorCode.BUSINESS_RULE_VIOLATION, "Offer changed since draft preparation")

            order.status = "pending_approval"
        
        event = _record(session, organization_id, "approval_requested", request_id,
                        {"terms": terms if order_id is not None else None, "order_id": order_id,
                         "approval_id": request_id, "state": "pending", "reason": reason,
                         "order": _order_data(order).model_dump(mode="json") if order_id is not None else None})
    
        row = ApprovalRequest(organization_id=organization_id, id=request_id,
                              order_id=order_id, reason=reason, status="pending",
                              created_at=datetime.fromisoformat(event.details["occurred_at"]))
    
        session.add(row)
        session.flush()
    
    return ToolResult[ApprovalRecord](ok=True, data={
        field: getattr(row, field) for field in ApprovalRecord.__annotations__
    })


@_write
def update_supplier_offer(
    session: Session, organization_id: str, *, offer_id: str,
    expected: SupplierOffer, changes: SupplierOfferPatch | SupplierOffer,
    verify_source: Callable[[SupplierMessageSource, SupplierOfferPatch, SupplierOffer], bool] | None = None,
    source: SupplierMessageSource | None = None,
) -> ToolResult[SupplierOfferRow]:
    """Merge authenticated supplier facts with optimistic concurrency checking.

    expected is the previously read full offer. None in changes preserves the
    stored field. The trusted verifier must authenticate the supplier and check
    message contents, offer identity and freshness; it must reject replayed or
    superseded messages. Extracted LLM claims alone are insufficient evidence.
    """

    if not isinstance(expected, SupplierOffer) or not isinstance(changes, (SupplierOffer, SupplierOfferPatch)):
        _reject(ToolErrorCode.INVALID_ARGUMENTS, "Provide a validated expected snapshot and offer patch")
    
    expected = SupplierOffer.model_validate(expected.model_dump())
    offer = _locked(session, StoredOffer, organization_id, offer_id)
    current = {field: getattr(offer, field) for field in SupplierOffer.model_fields}

    if isinstance(changes, SupplierOffer):
        changes = SupplierOffer.model_validate(changes.model_dump())
        for key in ("id", "supplier_id", "material"):
            value = getattr(changes, key)
            if value is not None and value != current[key]:
                _reject(ToolErrorCode.BUSINESS_RULE_VIOLATION, "Offer identity cannot change")
            
        changes = SupplierOfferPatch(**{k: v for k, v in changes.model_dump().items()
                                       if k not in ("id", "supplier_id", "material") and v is not None})
    else:
        changes = SupplierOfferPatch.model_validate(changes.model_dump())
    
    if not isinstance(source, SupplierMessageSource) or not callable(verify_source):
        _reject(ToolErrorCode.MISSING_DATA, "Authenticated message source and verifier must be injected by the application")
    
    for field in ("organization_id", "supplier_id", "offer_id", "message_id"):
        _text(getattr(source, field), field, 255 if field == "message_id" else 100)
    
    if (source.organization_id != organization_id or source.offer_id != offer_id
            or source.supplier_id != offer.supplier_id):
        _reject(ToolErrorCode.BUSINESS_RULE_VIOLATION, "Message source does not own this offer")
    
    if type(source.revision) is not int or source.revision < 1:
        _reject(ToolErrorCode.INVALID_ARGUMENTS, "Source revision must be a positive integer")
    
    identity = json.dumps([source.supplier_id, source.offer_id, source.message_id], ensure_ascii=False)

    request = {"offer_id": offer_id, "revision": source.revision,
               "patch": changes.model_dump(mode="json")}
    request["patch"]["clear_fields"] = sorted(request["patch"]["clear_fields"])

    previous = _event(session, organization_id, "supplier_offer_updated", identity)
    if previous is not None:
        if previous.details["request"] != request:
            _reject(ToolErrorCode.DUPLICATE_OPERATION, "Source message conflicts with its recorded update")
        
        # This is a replay of a persisted business fact, not another update.
        return ToolResult[SupplierOfferRow].model_validate_json(json.dumps(previous.details["result"]))

    for old in _events(session, organization_id, "supplier_offer_updated"):
        if old.details["request"]["offer_id"] == offer_id and old.details["request"]["revision"] >= source.revision:
            _reject(ToolErrorCode.DUPLICATE_OPERATION, "Stale or repeated supplier revision")
    
    if expected.model_dump() != current:
        _reject(ToolErrorCode.DUPLICATE_OPERATION, "Offer changed since it was read; refresh before updating")
    
    for attempt in _events(session, organization_id, "order_submission"):
        if attempt.details["request"]["offer_id"] == offer_id and attempt.details["state"] in ("sending", "unknown"):
            _reject(ToolErrorCode.OPERATION_OUTCOME_UNKNOWN, "Reconcile in-flight purchase before changing its offer", "unknown")
    
    update = {key: value for key, value in changes.model_dump().items()
              if value is not None and key != "clear_fields"}
    if any(field in update for field in changes.clear_fields):
        _reject(ToolErrorCode.INVALID_ARGUMENTS, "A field cannot be set and cleared together")
    
    update.update({field: None for field in changes.clear_fields})

    material_change = any(key in update and update[key] != current[key] for key in (
        "quantity", "unit_price_rub", "delivery_fee_rub", "delivery_date", "confirmed_quantity",
    ))
    if material_change and changes.confirmed is not True:
        update.update(confirmed=False, confirmed_quantity=None)
    elif changes.confirmed is False:
        update["confirmed_quantity"] = None
    
    # Explicit reconfirmation means the verifier must authenticate ALL merged
    # conditions; missing confirmation never inherits authority from old terms.
    merged = current | update
    if merged["confirmed_quantity"] is not None and merged["confirmed_quantity"] > merged["quantity"]:
        _reject(ToolErrorCode.INVALID_ARGUMENTS, "Confirmed quantity exceeds offered quantity")
    if merged["confirmed"] and any(merged[k] is None for k in ("unit_price_rub", "delivery_fee_rub", "delivery_date")):
        _reject(ToolErrorCode.MISSING_DATA, "Cannot confirm incomplete commercial terms")
    
    try:
        verified = verify_source(source, changes.model_copy(deep=True), SupplierOffer(**merged))
    except Exception:
        _reject(ToolErrorCode.EXTERNAL_SERVICE_ERROR, "Unable to authenticate supplier message")
    
    if verified is not True:
        _reject(ToolErrorCode.BUSINESS_RULE_VIOLATION, "Supplier source did not verify this update")
    
    for field, value in update.items():
        setattr(offer, field, value)
    
    session.flush()

    result = get_supplier_offers(session, organization_id, offer_id=offer_id)
    if not result.ok:
        raise _Rejected(result.error.code, result.error.message, "failed")
    
    checked = ToolResult[SupplierOfferRow](ok=True, data=result.data[0])

    _record(session, organization_id, "supplier_offer_updated", identity,
            {"request": request, "result": checked.model_dump(mode="json"),
             "supplier_id": source.supplier_id, "message_id": source.message_id})
    
    return checked


def dispatch_order(
    factory: sessionmaker, organization_id: str, submission_id: str, *,
    sender: OrderSender, as_of: date,
) -> ToolResult[PurchaseOrder]:
    """Send a committed intent once; reconcile uncertain attempts without resending.

    Owns and commits its transactions. A sender must durably deduplicate by
    (organization_id, submission_id). lookup must report unknown for absent or
    in-progress operations; failed/rejected must mean terminal, never sent.
    Inject the sender from trusted application context, not LLM arguments.
    """
    possible_external_effect = False

    try:
        try:
            now = current_event_time()
        except ValueError as exc:
            _reject(ToolErrorCode.MISSING_DATA, str(exc))
        
        if not isinstance(factory, sessionmaker) or not isinstance(factory.kw.get("bind"), Engine):
            _reject(ToolErrorCode.INVALID_ARGUMENTS, "Provide a session factory bound to an Engine, not an outer transaction")
        
        _text(organization_id, "organization_id")
        _text(submission_id, "submission_id")
    
        if type(as_of) is not date or not callable(getattr(sender, "send", None)) or not callable(getattr(sender, "lookup", None)):
            _reject(ToolErrorCode.INVALID_ARGUMENTS, "Provide an explicit date and trusted sender with send and lookup")
        
        with session_scope(factory) as session, localcontext() as context:
            context.prec = 64
            session.scalar(select(Organization).where(Organization.id == organization_id).with_for_update())
        
            intent = _locked(session, BusinessEvent, organization_id, submission_id)
            if intent.type != "order_submission" or not isinstance(intent.details, dict):
                _reject(ToolErrorCode.INVALID_ARGUMENTS, "Not a submission intent")
            
            details = dict(intent.details)
            order = _locked(session, StoredOrder, organization_id, details["order_id"])
            state = details["state"]
            recorded_at = details.get("observed_at") or details.get("attempted_at") or details.get("occurred_at")
            if recorded_at and now < datetime.fromisoformat(recorded_at):
                _reject(ToolErrorCode.INVALID_ARGUMENTS, "Runtime time precedes the recorded submission")
            
            possible_external_effect = state not in ("prepared", "rejected", "failed")
        
            if state == "sent":
                return ToolResult[PurchaseOrder](ok=True, data=_order_data(order))
            if state in ("rejected", "failed"):
                _reject(ToolErrorCode.EXTERNAL_SERVICE_ERROR, "Submission ended without sending; no automatic retry", "failed")
            if state not in ("prepared", "sending", "unknown"):
                _reject(ToolErrorCode.OPERATION_OUTCOME_UNKNOWN, "Unrecognized submission state; reconcile manually", "unknown")
            
            send_now = state == "prepared"
            if send_now:
                request = details["request"]
                _validate_submission(session, organization_id, order, request["offer_id"], as_of,
                                     date.fromisoformat(request["required_by"]))
            
                if _terms(session, organization_id, order) != details["terms"]:
                    _reject(ToolErrorCode.BUSINESS_RULE_VIOLATION, "Purchase terms changed since submission was staged")
                
                policy = session.scalar(select(OrganizationPolicy).where(
                    OrganizationPolicy.organization_id == organization_id).with_for_update()
                    .execution_options(populate_existing=True))
                policy.monthly_budget_remaining_rub -= order.total_rub
            
                details.update(state="sending", reserved_rub=format(order.total_rub, "f"), attempted_at=now.isoformat())
            
                _record(session, organization_id, "order_submission_started", submission_id,
                        {"submission_id": submission_id, "order_id": order.id, "state": "sending",
                         "order": _order_data(order).model_dump(mode="json")})
            
                payload = _order_data(order).model_copy(update={
                    "operation_id": request["operation_id"], "idempotency_key": request["idempotency_key"],
                })
            
                details["payload"] = payload.model_dump(mode="json")
                intent.details = details
            else:
                payload = PurchaseOrder.model_validate_json(json.dumps(details["payload"]))
            
            # Set before leaving the context: a failed COMMIT may have committed
            # the claim. No retry is safe until the durable attempt is inspected.
            possible_external_effect = True
        
        # No database transaction is held during the external call. The claim
        # and budget reservation are already committed and visible to peers.
        try:
            receipt = (sender.send if send_now else sender.lookup)(organization_id, submission_id, payload)
            if not isinstance(receipt, SubmissionReceipt):
                raise ValueError("Unvalidated receipt")
        
            receipt = SubmissionReceipt.model_validate(receipt.model_dump())
            if receipt.submission_id != submission_id:
                raise ValueError("Receipt belongs to another operation")
            if receipt.outcome == "sent" and (not receipt.reference or not receipt.reference.strip()):
                raise ValueError("Sent receipt lacks a reference")
        
        except Exception:
            receipt = SubmissionReceipt(submission_id=submission_id, outcome="unknown")
        with session_scope(factory) as session, localcontext() as context:
            context.prec = 64
            session.scalar(select(Organization).where(Organization.id == organization_id).with_for_update())
            intent = _locked(session, BusinessEvent, organization_id, submission_id)
            details = dict(intent.details)
            order = _locked(session, StoredOrder, organization_id, details["order_id"])
        
            if details["state"] == "sent":
                result = ToolResult[PurchaseOrder](ok=True, data=_order_data(order))
            elif details["state"] in ("failed", "rejected"):
                # Never overwrite a terminal result with an older lookup.
                result = ToolResult(ok=False, error=ToolError(
                    code=ToolErrorCode.OPERATION_OUTCOME_UNKNOWN if receipt.outcome == "sent" else ToolErrorCode.EXTERNAL_SERVICE_ERROR,
                    message="Terminal submission result already recorded; conflicting evidence requires review",
                    operation_outcome="unknown" if receipt.outcome == "sent" else "failed"))
            else:
                details.update(state=receipt.outcome, receipt=receipt.model_dump(mode="json"), observed_at=now.isoformat())
                if receipt.outcome == "sent":
                    order.status = "submitted"
                    result = ToolResult[PurchaseOrder](ok=True, data=_order_data(order))
                elif receipt.outcome in ("rejected", "failed"):
                    policy = session.scalar(select(OrganizationPolicy).where(
                        OrganizationPolicy.organization_id == organization_id).with_for_update()
                        .execution_options(populate_existing=True))
                
                    if policy is None or policy.monthly_budget_remaining_rub is None:
                        _reject(ToolErrorCode.OPERATION_OUTCOME_UNKNOWN, "Budget reservation cannot be released; reconcile", "unknown")
                    
                    policy.monthly_budget_remaining_rub += Decimal(details["reserved_rub"])
                
                    details["reserved_rub"] = None
                
                    result = ToolResult(ok=False, error=ToolError(
                        code=ToolErrorCode.EXTERNAL_SERVICE_ERROR,
                        message="Supplier refused the order" if receipt.outcome == "rejected" else "External operation failed without sending",
                        operation_outcome="failed"))
            
                else:
                    result = ToolResult(ok=False, error=ToolError(
                        code=ToolErrorCode.OPERATION_OUTCOME_UNKNOWN,
                        message="Submission outcome is unknown; only reconciliation is allowed",
                        operation_outcome="unknown"))
                
                intent.details = details
                identity = submission_id + ":" + receipt.outcome
            
                if _event(session, organization_id, "order_submission_result", identity) is None:
                    _record(session, organization_id, "order_submission_result", identity,
                            {"submission_id": submission_id, "order_id": order.id, "state": receipt.outcome,
                             "receipt": receipt.model_dump(mode="json"),
                             "order": _order_data(order).model_dump(mode="json")})
    
        # Successful return happens only after the final transaction committed.
        return result

    except _Rejected as exc:
        if possible_external_effect:
            return ToolResult(ok=False, error=ToolError(
                code=ToolErrorCode.OPERATION_OUTCOME_UNKNOWN,
                message="Submission may have taken effect; reconcile persisted intent",
                operation_outcome="unknown"))
    
        return ToolResult(ok=False, error=exc.error)

    except Exception:
        return ToolResult(ok=False, error=ToolError(
            code=ToolErrorCode.OPERATION_OUTCOME_UNKNOWN if possible_external_effect else ToolErrorCode.DATABASE_ERROR,
            message="Unable to finalize submission; reconcile persisted intent" if possible_external_effect else "Unable to prepare submission",
            operation_outcome="unknown" if possible_external_effect else "failed"))


@_write
def record_approval_decision(session: Session, organization_id: str, *, approval_id: str) -> ToolResult[ApprovalRecord]:
    """Journal an already persisted trusted manager decision; never grant approval."""

    row = _locked(session, ApprovalRequest, organization_id, approval_id)
    try:
        now = current_event_time()
    except ValueError as exc:
        _reject(ToolErrorCode.MISSING_DATA, str(exc))
    
    if (row.status not in ("approved", "rejected") or row.decision != row.status
            or not row.decided_by or not row.decided_by.strip() or row.decided_at is None
            or not row.created_at <= row.decided_at <= now):
        _reject(ToolErrorCode.MISSING_DATA, "A complete trusted manager decision must exist before journaling")
    
    details = {"approval_id": row.id, "order_id": row.order_id, "state": row.status,
               "decided_by": row.decided_by, "decided_at": row.decided_at.isoformat()}

    previous = _event(session, organization_id, "approval_decided", row.id)
    if previous is not None:
        if any(previous.details.get(k) != v for k, v in details.items()):
            _reject(ToolErrorCode.DUPLICATE_OPERATION, "Recorded decision is immutable; review conflicting changes")
    else:
        with event_time(row.decided_at):
            _record(session, organization_id, "approval_decided", row.id, dict(details, recorded_at=now.isoformat()))
    
    return ToolResult[ApprovalRecord](ok=True, data={field: getattr(row, field) for field in ApprovalRecord.__annotations__})
