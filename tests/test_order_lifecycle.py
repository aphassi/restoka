"""Restoka KT3: isolated order lifecycle and multi-event integration checks.
"""
import copy
import json
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from database.database import session_scope
from database.models import ApprovalRequest, PurchaseOrder as StoredOrder, SupplierOffer as StoredOffer
from database.runtime import event_time
from harness.contracts import ManagerDecision, RunConfig, RunResult, ScenarioEvent
from harness.events import apply_event
from harness.gateway import MockServices
from harness.scenario import ScenarioRunner
from tools.actions import create_order, dispatch_order, request_approval
from tools.external import MockOrderSender

from tests.test_purchase_policy import (
    AS_OF, REQUIRED_BY, isolated_fixture, draft_order, stage_order,
    remaining_budget, require_ok, require_blocked, submitted_orders,
)


def read_order(factory, org, order_id):
    with session_scope(factory) as session:
        row = session.scalar(select(StoredOrder).where(
            StoredOrder.organization_id == org, StoredOrder.id == order_id))
        assert row is not None
        return row.status


def read_approval(factory, org, approval_id):
    with session_scope(factory) as session:
        row = session.scalar(select(ApprovalRequest).where(
            ApprovalRequest.organization_id == org, ApprovalRequest.id == approval_id))
        assert row is not None
        return row.status


def ask_approval(factory, org, now, order_id, tag):
    with event_time(now), session_scope(factory) as session:
        return require_ok(request_approval(
            session, org, request_id=f"lifecycle-approval-{tag}",
            order_id=order_id, reason="Lifecycle integration test",
        ), "request_approval")


def manager_event(factory, org, when, approval_id, decision, tag):
    event = ScenarioEvent(
        event_id=f"lifecycle-manager-{tag}", at=when, kind="manager_decision",
        manager=ManagerDecision(
            approval_id=approval_id, decision=decision, decided_by="test-manager"),
    )
    assert apply_event(factory, org, event, MockServices()) is False


def test_status_transitions(fixture):
    config = copy.deepcopy(fixture)
    config["policy"]["mandatory_human_approval"] = True
    with isolated_fixture(config) as (factory, org, now):
        draft = draft_order(factory, org, now, tag="transitions")
        assert read_order(factory, org, draft.id) == "draft"
        approval = ask_approval(factory, org, now, draft.id, "transitions")
        assert read_order(factory, org, draft.id) == "pending_approval"
        assert read_approval(factory, org, approval["id"]) == "pending"
        require_blocked(stage_order(factory, org, now, draft.id, tag="transitions"),
                        "submission while approval is pending")
        approved_at = now + timedelta(minutes=1)
        manager_event(factory, org, approved_at, approval["id"], "approved", "transitions")
        assert read_approval(factory, org, approval["id"]) == "approved"
        plan = require_ok(stage_order(factory, org, approved_at, draft.id, tag="transitions"),
                          "create_order after approval")
        assert plan.state == "prepared"
        sender = MockOrderSender(outcome="sent")
        with event_time(approved_at):
            order = require_ok(dispatch_order(factory, org, plan.submission_id,
                                              sender=sender, as_of=AS_OF), "dispatch_order")
        assert order.status == "submitted" and sender.send_attempts == 1
        assert read_order(factory, org, draft.id) == "submitted"
        assert remaining_budget(factory, org) == Decimal("28700")
        print("  draft -> pending_approval -> approved -> submitted; budget reserved once")


def test_manager_rejection(fixture):
    config = copy.deepcopy(fixture)
    config["policy"]["mandatory_human_approval"] = True
    with isolated_fixture(config) as (factory, org, now):
        draft = draft_order(factory, org, now, tag="rejected")
        approval = ask_approval(factory, org, now, draft.id, "rejected")
        later = now + timedelta(minutes=1)
        manager_event(factory, org, later, approval["id"], "rejected", "rejected")
        assert read_approval(factory, org, approval["id"]) == "rejected"
        result = stage_order(factory, org, later, draft.id, tag="rejected")
        error = require_blocked(result, "submission after manager rejection")
        assert "business_rule_violation" in error.lower(), error
        assert submitted_orders(factory, org) == []
        assert remaining_budget(factory, org) == Decimal("30000")
        print("  manager rejection persisted; no submission or budget reservation")


