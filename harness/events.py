"""Trusted synthetic event adapters, never exposed as LLM tool arguments."""

from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from sqlalchemy import select

from database.database import session_scope
from database.history import add_message, get_recent_messages
from database.models import ApprovalRequest, BusinessEvent, Conversation, Organization, SupplierOffer
from database.runtime import event_time
from schemas import SupplierOffer as Offer
from tools.actions import SupplierMessageSource, SupplierOfferPatch, record_approval_decision
from tools.external import MockMessageChannel
from .contracts import RunResult
from .trace import json_value


def _identity(event):
    return "harness-event:" + sha256(event.event_id.encode()).hexdigest()


def _digest(event):
    payload = event.model_dump(mode="json", exclude={"at"})
    return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _mock_state(mocks):
    return (mocks.order_sender._snapshot_state(), mocks.message_channel._snapshot_state(),
            mocks.supplier_inbox._snapshot_state())


def _restore_mocks(mocks, state):
    mocks.order_sender._restore_state(state[0])
    mocks.message_channel._restore_state(state[1])
    mocks.supplier_inbox._restore_state(state[2])


def apply_event(factory, org, event, mocks):
    identity = _identity(event)

    # Delivery time can change on a redelivery, but trusted payload cannot.
    digest = _digest(event)
    reply = event.supplier
    source = patch = terms = None
    if reply:
        source = SupplierMessageSource(org, reply.supplier_id, reply.offer_id,
                                       reply.message_id, reply.revision)
        patch = SupplierOfferPatch.model_validate_json(json.dumps(reply.changes))
        terms = Offer.model_validate_json(json.dumps(reply.confirmed_terms)) if reply.confirmed_terms else None

    checked_channel = None
    if event.mocks and event.mocks.message_status:
        checked_channel = MockMessageChannel(event.mocks.message_status, delivered_at=event.mocks.delivered_at)

    state = _mock_state(mocks)
    try:
        with event_time(event.at), session_scope(factory) as session:
            organization = session.scalar(select(Organization).where(Organization.id == org).with_for_update())

            previous = session.get(BusinessEvent, (org, identity))
            if previous:
                if previous.details["digest"] != digest:
                    raise ValueError("Event identity conflicts with the recorded payload")

                return True

            if event.manager:
                decision = event.manager

                row = session.get(ApprovalRequest, (org, decision.approval_id))
                if row is None or row.created_at > (event.occurred_at or event.at):
                    raise ValueError("Manager decision needs an existing, earlier approval request")

                if row.status == "pending":
                    row.status = row.decision = decision.decision
                    row.decided_by, row.decided_at = decision.decided_by, (event.occurred_at or event.at)
                    session.flush()
                elif (row.status != decision.decision or row.decision != decision.decision
                      or row.decided_by != decision.decided_by):
                    raise ValueError("Cannot overwrite an existing manager decision")

                result = record_approval_decision(session, org, approval_id=row.id)
                if not result.ok:
                    raise ValueError("Manager decision could not be journaled")

            if reply:
                offer = session.get(SupplierOffer, (org, reply.offer_id))
                if offer is None or offer.supplier_id != reply.supplier_id:
                    raise ValueError("Supplier message does not belong to this offer and organization")
                mocks.supplier_inbox.validate_add(source, reply.text, patch, confirmed_terms=terms)

            external_row = None
            if event.external:
                result = event.external
                kinds = ("order_submission",) if result.service == "order" else ("supplier_message", "daily_review")
                rows = list(session.scalars(select(BusinessEvent).where(BusinessEvent.organization_id == org,
                    BusinessEvent.type.in_(kinds),
                    BusinessEvent.details["request"]["operation_id"].as_string() == result.operation_id)))
                if len(rows) != 1:
                    raise ValueError("External result needs one existing business operation")
                external_row = rows[0]
                if result.service == "order":
                    if result.outcome == "delivered":
                        raise ValueError("Order sender only confirms submission, not delivery")
                    mocks.order_sender.validate_resolve(org, external_row.id, result.outcome)
                else:
                    mocks.message_channel.validate_resolve(org, external_row.id, result.outcome,
                                                           observed_at=event.occurred_at or event.at)

            if reply:
                mocks.supplier_inbox.add(source, reply.text, patch, confirmed_terms=terms)
                # Registration authenticates the source; business tools must still apply/verify it.
            if event.external:
                if event.external.service == "order":
                    mocks.order_sender.resolve(org, external_row.id, event.external.outcome)
                else:
                    mocks.message_channel.resolve(org, external_row.id, event.external.outcome,
                        observed_at=event.occurred_at or event.at)

            if event.mocks:
                if event.mocks.order_outcome:
                    with mocks.order_sender.lock:
                        mocks.order_sender.outcome = event.mocks.order_outcome
                if checked_channel:
                    with mocks.message_channel.lock:
                        mocks.message_channel.status = checked_channel.status
                        mocks.message_channel.delivered_at = checked_channel.delivered_at

            session.add(BusinessEvent(organization_id=org, id=identity, type="harness_event_received",
                date=event.at.astimezone(ZoneInfo(organization.timezone)).date(),
                details={"event_id": event.event_id, "kind": event.kind, "digest": digest,
                         "occurred_at": event.at.isoformat(), "process_id": event.process_id,
                         "processing_state": "applied"}))
    except Exception:
        _restore_mocks(mocks, state)
        raise
    
    return False


