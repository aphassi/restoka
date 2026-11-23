"""Deterministic external-service doubles and committed message delivery."""

import copy
from datetime import date, datetime
from hashlib import sha256
import json
import logging
from threading import RLock
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from database.database import session_scope
from database.models import BusinessEvent, Organization, Supplier, SupplierOffer as StoredOffer
from schemas import PurchaseOrder, SupplierOffer
from .actions import (
    SubmissionReceipt, SupplierMessageSource, SupplierOfferPatch,
    _Rejected, _event_id, _locked, _reject, _text, _write, _record, _event, update_supplier_offer,
)
from .contracts import ToolError, ToolErrorCode, ToolResult
from .queries import SupplierOfferRow, _read_only

__all__ = [
    "MockOrderSender", "MockMessageChannel", "MessageReceipt", "MessageDelivery",
    "MockSupplierInbox", "SupplierReply", "get_supplier_reply", "send_supplier_message",
    "receive_supplier_reply", "process_supplier_reply",
]


def _key(org, operation):
    return sha256(json.dumps([org, operation], ensure_ascii=False).encode()).hexdigest()


class MockOrderSender:
    """Simulate idempotent order submission without network or wall-clock access."""

    def __init__(self, outcome="sent"):
        SubmissionReceipt(submission_id="configuration", outcome=outcome)
        self.outcome = outcome
        self.send_calls = 0
        self.lookup_calls = 0
        self.send_attempts = 0
        self.records = {}
        self._payloads = {}
        self.lock = RLock()

    def send(self, org, key, order: PurchaseOrder) -> SubmissionReceipt:
        """Return one receipt per operation; reject conflicting payload reuse."""
        _text(org, "organization_id")
        _text(key, "submission_id")
        order = PurchaseOrder.model_validate(order.model_dump())
        with self.lock:
            self.send_calls += 1
            identity = (org, key)
            payload = order.model_dump(mode="json")
            if identity in self._payloads and self._payloads[identity] != payload:
                raise ValueError("Submission identifier was reused for another order")
            if identity not in self.records:
                self.send_attempts += 1
                self._payloads[identity] = payload
                self.records[identity] = SubmissionReceipt(
                    submission_id=key, outcome=self.outcome,
                    reference="mock-order:" + _key(org, key) if self.outcome == "sent" else None,
                )
            return self.records[identity].model_copy(deep=True)

    def lookup(self, org, key, order: PurchaseOrder) -> SubmissionReceipt:
        """Report unknown when no durable mock observation is available."""
        with self.lock:
            self.lookup_calls += 1
            if (org, key) in self._payloads and self._payloads[org, key] != order.model_dump(mode="json"):
                raise ValueError("Submission payload conflicts with the recorded operation")
            return self.records.get((org, key), SubmissionReceipt(
                submission_id=key, outcome="unknown")).model_copy(deep=True)

    def _snapshot_state(self):
        with self.lock:
            return (self.outcome, self.send_calls, self.lookup_calls, self.send_attempts,
                    {key: value.model_copy(deep=True) for key, value in self.records.items()},
                    copy.deepcopy(self._payloads))

    def _restore_state(self, state):
        with self.lock:
            (self.outcome, self.send_calls, self.lookup_calls, self.send_attempts,
             self.records, self._payloads) = state

    def validate_resolve(self, org, key, outcome):
        SubmissionReceipt(submission_id="configuration", outcome=outcome)
        with self.lock:
            old = self.records.get((org, key))
            if old is None or old.outcome not in ("unknown", outcome):
                raise ValueError("Only known unresolved operations can acquire a new outcome")

    def resolve(self, org, key, outcome):
        """Seed a later service observation from trusted scenario code."""
        self.validate_resolve(org, key, outcome)
        with self.lock:
            self.records[org, key] = SubmissionReceipt(
                submission_id=key, outcome=outcome,
                reference="mock-order:" + _key(org, key) if outcome == "sent" else None,
            )


class MessageReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation_id: str = Field(min_length=1, max_length=255)
    status: Literal["sent", "delivered", "rejected", "failed", "unknown"]
    reference: str | None = None
    sent_at: datetime | None = None
    delivered_at: datetime | None = None


class MessageDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    event_id: str
    operation_id: str
    status: Literal["prepared", "sending", "sent", "delivered", "rejected", "failed", "unknown"]
    reference: str | None
    sent_at: datetime | None
    delivered_at: datetime | None
    body: str


def _aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


