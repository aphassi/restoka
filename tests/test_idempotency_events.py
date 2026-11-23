"""Restoka KT3: replay / idempotency integration tests, isolated PostgreSQL schemas.
"""
import copy
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from database.database import session_scope
from database.models import ApprovalRequest, BusinessEvent, PurchaseOrder as StoredOrder
from database.runtime import event_time
from harness.contracts import ManagerDecision, ScenarioEvent
from harness.events import apply_event
from harness.gateway import MockServices
from tools.actions import create_order, dispatch_order, request_approval
from tools.external import MockOrderSender

from tests.test_purchase_policy import (
    AS_OF, REQUIRED_BY, isolated_fixture, draft_order, stage_order,
    remaining_budget, require_ok, require_blocked, submitted_orders,
)


def assert_error_code(result, keyword):
    assert not result.ok, f"Expected {keyword} error, got success: {result}"
    actual = str(result.error.code).lower()
    assert keyword.lower() in actual, f"Expected {keyword}; got {actual}"


def count_rows(factory, model, org):
    with session_scope(factory) as session:
        return len(list(session.scalars(select(model).where(model.organization_id == org))))


def build_plan(factory, org, now, tag):
    draft = draft_order(factory, org, now, tag=tag)
    plan = require_ok(stage_order(factory, org, now, draft.id, tag=tag), "create_order")
    assert plan.state == "prepared"
    return draft, plan


def test_replayed_submission_intent(fixture):
    with isolated_fixture(fixture) as (factory, org, now):
        draft, plan = build_plan(factory, org, now, "replay-intent")
        baseline_events = count_rows(factory, BusinessEvent, org)
        replay = require_ok(stage_order(factory, org, now, draft.id, tag="replay-intent"),
                            "same create_order retry")
        assert replay.submission_id == plan.submission_id
        assert count_rows(factory, BusinessEvent, org) == baseline_events

        # A second operation for the SAME order with distinct IDs must be refused.
        require_blocked(stage_order(factory, org, now, draft.id, tag="different-intent"),
                        "second submission attempt for same order")
        assert count_rows(factory, BusinessEvent, org) == baseline_events
        assert remaining_budget(factory, org) == Decimal("30000")
        print("  exact replay returns existing intent; conflicting new attempt rejected")


def test_unknown_then_sent(fixture):
    with isolated_fixture(fixture) as (factory, org, now):
        draft, plan = build_plan(factory, org, now, "unknown-sent")
        sender = MockOrderSender(outcome="unknown")
        with event_time(now):
            first = dispatch_order(factory, org, plan.submission_id, sender=sender, as_of=AS_OF)
        assert_error_code(first, "operation_outcome_unknown")
        assert sender.send_attempts == 1 and sender.lookup_calls == 0
        assert remaining_budget(factory, org) == Decimal("28700")  # Reserved, not refunded
        assert submitted_orders(factory, org) == []

        with event_time(now + timedelta(minutes=1)):
            second = dispatch_order(factory, org, plan.submission_id, sender=sender, as_of=AS_OF)
        assert_error_code(second, "operation_outcome_unknown")
        assert sender.send_attempts == 1 and sender.lookup_calls >= 1
        assert remaining_budget(factory, org) == Decimal("28700")

        sender.resolve(org, plan.submission_id, "sent")  # trusted later observation
        with event_time(now + timedelta(minutes=2)):
            third = require_ok(dispatch_order(factory, org, plan.submission_id,
                                              sender=sender, as_of=AS_OF), "reconcile sent")
        assert third.id == draft.id and third.status == "submitted"
        assert sender.send_attempts == 1
        assert len(submitted_orders(factory, org)) == 1
        assert remaining_budget(factory, org) == Decimal("28700")
        with event_time(now + timedelta(minutes=3)):
            fourth = require_ok(dispatch_order(factory, org, plan.submission_id,
                                               sender=sender, as_of=AS_OF), "repeat sent")
        assert fourth.id == draft.id and sender.send_attempts == 1
        print("  unknown -> lookup-only -> trusted sent: one send, one budget reservation")