def test_stale_approval_after_offer_change(fixture):
    config = copy.deepcopy(fixture)
    config["policy"]["mandatory_human_approval"] = True
    with isolated_fixture(config) as (factory, org, now):
        draft = draft_order(factory, org, now, tag="stale-terms")
        approval = ask_approval(factory, org, now, draft.id, "stale-terms")
        later = now + timedelta(minutes=1)
        manager_event(factory, org, later, approval["id"], "approved", "stale-terms")
        # Trusted test-fixture mutation simulates an externally confirmed offer revision.
        # It is not an LLM/tool write; the safety property under test is that old
        # approval/draft terms cannot authorize a later submission after revision.
        with session_scope(factory) as session:
            offer = session.scalar(select(StoredOffer).where(
                StoredOffer.organization_id == org, StoredOffer.id == "of29a"))
            assert offer is not None
            offer.unit_price_rub = Decimal("550")
        result = stage_order(factory, org, later, draft.id, tag="stale-terms")
        error = require_blocked(result, "submission after offer price changed")
        assert "business_rule_violation" in error.lower(), error
        assert read_order(factory, org, draft.id) == "pending_approval"
        assert submitted_orders(factory, org) == []
        assert remaining_budget(factory, org) == Decimal("30000")
        print("  approved old terms cannot submit after supplier price revision")


class WaitingThenApprovedRunner:
    """Purpose-built harness runner for verifying state across two real events."""

    def __init__(self):
        self.order_id = None

    @staticmethod
    def tool(context, name, arguments):
        response = context.tools.call(name, arguments)
        if not response.ok:
            raise AssertionError(f"{name}: {response.error.model_dump(mode='json')}")
        return response.data

    def run(self, request, context):
        if request["event_id"] == "lifecycle-open":
            draft = self.tool(context, "prepare_draft", {
                "offer_id": "of29a", "material": "краситель для бровей",
                "quantity": "2", "as_of": AS_OF.isoformat(),
                "required_by": REQUIRED_BY.isoformat(),
                "operation_id": "harness-draft-op", "idempotency_key": "harness-draft-key",
            })
            self.order_id = draft.id
            approval = self.tool(context, "request_approval", {
                "request_id": "harness-approval-id", "order_id": draft.id,
                "reason": "Wait for trusted manager decision",
            })
            assert approval["id"] == "harness-approval-id"
            return RunResult(action="escalate", process_status="waiting",
                             text="Awaiting manager decision",
                             facts={"order_id": self.order_id})

        if request["event_id"] == "lifecycle-approve":
            assert request["kind"] == "manager_decision"
            assert self.order_id is not None
            plan = self.tool(context, "create_order", {
                "order_id": self.order_id, "offer_id": "of29a",
                "as_of": AS_OF.isoformat(), "required_by": REQUIRED_BY.isoformat(),
                "operation_id": "harness-submit-op", "idempotency_key": "harness-submit-key",
            })
            sent = self.tool(context, "dispatch_order", {
                "submission_id": plan.submission_id, "as_of": AS_OF.isoformat(),
            })
            assert sent.id == self.order_id and sent.status == "submitted"
            return RunResult(action="act", process_status="completed", text="Submitted",
                             facts={"order_id": sent.id, "status": sent.status})
        raise AssertionError("Unexpected lifecycle event")