class MockMessageChannel:
    """Simulate message sending and delivery at explicit scenario times."""

    def __init__(self, status="sent", *, delivered_at: datetime | None = None):
        MessageReceipt(operation_id="configuration", status=status)
        if status == "delivered" and not _aware(delivered_at):
            raise ValueError("Delivered scenarios require an explicit aware timestamp")
        self.status, self.delivered_at = status, delivered_at
        self.records, self._payloads = {}, {}
        self.send_calls = self.lookup_calls = self.send_attempts = 0
        self.lock = RLock()

    def send(self, org, key, payload, now) -> MessageReceipt:
        """Deduplicate message operations independently of database rollback."""
        if not _aware(now):
            raise ValueError("An explicit aware send time is required")
        with self.lock:
            self.send_calls += 1
            identity = (org, key)
            if identity in self._payloads and self._payloads[identity] != payload:
                raise ValueError("Message identifier conflicts with its original payload")
            if identity not in self.records:
                if self.status == "delivered" and self.delivered_at < now:
                    raise ValueError("Delivery cannot precede sending")
                self.send_attempts += 1
                self._payloads[identity] = json.loads(json.dumps(payload))
                self.records[identity] = MessageReceipt(
                    operation_id=key, status=("sent" if self.status == "delivered" and self.delivered_at > now else self.status),
                    reference="mock-message:" + _key(org, key) if self.status in ("sent", "delivered") else None,
                    sent_at=now if self.status in ("sent", "delivered") else None,
                    delivered_at=self.delivered_at if self.status == "delivered" and self.delivered_at <= now else None,
                )
            return self.records[identity].model_copy(deep=True)

    def lookup(self, org, key, payload, now) -> MessageReceipt:
        """Return recorded evidence without treating absence as a failure."""
        with self.lock:
            self.lookup_calls += 1
            if (org, key) in self._payloads and self._payloads[org, key] != payload:
                raise ValueError("Message payload conflicts with the recorded operation")
            previous = self.records.get((org, key))
            if (previous is not None and previous.status == "sent" and self.status == "delivered"
                    and self.delivered_at <= now):
                self.records[org, key] = previous.model_copy(update={"status": "delivered", "delivered_at": self.delivered_at})
            return self.records.get((org, key), MessageReceipt(
                operation_id=key, status="unknown")).model_copy(deep=True)

    def _snapshot_state(self):
        with self.lock:
            return (self.status, self.delivered_at,
                    {key: value.model_copy(deep=True) for key, value in self.records.items()},
                    copy.deepcopy(self._payloads), self.send_calls, self.lookup_calls, self.send_attempts)

    def _restore_state(self, state):
        with self.lock:
            (self.status, self.delivered_at, self.records, self._payloads,
             self.send_calls, self.lookup_calls, self.send_attempts) = state

    def validate_resolve(self, org, key, status, *, observed_at: datetime):
        if not _aware(observed_at):
            raise ValueError("An explicit aware observation time is required")
        MessageReceipt(operation_id=key, status=status)
        with self.lock:
            previous = self.records.get((org, key))
            if previous is None or previous.status not in ("unknown", "sent", status):
                raise ValueError("A terminal delivery result cannot be changed")
            if previous.status == "sent" and status not in ("sent", "delivered"):
                raise ValueError("Confirmed sending cannot be revoked by a later observation")

    def resolve(self, org, key, status, *, observed_at: datetime):
        """Supply a later authoritative observation from the scenario."""
        self.validate_resolve(org, key, status, observed_at=observed_at)
        with self.lock:
            previous = self.records.get((org, key))
            if previous.status in ("delivered", "rejected", "failed"):
                return  # A repeated terminal observation must preserve its timestamp.
            self.records[org, key] = MessageReceipt(
                operation_id=key, status=status,
                reference="mock-message:" + _key(org, key) if status in ("sent", "delivered") else None,
                sent_at=previous.sent_at or (observed_at if status in ("sent", "delivered") else None),
                delivered_at=observed_at if status == "delivered" else None,
            )


def _delivery(event):
    data = event.details
    receipt = data.get("receipt", {})
    return MessageDelivery(
        event_id=event.id, operation_id=data["request"]["operation_id"], status=data["state"],
        reference=receipt.get("reference"), body=data["request"]["body"],
        sent_at=datetime.fromisoformat(receipt["sent_at"]) if receipt.get("sent_at") else None,
        delivered_at=datetime.fromisoformat(receipt["delivered_at"]) if receipt.get("delivered_at") else None,
    )


