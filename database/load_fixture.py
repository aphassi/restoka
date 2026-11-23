"""Load initial eval scenario data into PostgreSQL."""

import argparse
import copy
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from database.database import make_engine, make_session_factory, session_scope
from database.models import (
    Appointment, ApprovalRequest, BusinessEvent, ConsumptionRule, Inventory,
    Organization, OrganizationPolicy, PurchaseOrder, Supplier, SupplierOffer, Conversation, Message,
)


@dataclass(frozen=True)
class FixtureRuntime:
    """Scenario execution context, stored separately from business data."""

    organization_id: str
    now: datetime
    timezone: str
    tool_responses: dict[str, Any]
    proposed_order: dict[str, Any] | None
    procurement_case_ref: str | None
    supplier_message: str | None


def decimal_value(value: int | float | str | Decimal) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid quantity or price")

    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Quantity or price must be finite")

    return result


def _optional(value, convert):
    return None if value is None else convert(value)


def load_fixture(session: Session, fixture: dict[str, Any], *, replace: bool = False) -> FixtureRuntime:
    """Load one scenario's initial state into PostgreSQL.

    replace=True resets existing data for the organization.
    Call once per scenario, not between its steps."""

    org_id = fixture["organization_id"]
    now = datetime.fromisoformat(fixture["now"])

    if now.tzinfo is None:
        raise ValueError("fixture.now must include a timezone offset")

    timezone = fixture["timezone"]
    ZoneInfo(timezone)

    suppliers = fixture.get("suppliers", [])
    policy = fixture.get("policy")

    for supplier in suppliers:
        if type(supplier.get("approved")) is not bool:
            raise ValueError(f"Supplier {supplier.get('id')!r}: approved must be boolean")
    
    if policy is not None and "approved_supplier_ids" in policy:
        listed = policy["approved_supplier_ids"]
        if not isinstance(listed, list) or any(not isinstance(x, str) for x in listed):
            raise ValueError("policy.approved_supplier_ids must be a list of IDs")
    
        approved = {s["id"] for s in suppliers if s["approved"]}
        if set(listed) != approved:
            raise ValueError(f"Conflicting approved suppliers: policy={sorted(set(listed))}, suppliers={sorted(approved)}")

    with session.begin_nested():
        existing = session.get(Organization, org_id)
        if existing is not None and not replace:
            raise ValueError(f"Organization {org_id!r} already exists; use a separate test database or replace=True to reset")

        if existing is not None:
            for model in (Message, Conversation, ApprovalRequest, BusinessEvent, SupplierOffer, PurchaseOrder,
                          Inventory, ConsumptionRule, Appointment, OrganizationPolicy, Supplier):
                session.execute(delete(model).where(model.organization_id == org_id))
            existing.timezone = timezone
        else:
            session.add(Organization(id=org_id, timezone=timezone))
    
        session.flush()

        for item in fixture.get("appointments", []):
            session.add(Appointment(
                organization_id=org_id, id=item["id"], service=item["service"],
                date=date.fromisoformat(item["date"]), count=item["count"],
                external_id=item.get("external_id"), status=item.get("status"),
                starts_at=_optional(item.get("starts_at"), datetime.fromisoformat),
                source_updated_at=_optional(item.get("source_updated_at"), datetime.fromisoformat),
            ))
    
        for item in fixture.get("consumption_rules", []):
            session.add(ConsumptionRule(
                organization_id=org_id, material=item["material"], service=item.get("service"),
                units_per_appointment=decimal_value(item["units_per_appointment"]),
            ))
    
        for item in fixture.get("inventory", []):
            session.add(Inventory(
                organization_id=org_id, material=item["material"],
                quantity=decimal_value(item["quantity"]), as_of=date.fromisoformat(item["as_of"]),
            ))
    
        for item in suppliers:
            session.add(Supplier(organization_id=org_id, id=item["id"], approved=item["approved"]))
        
        session.flush()
        for item in fixture.get("offers", []):
            session.add(SupplierOffer(
                organization_id=org_id, id=item["id"], supplier_id=item["supplier_id"],
                material=item["material"], quantity=decimal_value(item["quantity"]),
                unit_price_rub=_optional(item.get("unit_price_rub"), decimal_value),
                delivery_fee_rub=_optional(item.get("delivery_fee_rub"), decimal_value),
                delivery_date=_optional(item.get("delivery_date"), date.fromisoformat),
                confirmed=item.get("confirmed", False),
                confirmed_quantity=_optional(item.get("confirmed_quantity"), decimal_value),
            ))
    
        if policy is not None:
            session.add(OrganizationPolicy(
                organization_id=org_id,
                auto_order_limit_rub=_optional(policy.get("auto_order_limit_rub"), decimal_value),
                monthly_budget_remaining_rub=_optional(policy.get("monthly_budget_remaining_rub"), decimal_value),
                approval_required_for_new_supplier=policy.get("approval_required_for_new_supplier"),
                automatic_ordering_enabled=policy.get("automatic_ordering_enabled"),
                # An omitted extra approval rule is disabled; explicit null stays unknown.
                mandatory_human_approval=policy.get("mandatory_human_approval", False),
            ))

        # Both arrays describe orders. Merge complementary records by ID, but
        # reject contradictory observations rather than silently picking one.
        orders = {}
        for item in [*fixture.get("pending_orders", []), *fixture.get("orders", [])]:
            merged = orders.setdefault(item["id"], {})
            for key, value in item.items():
                if value is not None and key in merged and merged[key] is not None and merged[key] != value:
                    raise ValueError(f"Conflicting order {item['id']!r}, field {key!r}")
                if value is not None or key not in merged:
                    merged[key] = value
        
        for item in orders.values():
            session.add(PurchaseOrder(
                organization_id=org_id, id=item["id"], status=item["status"],
                supplier_id=item.get("supplier_id"), material=item.get("material"),
                operation_id=item.get("operation_id"), idempotency_key=item.get("idempotency_key"),
                quantity=_optional(item.get("quantity"), decimal_value),
                total_rub=_optional(item.get("total_rub"), decimal_value),
                eta=_optional(item.get("eta"), date.fromisoformat),
            ))
    
        for item in fixture.get("action_log", []):
            session.add(BusinessEvent(
                organization_id=org_id, id=item["id"], type=item["type"],
                date=_optional(item.get("date"), date.fromisoformat),
                details=copy.deepcopy({k: v for k, v in item.items() if k not in {"id", "type", "date"}}),
            ))
        
        session.flush()
    
    return FixtureRuntime(
        organization_id=org_id, now=now, timezone=timezone,
        tool_responses=copy.deepcopy(fixture.get("tool_responses", {})),
        proposed_order=copy.deepcopy(fixture.get("proposed_order")),
        procurement_case_ref=fixture.get("procurement_case_ref"),
        supplier_message=fixture.get("supplier_message"),
    )


