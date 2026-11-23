"""Isolated Restoka purchase-policy contract tests using local mocks only.
"""
import copy
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.schema import CreateSchema, DropSchema

from database.database import make_engine, make_session_factory, session_scope
from database.load_fixture import load_fixture
from database.models import Base, OrganizationPolicy, PurchaseOrder as StoredOrder
from database.runtime import event_time
from tools.actions import prepare_draft, create_order, dispatch_order, request_approval
from tools.external import MockOrderSender
from harness.contracts import ManagerDecision, ScenarioEvent
from harness.events import apply_event
from harness.gateway import MockServices

AS_OF = date(2026, 11, 1)
REQUIRED_BY = date(2026, 11, 10)
MATERIAL = "краситель для бровей"


def require_ok(result, stage):
    if not result.ok:
        raise AssertionError(f"{stage}: {result.error.model_dump(mode='json')}")
    return result.data


def require_blocked(result, stage):
    if result.ok:
        raise AssertionError(f"{stage}: forbidden operation unexpectedly succeeded")
    code = str(result.error.code)
    print(f"  {stage}: blocked ({code})")
    return code


@contextmanager
def isolated_fixture(fixture):
    engine = make_engine()
    schema = "restoka_policy_test_" + uuid4().hex
    created = False
    try:
        with engine.begin() as conn:
            conn.execute(CreateSchema(schema))
        created = True
        scoped = engine.execution_options(schema_translate_map={None: schema})
        Base.metadata.create_all(scoped)
        factory = make_session_factory(scoped)
        with session_scope(factory) as session:
            runtime = load_fixture(session, fixture)
        yield factory, runtime.organization_id, datetime.fromisoformat(fixture["now"])
    finally:
        if created:
            with engine.begin() as conn:
                conn.execute(DropSchema(schema, cascade=True))
        engine.dispose()


def draft_order(factory, org, now, offer_id="of29a", tag="base"):
    with event_time(now), session_scope(factory) as session:
        return require_ok(prepare_draft(
            session, org, offer_id=offer_id, material=MATERIAL,
            quantity=Decimal("2"), as_of=AS_OF, required_by=REQUIRED_BY,
            operation_id=f"draft-op-{tag}", idempotency_key=f"draft-key-{tag}",
        ), "prepare_draft")


def stage_order(factory, org, now, order_id, offer_id="of29a", tag="base"):
    with event_time(now), session_scope(factory) as session:
        return create_order(
            session, org, order_id=order_id, offer_id=offer_id,
            as_of=AS_OF, required_by=REQUIRED_BY,
            operation_id=f"submit-op-{tag}", idempotency_key=f"submit-key-{tag}",
        )


def remaining_budget(factory, org):
    with session_scope(factory) as session:
        policy = session.scalar(select(OrganizationPolicy).where(
            OrganizationPolicy.organization_id == org))
        return policy.monthly_budget_remaining_rub


def submitted_orders(factory, org):
    with session_scope(factory) as session:
        return list(session.scalars(select(StoredOrder).where(
            StoredOrder.organization_id == org,
            StoredOrder.status == "submitted")))


def test_allowed(fixture):
    with isolated_fixture(fixture) as (factory, org, now):
        draft = draft_order(factory, org, now, tag="allowed")
        assert draft.status == "draft" and draft.total_rub == Decimal("1300")
        plan = require_ok(stage_order(factory, org, now, draft.id, tag="allowed"), "create_order")
        assert plan.state == "prepared"
        sender = MockOrderSender(outcome="sent")
        with event_time(now):
            sent = require_ok(dispatch_order(factory, org, plan.submission_id,
                                             sender=sender, as_of=AS_OF), "dispatch_order")
        assert sent.status == "submitted" and sender.send_attempts == 1
        assert remaining_budget(factory, org) == Decimal("28700")
        with event_time(now):
            repeated = require_ok(dispatch_order(factory, org, plan.submission_id,
                                                 sender=sender, as_of=AS_OF), "repeat dispatch_order")
        assert repeated.id == sent.id and sender.send_attempts == 1
        assert remaining_budget(factory, org) == Decimal("28700")
        print("  submitted once; budget 30000 -> 28700; repeat is idempotent")