def _message_result(event):
    data = _delivery(event)
    if data.status in ("unknown", "sending"):
        return ToolResult(ok=False, error=ToolError(code=ToolErrorCode.OPERATION_OUTCOME_UNKNOWN,
            message="Message result is unknown; reconcile this operation without sending again",
            operation_id=data.operation_id, operation_outcome="unknown"))
    if data.status in ("failed", "rejected"):
        return ToolResult(ok=False, error=ToolError(code=ToolErrorCode.EXTERNAL_SERVICE_ERROR,
            message="Message was rejected" if data.status == "rejected" else "Message operation failed without sending",
            operation_id=data.operation_id, operation_outcome="failed"))
    return ToolResult[MessageDelivery](ok=True, data=data)


def _send_message(factory, org, *, purpose, destination, body, operation_id, now, channel,
                  report_date=None, facts_digest=None, facts_reader=None):
    # Shared by supplier messages and review delivery; this function owns all
    # commits. No channel method runs before the durable sending claim commits.
    possible_effect = False
    try:
        if not isinstance(factory, sessionmaker) or not isinstance(factory.kw.get("bind"), Engine):
            _reject(ToolErrorCode.INVALID_ARGUMENTS, "Provide a factory bound to an Engine")
        _text(org, "organization_id")
        _text(operation_id, "operation_id", 255)
        if not isinstance(body, str) or not body.strip() or not _aware(now):
            _reject(ToolErrorCode.INVALID_ARGUMENTS, "Provide nonblank text and an explicit aware scenario time")
        if not callable(getattr(channel, "send", None)) or not callable(getattr(channel, "lookup", None)):
            _reject(ToolErrorCode.INVALID_ARGUMENTS, "A trusted message channel is required")
        if purpose == "supplier_message":
            _text(destination, "supplier_id")
        request = dict(purpose=purpose, destination=destination, body=body, operation_id=operation_id,
                       report_date=report_date.isoformat() if report_date else None)
        identity = _event_id(purpose, report_date.isoformat() if purpose == "daily_review" else operation_id)
        with session_scope(factory) as session:
            organization = session.scalar(select(Organization).where(Organization.id == org).with_for_update())
            if organization is None:
                _reject(ToolErrorCode.NOT_FOUND, "Organization not found")
            zone = ZoneInfo(organization.timezone)
            if report_date is not None and report_date >= now.astimezone(zone).date():
                _reject(ToolErrorCode.INVALID_ARGUMENTS, "Final review requires a completed local calendar day")
            if purpose == "supplier_message":
                _locked(session, Supplier, org, destination)
            entry = session.get(BusinessEvent, (org, identity))
            if purpose == "daily_review":
                # Reconcile old operation-keyed reviews as well as the new per-day key.
                earlier = list(session.scalars(select(BusinessEvent).where(
                    BusinessEvent.organization_id == org, BusinessEvent.type == "daily_review",
                    BusinessEvent.details["request"]["report_date"].as_string() == report_date.isoformat())))
                if len(earlier) > 1:
                    _reject(ToolErrorCode.DUPLICATE_OPERATION, "Multiple legacy reviews exist for this day; manual reconciliation required")
                if earlier:
                    entry = earlier[0]
                    identity = entry.id
                if entry is not None:
                    request["operation_id"] = entry.details["request"]["operation_id"]
                if entry is None or "facts_digest" in entry.details["request"]:
                    request["facts_digest"] = facts_digest
            if entry is None:
                if purpose == "daily_review":
                    if not callable(facts_reader):
                        _reject(ToolErrorCode.INVALID_ARGUMENTS, "A trusted daily facts reader is required")
                    facts = facts_reader(session, org, report_date=report_date)
                    if not facts.ok:
                        raise _Rejected(facts.error.code, facts.error.message)
                    if not facts_digest or facts_digest != facts.data.facts_digest:
                        _reject(ToolErrorCode.INVALID_ARGUMENTS, "Provide the current daily facts digest; text accuracy remains caller responsibility")
                entry = BusinessEvent(organization_id=org, id=identity, type=purpose,
                    date=now.astimezone(zone).date(), details={"request": request, "state": "prepared",
                    "prepared_at": now.isoformat(), "occurred_at": now.isoformat(), "receipt": None,
                    "initial_snapshot": {"request": request, "state": "prepared", "occurred_at": now.isoformat()}})
                session.add(entry)
            elif entry.type != purpose or entry.details.get("request") != request:
                _reject(ToolErrorCode.DUPLICATE_OPERATION, "Operation identifier conflicts with the stored message")
            elif now < datetime.fromisoformat(entry.details.get("observed_at") or entry.details["prepared_at"]):
                _reject(ToolErrorCode.INVALID_ARGUMENTS, "Scenario time cannot precede the recorded message observation")
        with session_scope(factory) as session:
            session.scalar(select(Organization).where(Organization.id == org).with_for_update())
            entry = _locked(session, BusinessEvent, org, identity)
            state = entry.details["state"]
            if state in ("delivered", "failed", "rejected"):
                return _message_result(entry)
            if state not in ("prepared", "sending", "unknown", "sent"):
                _reject(ToolErrorCode.OPERATION_OUTCOME_UNKNOWN, "Unrecognized message state", "unknown")
            send_now = state == "prepared"
            if send_now:
                entry.details = dict(entry.details, state="sending", attempted_at=now.isoformat())
                session.add(BusinessEvent(organization_id=org, id=_event_id(purpose + "_started", identity),
                    type=purpose + "_started", date=now.astimezone(zone).date(),
                    details={"message_event_id": identity, "state": "sending", "occurred_at": now.isoformat()}))
            possible_effect = True
        try:
            # Event identity namespaces supplier and review operation IDs.
            receipt = (channel.send if send_now else channel.lookup)(org, identity, request, now)
            if not isinstance(receipt, MessageReceipt):
                raise ValueError("Unvalidated message receipt")
            receipt = MessageReceipt.model_validate(receipt.model_dump())
            if receipt.operation_id != identity:
                raise ValueError("Receipt belongs to another message")
            if receipt.status in ("sent", "delivered") and (
                not receipt.reference or not receipt.reference.strip() or not _aware(receipt.sent_at)
                or receipt.sent_at > now
            ):
                raise ValueError("Sending evidence is incomplete or in the future")
            if receipt.status == "delivered" and (
                not _aware(receipt.delivered_at) or receipt.delivered_at < receipt.sent_at or receipt.delivered_at > now
            ):
                raise ValueError("Delivery evidence is invalid or in the future")
        except Exception:
            logging.getLogger(__name__).exception("Message channel returned no usable evidence")
            receipt = MessageReceipt(operation_id=identity, status="unknown")
        with session_scope(factory) as session:
            session.scalar(select(Organization).where(Organization.id == org).with_for_update())
            entry = _locked(session, BusinessEvent, org, identity)
            stored_state = entry.details["state"]
            if stored_state not in ("delivered", "failed", "rejected"):
                # Never downgrade already verified sending on an older lookup.
                if stored_state != "sent" or receipt.status == "delivered":
                    entry.details = dict(entry.details, state=receipt.status, receipt=receipt.model_dump(mode="json"),
                                         observed_at=now.isoformat())
                    observation_id = _event_id(purpose + "_result", identity + ":" + receipt.status)
                    if session.get(BusinessEvent, (org, observation_id)) is None:
                        session.add(BusinessEvent(organization_id=org, id=observation_id, type=purpose + "_result",
                            date=now.astimezone(zone).date(), details={"message_event_id": identity,
                            "state": receipt.status, "receipt": receipt.model_dump(mode="json"),
                            "observed_at": now.isoformat(), "occurred_at": now.isoformat()}))
            result = _message_result(entry)
        return result
    except _Rejected as exc:
        if not possible_effect:
            return ToolResult(ok=False, error=exc.error)
    except SQLAlchemyError:
        logging.getLogger(__name__).exception("Message persistence failed")
        if not possible_effect:
            return ToolResult(ok=False, error=ToolError(code=ToolErrorCode.DATABASE_ERROR,
                message="Unable to persist message preparation", operation_outcome="failed"))
    except Exception:
        logging.getLogger(__name__).exception("Unexpected message operation failure")
        if not possible_effect:
            return ToolResult(ok=False, error=ToolError(code=ToolErrorCode.INTERNAL_ERROR,
                message="Internal message operation failure", operation_outcome="failed"))
    return ToolResult(ok=False, error=ToolError(code=ToolErrorCode.OPERATION_OUTCOME_UNKNOWN,
        message="Message may have taken effect; reconcile the persisted operation", operation_outcome="unknown"))