def processed_event_result(factory, org, event):
    with session_scope(factory) as session:
        row = session.get(BusinessEvent, (org, _identity(event)))
        if row is None or row.details.get("digest") != _digest(event):
            raise ValueError("Event processing evidence does not match the delivered event")
        if row.details.get("processing_state") != "processed":
            return None
        return RunResult.model_validate(row.details["result"])


def record_event_processed(factory, org, event, result):
    result = RunResult.model_validate(result)
    with session_scope(factory) as session:
        row = session.scalar(select(BusinessEvent).where(
            BusinessEvent.organization_id == org,
            BusinessEvent.id == _identity(event),
        ).with_for_update())
        if row is None or row.details.get("digest") != _digest(event):
            raise ValueError("Cannot complete an event without matching application evidence")
        
        stored = row.details.get("result")
        if row.details.get("processing_state") == "processed":
            if stored != result.model_dump(mode="json"):
                raise ValueError("Processed event result is immutable")
            details = row.details
        else:
            details = dict(row.details, processing_state="processed",
                           processed_at=event.at.isoformat(), result=result.model_dump(mode="json"))
            row.details = details
        
        processed_id = row.id
    
    return processed_id, json_value(details)


class HistoryAccess:
    def __init__(self, factory, org, event, trace):
        self._factory, self._org, self._event, self._trace = factory, org, event, trace
        self._message_counts = {}
        with session_scope(factory) as session:
            row = session.get(Conversation, (org, event.conversation_id))
            if row is None:
                session.add(Conversation(organization_id=org, id=event.conversation_id,
                    user_id=event.user_id, created_at=event.at))
            elif row.user_id != event.user_id:
                raise ValueError("Conversation belongs to another user")

    def recent(self):
        with session_scope(self._factory) as session:
            result = get_recent_messages(session, self._org, self._event.user_id, self._event.conversation_id)
    
        self._trace.event("recovery", source="conversation", ok=result.ok)
        
        return result

    def add(self, message, *, message_id=None):
        role = getattr(message, "role", "unknown")
        ordinal = self._message_counts.get(role, 0)
        
        if message_id is None:
            value = json.dumps([self._event.conversation_id, self._event.event_id, role, ordinal])
            message_id = "harness-message:" + sha256(value.encode()).hexdigest()
        
        with session_scope(self._factory) as session:
            result = add_message(session, self._org, self._event.user_id, self._event.conversation_id,
                message=message, now=self._event.at, message_id=message_id)
        
        if result.ok:
            self._message_counts[role] = ordinal + 1
        
        return result