def read_visible_case(path: str | Path, case_id: str) -> dict[str, Any]:
    """Read a visible eval case by ID."""

    cases = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(cases, list):
        raise ValueError("Expected a JSON array of eval cases")

    found = [case for case in cases if case.get("id") == case_id]
    if len(found) != 1:
        raise ValueError(f"Expected exactly one case with id={case_id!r}, got {len(found)}")

    case = found[0]
    if case.get("split") != "visible":
        raise ValueError("Only visible cases may be loaded by this development helper")

    return case


def main() -> None:
    parser = argparse.ArgumentParser(description="Load one visible Restoka fixture")

    parser.add_argument("--case-id", required=True, help="For example R-001")
    parser.add_argument(
        "--eval-file", default="eval_visible.json", type=Path,
        help="Path to your existing eval_visible.json (not copied into the package)",
    )
    parser.add_argument(
        "--replace", action="store_true",
        help="Reset this organization's rows ONLY in your dedicated test database",
    )
    parser.add_argument("--database-url", default=None)

    args = parser.parse_args()

    case = read_visible_case(args.eval_file, args.case_id)
    engine = make_engine(args.database_url)
    try:
        # Restrict data resets to databases with test-specific names.
        database_name = engine.url.database or ""
        if args.replace and engine.dialect.name == "postgresql" and not any(
            token in database_name.lower() for token in ("_eval", "_test")
        ):
            raise ValueError(
                "--replace is permitted only for a PostgreSQL database named "
                "with _eval or _test (for example restoka_eval)."
            )
    
        factory = make_session_factory(engine)
        with session_scope(factory) as session:
            runtime = load_fixture(session, case["fixture"], replace=args.replace)
            counts = {
                "appointments": session.scalar(
                    select(func.count()).select_from(Appointment).where(
                        Appointment.organization_id == runtime.organization_id
                    )
                ),
                "inventory": session.scalar(
                    select(func.count()).select_from(Inventory).where(
                        Inventory.organization_id == runtime.organization_id
                    )
                ),
                "offers": session.scalar(
                    select(func.count()).select_from(SupplierOffer).where(
                        SupplierOffer.organization_id == runtime.organization_id
                    )
                ),
            }
        
        print(
            f"Loaded {args.case_id} for {runtime.organization_id}: "
            + ", ".join(f"{name}={count}" for name, count in counts.items())
        )
    
        if runtime.tool_responses:
            print("Note: tool_responses belong to the runner's failure-injection adapter, not to SQL tables.")
        
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