def test_harness_multistep_wait_resume(fixture):
    config_fixture = copy.deepcopy(fixture)
    config_fixture["policy"]["mandatory_human_approval"] = True
    now = datetime.fromisoformat(fixture["now"])
    later = now + timedelta(minutes=1)
    scenario = {
        "scenario_id": "KT3-LIFECYCLE-2STEP",
        "fixture": config_fixture,
        "events": [
            {"event_id": "lifecycle-open", "at": now.isoformat(),
             "kind": "user_message", "input": {"text": "Prepare a draft and request approval"},
             "conversation_id": "lifecycle-conversation", "user_id": "test-manager-user"},
            {"event_id": "lifecycle-approve", "at": later.isoformat(),
             "kind": "manager_decision", "input": {},
             "manager": {"approval_id": "harness-approval-id",
                         "decision": "approved", "decided_by": "test-manager"},
             "conversation_id": "lifecycle-conversation", "user_id": "test-manager-user"},
        ],
    }
    runner = WaitingThenApprovedRunner()
    with tempfile.TemporaryDirectory(prefix="restoka_lifecycle_") as output:
        record = ScenarioRunner().run(scenario, RunConfig(
            eval_version="kt3-lifecycle-tests", runner_name="lifecycle-test",
            output_dir=Path(output), capture_response_text=True,
        ), runner)
        steps = record["step_results"]
        assert len(steps) == 2, f"Expected 2 events, got {len(steps)}"
        assert not record["technical_errors"], record["technical_errors"]
        assert not record["tool_errors"], record["tool_errors"]
        assert [s["result"]["process_status"] for s in steps] == ["waiting", "completed"]
        assert [s["result"]["action"] for s in steps] == ["escalate", "act"]
        assert steps[0]["result"]["facts"]["order_id"] == steps[1]["result"]["facts"]["order_id"]
        assert record["scenario_status"] == "completed"
        assert len(record["changes"]["orders_created"]) == 1, record["changes"]
        assert steps[1]["result"]["facts"]["status"] == "submitted"
        snapshots = []
        for idx, step in enumerate(steps):
            path = Path(step["snapshot_path"])
            assert path.exists(), f"Missing state snapshot {idx}"
            snapshots.append(json.loads(path.read_text(encoding="utf-8")))
        first_orders = snapshots[0]["purchase_orders"]["rows"]
        last_orders = snapshots[1]["purchase_orders"]["rows"]
        assert len(first_orders) == len(last_orders) == 1
        assert first_orders[0]["status"] == "pending_approval"
        assert last_orders[0]["status"] == "submitted"
        first_approvals = snapshots[0]["approval_requests"]["rows"]
        last_approvals = snapshots[1]["approval_requests"]["rows"]
        assert len(first_approvals) == len(last_approvals) == 1
        assert first_approvals[0]["status"] == "pending"
        assert last_approvals[0]["status"] == "approved"
        print("  ScenarioRunner: 2 events; waiting -> completed; snapshots prove persisted order/approval")


def test_submitted_order_cannot_resubmit(fixture):
    with isolated_fixture(fixture) as (factory, org, now):
        draft = draft_order(factory, org, now, tag="finished")
        plan = require_ok(stage_order(factory, org, now, draft.id, tag="finished"),
                          "initial create_order")
        sender = MockOrderSender(outcome="sent")
        with event_time(now):
            sent = require_ok(dispatch_order(factory, org, plan.submission_id,
                                             sender=sender, as_of=AS_OF), "initial dispatch")
        assert sent.status == "submitted"
        blocked = stage_order(factory, org, now, draft.id, tag="finished-new-attempt")
        code = require_blocked(blocked, "fresh submission attempt for submitted order")
        assert "duplicate_operation" in code.lower() or "business_rule_violation" in code.lower()
        assert sender.send_attempts == 1
        assert remaining_budget(factory, org) == Decimal("28700")
        print("  submitted order cannot be submitted through a new operation")


def main():
    cases = json.loads(Path("eval_visible.json").read_text(encoding="utf-8-sig"))
    fixture = next(case["fixture"] for case in cases if case["id"] == "R-029")
    tests = [
        ("Order status transitions with approval", test_status_transitions),
        ("Manager rejection blocks submission", test_manager_rejection),
        ("Offer changes invalidate prior approval", test_stale_approval_after_offer_change),
        ("ScenarioRunner persists state across waiting and approval", test_harness_multistep_wait_resume),
        ("Submitted order cannot be freshly submitted", test_submitted_order_cannot_resubmit),
    ]
    failures = []
    for label, test in tests:
        print(f"\n=== {label} ===", flush=True)
        try:
            test(fixture)
            print("PASS", flush=True)
        except Exception as err:
            failures.append(label)
            print(f"FAIL: {type(err).__name__}: {err}", flush=True)
    print(f"\nRESULT: {len(tests) - len(failures)}/{len(tests)} passed")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