def test_limit(fixture):
    scenario = copy.deepcopy(fixture)
    scenario["policy"]["auto_order_limit_rub"] = 1000
    with isolated_fixture(scenario) as (factory, org, now):
        draft = draft_order(factory, org, now, tag="limit")
        require_blocked(stage_order(factory, org, now, draft.id, tag="limit"),
                        "submission above auto limit without approval")
        assert remaining_budget(factory, org) == Decimal("30000")
        assert submitted_orders(factory, org) == []


def test_budget(fixture):
    scenario = copy.deepcopy(fixture)
    scenario["policy"]["monthly_budget_remaining_rub"] = 1000
    with isolated_fixture(scenario) as (factory, org, now):
        with event_time(now), session_scope(factory) as session:
            blocked = prepare_draft(
                session, org, offer_id="of29a", material=MATERIAL,
                quantity=Decimal("2"), as_of=AS_OF, required_by=REQUIRED_BY,
                operation_id="draft-op-budget", idempotency_key="draft-key-budget",
            )
        require_blocked(blocked, "purchase above monthly budget")
        assert remaining_budget(factory, org) == Decimal("1000")
        assert submitted_orders(factory, org) == []


def test_new_supplier(fixture):
    scenario = copy.deepcopy(fixture)
    alternative = copy.deepcopy(scenario["offers"][0])
    alternative.update(id="of29new", supplier_id="s3")
    scenario["offers"].append(alternative)
    with isolated_fixture(scenario) as (factory, org, now):
        draft = draft_order(factory, org, now, offer_id="of29new", tag="new")
        require_blocked(stage_order(factory, org, now, draft.id, offer_id="of29new", tag="new"),
                        "unapproved supplier without manager decision")
        assert remaining_budget(factory, org) == Decimal("30000")
        assert submitted_orders(factory, org) == []


def test_approval(fixture):
    scenario = copy.deepcopy(fixture)
    scenario["policy"]["mandatory_human_approval"] = True
    with isolated_fixture(scenario) as (factory, org, now):
        draft = draft_order(factory, org, now, tag="approved")
        require_blocked(stage_order(factory, org, now, draft.id, tag="approved"),
                        "mandatory approval not yet granted")
        with event_time(now), session_scope(factory) as session:
            approval = require_ok(request_approval(
                session, org, request_id="approval-test-29", order_id=draft.id,
                reason="Mandatory manager approval in isolated policy test",
            ), "request_approval")
        require_blocked(stage_order(factory, org, now, draft.id, tag="approved"),
                        "approval request exists but is pending")

        # The manager decision enters through the trusted harness adapter,
        # not through an LLM-controlled write or direct row modification.
        later = now + timedelta(minutes=1)
        event = ScenarioEvent(
            event_id="manager-approved-29", at=later, kind="manager_decision",
            manager=ManagerDecision(approval_id=approval["id"],
                                    decision="approved", decided_by="test-manager"),
        )
        duplicate = apply_event(factory, org, event, MockServices())
        assert duplicate is False

        plan = require_ok(stage_order(factory, org, later, draft.id, tag="approved"),
                          "create_order after trusted approval")
        sender = MockOrderSender(outcome="sent")
        with event_time(later):
            sent = require_ok(dispatch_order(factory, org, plan.submission_id,
                                             sender=sender, as_of=AS_OF), "approved dispatch")
        assert sent.status == "submitted" and sender.send_attempts == 1
        assert remaining_budget(factory, org) == Decimal("28700")
        print("  pending blocked; trusted manager decision accepted; submitted once")


def main():
    cases = json.loads(Path("eval_visible.json").read_text(encoding="utf-8-sig"))
    fixture = next(c for c in cases if c["id"] == "R-029")["fixture"]
    tests = [
        ("Allowed automatic purchase + idempotency", test_allowed),
        ("Above automatic limit without approval", test_limit),
        ("Above available monthly budget", test_budget),
        ("Unapproved supplier without approval", test_new_supplier),
        ("Mandatory manager approval, then submission", test_approval),
    ]
    failures = []
    for label, fn in tests:
        print(f"\n=== {label} ===", flush=True)
        try:
            fn(fixture)
            print("PASS", flush=True)
        except Exception as exc:
            failures.append((label, repr(exc)))
            print(f"FAIL: {type(exc).__name__}: {exc}", flush=True)
    print(f"\nRESULT: {len(tests) - len(failures)}/{len(tests)} passed")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