def test_unknown_then_rejected(fixture):
    with isolated_fixture(fixture) as (factory, org, now):
        _, plan = build_plan(factory, org, now, "unknown-rejected")
        sender = MockOrderSender(outcome="unknown")
        with event_time(now):
            first = dispatch_order(factory, org, plan.submission_id, sender=sender, as_of=AS_OF)
        assert_error_code(first, "operation_outcome_unknown")
        assert sender.send_attempts == 1 and remaining_budget(factory, org) == Decimal("28700")

        sender.resolve(org, plan.submission_id, "rejected")
        with event_time(now + timedelta(minutes=1)):
            resolved = dispatch_order(factory, org, plan.submission_id, sender=sender, as_of=AS_OF)
        assert_error_code(resolved, "external_service_error")
        assert sender.send_attempts == 1
        assert remaining_budget(factory, org) == Decimal("30000")  # Released once
        assert submitted_orders(factory, org) == []

        with event_time(now + timedelta(minutes=2)):
            repeated = dispatch_order(factory, org, plan.submission_id, sender=sender, as_of=AS_OF)
        assert not repeated.ok and sender.send_attempts == 1
        assert remaining_budget(factory, org) == Decimal("30000")
        print("  unknown -> trusted rejected: no resend, reservation released once")


def test_terminal_failure(fixture):
    with isolated_fixture(fixture) as (factory, org, now):
        _, plan = build_plan(factory, org, now, "terminal-failure")
        sender = MockOrderSender(outcome="failed")
        with event_time(now):
            first = dispatch_order(factory, org, plan.submission_id, sender=sender, as_of=AS_OF)
        assert_error_code(first, "external_service_error")
        assert sender.send_attempts == 1
        assert remaining_budget(factory, org) == Decimal("30000")
        with event_time(now + timedelta(minutes=1)):
            second = dispatch_order(factory, org, plan.submission_id, sender=sender, as_of=AS_OF)
        assert not second.ok
        assert sender.send_attempts == 1
        assert remaining_budget(factory, org) == Decimal("30000")
        print("  terminal failed result cannot automatically retry or double-refund")


def test_manager_event_replay(fixture):
    scenario = copy.deepcopy(fixture)
    scenario["policy"]["mandatory_human_approval"] = True
    with isolated_fixture(scenario) as (factory, org, now):
        draft = draft_order(factory, org, now, tag="event-replay")
        with event_time(now), session_scope(factory) as session:
            approval = require_ok(request_approval(
                session, org, request_id="approval-idempotency-test", order_id=draft.id,
                reason="Idempotency test"), "request_approval")
        later = now + timedelta(minutes=1)
        event = ScenarioEvent(event_id="manager-event-idempotency", at=later,
                              kind="manager_decision", manager=ManagerDecision(
                                  approval_id=approval["id"], decision="approved", decided_by="test-manager"))
        mocks = MockServices()
        assert apply_event(factory, org, event, mocks) is False
        event_count = count_rows(factory, BusinessEvent, org)
        assert apply_event(factory, org, event, mocks) is True
        assert count_rows(factory, BusinessEvent, org) == event_count
        with session_scope(factory) as session:
            approvals = list(session.scalars(select(ApprovalRequest).where(
                ApprovalRequest.organization_id == org,
                ApprovalRequest.order_id == draft.id)))
        assert len(approvals) == 1
        assert approvals[0].status == "approved"
        conflict = event.model_copy(update={"manager": ManagerDecision(
            approval_id=approval["id"], decision="rejected", decided_by="test-manager")})
        try:
            apply_event(factory, org, conflict, mocks)
        except ValueError:
            pass
        else:
            raise AssertionError("Reused event_id with conflicting payload was accepted")
        assert count_rows(factory, BusinessEvent, org) == event_count
        assert remaining_budget(factory, org) == Decimal("30000")
        print("  exact manager replay deduplicated; conflicting event payload rejected")


def main():
    cases = json.loads(Path("eval_visible.json").read_text(encoding="utf-8-sig"))
    fixture = next(case["fixture"] for case in cases if case["id"] == "R-029")
    tests = [
        ("Replayed submission intent and conflicting retry", test_replayed_submission_intent),
        ("Unknown outcome reconciled as sent", test_unknown_then_sent),
        ("Unknown outcome reconciled as rejected", test_unknown_then_rejected),
        ("Terminal failure cannot resend", test_terminal_failure),
        ("Duplicate and conflicting manager event", test_manager_event_replay),
    ]
    failures = []
    for label, fn in tests:
        print(f"\n=== {label} ===", flush=True)
        try:
            fn(fixture)
            print("PASS", flush=True)
        except Exception as exc:
            failures.append(label)
            print(f"FAIL: {type(exc).__name__}: {exc}", flush=True)
    print(f"\nRESULT: {len(tests) - len(failures)}/{len(tests)} passed")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