def send_supplier_message(
    factory: sessionmaker, organization_id: str, *, supplier_id: str, text: str,
    operation_id: str, now: datetime, channel: MockMessageChannel,
) -> ToolResult[MessageDelivery]:
    """Persist and send one supplier message through a trusted mock channel."""
    return _send_message(factory, organization_id, purpose="supplier_message", destination=supplier_id,
                         body=text, operation_id=operation_id, now=now, channel=channel)


class SupplierReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    organization_id: str
    supplier_id: str
    offer_id: str
    message_id: str
    revision: int
    text: str


class MockSupplierInbox:
    """Hold trusted synthetic messages and their authenticated structured facts."""

    def __init__(self):
        self._messages = {}
        self._lock = RLock()

    def _snapshot_state(self):
        with self._lock:
            return copy.deepcopy(self._messages)

    def _restore_state(self, state):
        with self._lock:
            self._messages = state

    def _validated_entry(self, source, text, patch, confirmed_terms):
        if not isinstance(source, SupplierMessageSource) or not isinstance(text, str) or not text.strip():
            raise ValueError("Provide a trusted source and nonblank original text")
        for field in ("organization_id", "supplier_id", "offer_id", "message_id"):
            _text(getattr(source, field), field, 255 if field == "message_id" else 100)
        if type(source.revision) is not int or source.revision < 1:
            raise ValueError("Revision must be a positive integer")
        patch = SupplierOfferPatch.model_validate(patch.model_dump())
        confirmed_terms = (SupplierOffer.model_validate(confirmed_terms.model_dump())
                           if confirmed_terms is not None else None)
        if patch.confirmed is True and confirmed_terms is None:
            raise ValueError("Explicit confirmation requires authenticated full terms")
        return source, text, patch, confirmed_terms

    def validate_add(self, source, text, patch, *, confirmed_terms=None):
        entry = self._validated_entry(source, text, patch, confirmed_terms)
        key = (source.organization_id, source.supplier_id, source.message_id)
        with self._lock:
            if key in self._messages and self._messages[key] != entry:
                raise ValueError("Message identity conflicts with registered evidence")
            for old, *_ in self._messages.values():
                if (old.organization_id, old.offer_id, old.revision) == (source.organization_id, source.offer_id, source.revision) and old != source:
                    raise ValueError("Offer revision already belongs to another message")
        return entry

    def add(self, source: SupplierMessageSource, text: str, patch: SupplierOfferPatch,
            *, confirmed_terms: SupplierOffer | None = None):
        """Register scenario evidence outside LLM-controlled tool arguments."""
        entry = self.validate_add(source, text, patch, confirmed_terms=confirmed_terms)
        key = (source.organization_id, source.supplier_id, source.message_id)
        with self._lock:
            self._messages[key] = entry

    def get(self, org, supplier, message_id) -> SupplierReply | None:
        """Return text and references, without exposing verification authority."""
        with self._lock:
            entry = self._messages.get((org, supplier, message_id))
            if entry is None:
                return None
            source, text, *_ = entry
            return SupplierReply(organization_id=org, supplier_id=supplier, offer_id=source.offer_id,
                                 message_id=message_id, revision=source.revision, text=text)

    def verify_source(self, source, patch, resulting_offer) -> bool:
        """Match extracted updates against registered source facts and freshness."""
        with self._lock:
            entry = self._messages.get((source.organization_id, source.supplier_id, source.message_id))
            if entry is None or entry[0] != source or entry[2] != patch:
                return False
            if resulting_offer.id != source.offer_id or resulting_offer.supplier_id != source.supplier_id:
                return False
            if any(s.organization_id == source.organization_id and s.offer_id == source.offer_id
                   and s.revision > source.revision for s, *_ in self._messages.values()):
                return False
            return patch.confirmed is not True or entry[3] == resulting_offer


@_read_only
def get_supplier_reply(session: Session, organization_id: str, *, supplier_id: str,
                       message_id: str, inbox: MockSupplierInbox) -> ToolResult[SupplierReply]:
    """Read a synthetic reply belonging to this organization's supplier."""
    if not isinstance(inbox, MockSupplierInbox) or not all(isinstance(v, str) and v.strip() for v in (supplier_id, message_id)):
        return ToolResult(ok=False, error=ToolError(code=ToolErrorCode.INVALID_ARGUMENTS,
            message="Provide supplier/message identifiers and a trusted inbox", operation_outcome="not_started"))
    if session.get(Supplier, (organization_id, supplier_id)) is None:
        return ToolResult(ok=False, error=ToolError(code=ToolErrorCode.NOT_FOUND,
            message="Supplier not found in this organization", operation_outcome="not_started"))
    reply = inbox.get(organization_id, supplier_id, message_id)
    if reply is None:
        return ToolResult(ok=False, error=ToolError(code=ToolErrorCode.NOT_FOUND,
            message="Supplier message not found", operation_outcome="not_started"))
    return ToolResult[SupplierReply](ok=True, data=reply)


@_write
def receive_supplier_reply(session: Session, organization_id: str, *, supplier_id: str,
                           message_id: str, inbox: MockSupplierInbox) -> ToolResult[SupplierReply]:
    """Persist authenticated receipt separately from any application of its terms."""
    result = get_supplier_reply(session, organization_id, supplier_id=supplier_id,
                                message_id=message_id, inbox=inbox)
    if not result.ok:
        return result
    identity = json.dumps([supplier_id, message_id])
    previous = _event(session, organization_id, "supplier_reply_received", identity)
    reply = result.data.model_dump(mode="json")
    if previous is not None:
        if previous.details["reply"] != reply:
            _reject(ToolErrorCode.DUPLICATE_OPERATION, "Incoming message conflicts with recorded source")
    else:
        offer = _locked(session, StoredOffer, organization_id, result.data.offer_id)
        if offer.supplier_id != supplier_id:
            _reject(ToolErrorCode.BUSINESS_RULE_VIOLATION, "Message source does not own this offer")
        _record(session, organization_id, "supplier_reply_received", identity,
                {"reply": reply, "supplier_id": supplier_id, "offer_id": result.data.offer_id,
                 "message_id": message_id, "revision": result.data.revision, "state": "received"})
    return result


@_write
def process_supplier_reply(session: Session, organization_id: str, *, supplier_id: str,
                           message_id: str, inbox: MockSupplierInbox, expected: SupplierOffer,
                           changes: SupplierOfferPatch) -> ToolResult:
    """Authenticate, journal receipt, apply verified terms, and journal the actual result."""
    received = receive_supplier_reply(session, organization_id, supplier_id=supplier_id,
                                      message_id=message_id, inbox=inbox)
    if not received.ok:
        return received
    session.flush()
    identity = json.dumps([supplier_id, message_id])
    request = {"expected": expected.model_dump(mode="json"), "changes": changes.model_dump(mode="json")}
    # Keep compatibility with successes and failures written by the old implementation.
    previous = (_event(session, organization_id, "supplier_reply_applied", identity)
                or _event(session, organization_id, "supplier_reply_processed", identity))
    if previous is not None and previous.details["result"]["ok"]:
        if previous.details["request"] != request:
            _reject(ToolErrorCode.DUPLICATE_OPERATION, "Message processing arguments changed")
        return ToolResult[SupplierOfferRow].model_validate_json(json.dumps(previous.details["result"]))
    safe_codes = {ToolErrorCode.INVALID_ARGUMENTS, ToolErrorCode.BUSINESS_RULE_VIOLATION,
                  ToolErrorCode.DUPLICATE_OPERATION, ToolErrorCode.MISSING_DATA, ToolErrorCode.NOT_FOUND}
    uncertain = _event(session, organization_id, "supplier_reply_processing_unknown", identity)
    if previous is not None:
        error = previous.details["result"].get("error") or {}
        if error.get("operation_outcome") == "unknown" or error.get("code") not in safe_codes:
            uncertain = previous
    if uncertain is not None:
        # A technical failure is not proof that it is safe to apply another patch.
        _reject(ToolErrorCode.OPERATION_OUTCOME_UNKNOWN,
                "Reconcile the recorded supplier update before retrying this message", "unknown")
    reply = received.data
    source = SupplierMessageSource(organization_id, supplier_id, reply.offer_id, message_id, reply.revision)
    result = update_supplier_offer(session, organization_id, offer_id=reply.offer_id,
        expected=expected, changes=changes, source=source, verify_source=inbox.verify_source)
    details = {"supplier_id": supplier_id, "offer_id": reply.offer_id, "message_id": message_id,
               "revision": reply.revision, "request": request, "result": result.model_dump(mode="json"),
               "state": "processed" if result.ok else "failed",
               "error": result.error.model_dump(mode="json") if result.error else None}
    if result.ok:
        _record(session, organization_id, "supplier_reply_applied", identity, details)
    elif result.error.operation_outcome == "unknown" or result.error.code not in safe_codes:
        _record(session, organization_id, "supplier_reply_processing_unknown", identity,
                dict(details, state="unknown"))
    else:
        attempt_id = identity + ":" + sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        if _event(session, organization_id, "supplier_reply_processing_failed", attempt_id) is None:
            _record(session, organization_id, "supplier_reply_processing_failed", attempt_id, details)
    return result
