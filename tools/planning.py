"""Calculate material deficits and compare supplier offers using stored facts.
Planning reads data without changing orders, approvals or inventory.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal, localcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from sqlalchemy.orm import Session
from typing_extensions import TypedDict

from schemas import Quantity, PurchaseOrder
from . import queries
from .contracts import ToolError, ToolErrorCode, ToolResult

__all__ = ["calc_material_need", "compare_suppliers"]


class ConsumptionSource(TypedDict):
    appointment_id: str
    rule_id: int
    service: str
    date: date
    count: int
    units_per_appointment: Decimal
    quantity: Decimal


class DailyNeed(TypedDict):
    date: date
    cumulative_need: Decimal
    cumulative_incoming: Decimal
    shortage: Decimal


class OrderReview(TypedDict):
    order: PurchaseOrder
    reason: str


class MaterialNeed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material: str
    required_quantity: Decimal
    prior_required_quantity: Decimal
    inventory: queries.InventoryRow
    incoming_quantity: Decimal
    incoming_orders: list[PurchaseOrder]
    deficit: Decimal
    first_shortage_date: date | None
    consumption: list[ConsumptionSource]
    timeline: list[DailyNeed]
    orders_to_review: list[OrderReview]


class MaterialNeedResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: str
    date_from: date
    date_to: date
    as_of: date
    orders_to_review: list[OrderReview]
    materials: list[MaterialNeed]
    excluded_appointment_ids: list[str]
    requires_order_review: bool


class ComparedOffer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offer: queries.SupplierOfferRow
    confirmed_available: Decimal | None
    total_rub: Decimal | None
    suitability: Literal["eligible", "requires_approval", "incomplete", "rejected"]
    reasons: list[str]
    approval_required: bool | None
    approval_reasons: list[str]
    missing_policy_fields: list[str]
    commercial_suitability: Literal["eligible", "incomplete", "rejected"]
    automatic_ordering_check: Literal["not_blocked", "blocked", "unknown"]
    automatic_ordering_reasons: list[str]


class SupplierComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: str
    material: str
    quantity: Decimal
    as_of: date
    required_by: date
    policy: queries.PolicyRow
    offers: list[ComparedOffer]
    outcome: Literal["offers_found", "incomplete", "no_suitable_offer"]
    recommended_offer_id: str | None


def _error(code: ToolErrorCode, message: str) -> ToolResult:
    return ToolResult(ok=False, error=ToolError(
        code=code, message=message, operation_outcome="not_started",
    ))


def _valid_period(start: date, end: date) -> bool:
    return type(start) is date and type(end) is date and start <= end


def _valid_material(material: str | None) -> bool:
    return material is None or isinstance(material, str) and bool(material.strip())


def calc_material_need(
    session: Session, organization_id: str, *, date_from: date, date_to: date,
    material: str | None = None, as_of: date | None = None,
) -> ToolResult[MaterialNeedResult]:
    """Calculate dated deficits from a stock snapshot on date_from through date_to.

    Specific service rules override general rules. Only confirmed, dated incoming
    orders count as supply; other existing orders require review before buying.
    """
    if not _valid_period(date_from, date_to) or not _valid_material(material):
        return _error(ToolErrorCode.INVALID_ARGUMENTS, "Provide an ordered date range and a nonblank material")
    
    as_of = date_from if as_of is None else as_of
    if not _valid_period(as_of, date_from):
        return _error(ToolErrorCode.INVALID_ARGUMENTS, "as_of must be a date no later than date_from")

    # Forecast consumption before the requested period to avoid spending the
    # same observed stock twice when planning a later window.
    appointments = queries.get_appointments(
        session, organization_id, date_from=as_of, date_to=date_to,
    )
    if not appointments.ok:
        return ToolResult(ok=False, error=appointments.error)
    
    active, excluded = [], []
    for appointment in appointments.data:
        status = appointment["status"]
        if status in ("cancelled", "canceled", "completed"):
            excluded.append(appointment["id"])
        elif status in (None, "scheduled", "confirmed"):
            active.append(appointment)
        else:
            return _error(ToolErrorCode.MISSING_DATA, f"Unknown appointment status for {appointment['id']}")
        
    orders = queries.get_pending_orders(
        session, organization_id, statuses=("draft", "pending_approval", "submitted", "confirmed"),
    )
    if not orders.ok:
        return ToolResult(ok=False, error=orders.error)

    review_orders = [OrderReview(order=order, reason="order_" + order.status)
                     for order in orders.data if order.status != "confirmed"
                     and (order.material is None or order.material == material)]

    if not active:
        return ToolResult[MaterialNeedResult](ok=True, data=MaterialNeedResult(
            organization_id=organization_id, date_from=date_from, date_to=date_to, as_of=as_of,
            materials=[], excluded_appointment_ids=excluded,
            orders_to_review=review_orders, requires_order_review=bool(review_orders),
        ))

    # Read all rules to check service coverage, even when filtering by material.
    rules = queries.get_consumption_rules(session, organization_id)
    if not rules.ok:
        return ToolResult(ok=False, error=rules.error)

    for appointment in active:
        if not any(rule["service"] in (None, appointment["service"]) for rule in rules.data):
            return _error(ToolErrorCode.MISSING_DATA,
                          f"No consumption rules for service {appointment['service']}")
    
    if any(order.status == "confirmed" and order.material is None for order in orders.data):
        return _error(ToolErrorCode.MISSING_DATA, "A confirmed order has no material; reconcile it before planning")

    rules_by_material = defaultdict(dict)
    for rule in rules.data:
        if (material is None or rule["material"] == material) and any(
            rule["service"] in (None, appointment["service"]) for appointment in active
        ):
            rules_by_material[rule["material"]][rule["service"]] = rule
    
    review_orders = [OrderReview(order=order, reason="order_" + order.status)
                     for order in orders.data if order.status != "confirmed"
                     and (order.material is None or order.material in rules_by_material)]

    inventory = (queries.get_inventory(session, organization_id, material=material)
                 if rules_by_material else ToolResult(ok=True, data=[]))
    if not inventory.ok:
        return ToolResult(ok=False, error=inventory.error)

    stocks = {item["material"]: item for item in inventory.data}
    results = []
    with localcontext() as context:
        context.prec = 64
        for name, service_rules in sorted(rules_by_material.items()):
            stock = stocks.get(name)
            if stock is None or stock["as_of"] != as_of:
                return _error(ToolErrorCode.MISSING_DATA, f"Inventory for {name} must be observed on as_of")
            
            consumption, by_day = [], defaultdict(Decimal)
            for appointment in active:
                rule = service_rules.get(appointment["service"], service_rules.get(None))
                if rule is None:
                    continue
                
                quantity = rule["units_per_appointment"] * appointment["count"]
                by_day[appointment["date"]] += quantity
            
                consumption.append(ConsumptionSource(
                    appointment_id=appointment["id"], rule_id=rule["id"],
                    service=appointment["service"], date=appointment["date"], count=appointment["count"],
                    units_per_appointment=rule["units_per_appointment"], quantity=quantity,
                ))
            
            incoming, review = [], []
            last_use = max(by_day)
            for order in orders.data:
                if order.material != name:
                    continue
                if order.status != "confirmed":
                    review.append(OrderReview(order=order, reason="order_" + order.status))
                    continue
                if order.quantity is None or order.eta is None:
                    return _error(ToolErrorCode.MISSING_DATA,
                                  f"Existing order {order.id} lacks quantity or delivery date")
            
                if order.eta < as_of:
                    reason = "overdue_delivery_requires_verification"
                elif order.eta > last_use:
                    reason = "delivery_after_last_use"
                else:
                    incoming.append(order)
                    continue
            
                review.append(OrderReview(order=order, reason=reason))

            # Max cumulative shortfall preserves an early deficit even when a
            # later delivery would make the end-of-period balance nonnegative.
            total_need = Decimal(0)
            prior_need = Decimal(0)
            deficit = Decimal(0)
            first_shortage = None
            timeline = []
            for day, quantity in sorted(by_day.items()):
                total_need += quantity
                arrived = sum((order.quantity for order in incoming if order.eta <= day), Decimal(0))
                shortage = max(Decimal(0), total_need - stock["quantity"] - arrived)
    
                if day < date_from:
                    prior_need += quantity
                else:
                    deficit = max(deficit, shortage)
                    if shortage > 0 and first_shortage is None:
                        first_shortage = day
                
                timeline.append(DailyNeed(date=day, cumulative_need=total_need,
                                          cumulative_incoming=arrived, shortage=shortage))
            
            if first_shortage is not None:
                review.extend(OrderReview(order=order, reason="delivery_after_first_shortage")
                              for order in incoming if order.eta > first_shortage)
            
            results.append(MaterialNeed(
                material=name, required_quantity=total_need - prior_need,
                prior_required_quantity=prior_need, inventory=stock,
                incoming_quantity=sum((order.quantity for order in incoming), Decimal(0)),
                incoming_orders=incoming, deficit=deficit, first_shortage_date=first_shortage,
                consumption=consumption, timeline=timeline, orders_to_review=review,
            ))
    
    seen_reviews = {item["order"].id for item in review_orders}
    for item in results:
        for review in item.orders_to_review:
            if review["order"].id not in seen_reviews:
                review_orders.append(review)
                seen_reviews.add(review["order"].id)
    return ToolResult[MaterialNeedResult](ok=True, data=MaterialNeedResult(
        organization_id=organization_id, date_from=date_from, date_to=date_to, as_of=as_of,
        materials=results, excluded_appointment_ids=excluded, orders_to_review=review_orders,
        requires_order_review=bool(review_orders),
    ))


def compare_suppliers(
    session: Session, organization_id: str, *, material: str,
    quantity: Decimal, as_of: date, required_by: date,
) -> ToolResult[SupplierComparison]:
    """Compare whole-order offers by confirmation, quantity, deadline and cost.

    Keep unsuitable offers with reasons. Eligible means commercially suitable,
    not authorized to order: policy gaps and approvals remain explicit.
    """
    if material is None or not _valid_material(material) or not _valid_period(as_of, required_by):
        return _error(ToolErrorCode.INVALID_ARGUMENTS, "Provide a material and ordered as_of/required_by dates")
    if not isinstance(quantity, Decimal):
        return _error(ToolErrorCode.INVALID_ARGUMENTS, "quantity must be a positive Decimal")

    try:
        quantity = TypeAdapter(Quantity).validate_python(quantity)
    except ValidationError:
        return _error(ToolErrorCode.INVALID_ARGUMENTS, "quantity must fit a finite nonnegative NUMERIC(16, 3)")
    
    if quantity <= 0:
        return _error(ToolErrorCode.INVALID_ARGUMENTS, "quantity must be greater than zero")

    offers = queries.get_supplier_offers(session, organization_id, material=material)
    if not offers.ok:
        return ToolResult(ok=False, error=offers.error)

    policy_result = queries.get_organization_policy(session, organization_id)
    if not policy_result.ok:
        return ToolResult(ok=False, error=policy_result.error)

    policy = policy_result.data
    compared = []
    with localcontext() as context:
        context.prec = 64
        for offer in offers.data:
            incomplete, rejected, approval = [], [], []
            available = None
            if not offer["confirmed"]:
                incomplete.append("offer_not_confirmed")
            else:
                available = (offer["confirmed_quantity"] if offer["confirmed_quantity"] is not None
                             else offer["quantity"])
                if available < quantity:
                    rejected.append("insufficient_confirmed_quantity")
            
            if offer["quantity"] < quantity:
                rejected.append("insufficient_available_quantity")
            for field in ("unit_price_rub", "delivery_fee_rub", "delivery_date"):
                if offer[field] is None:
                    incomplete.append("unknown_" + field)
            
            if offer["delivery_date"] is not None:
                if offer["delivery_date"] < as_of:
                    rejected.append("delivery_date_in_past")
                elif offer["delivery_date"] > required_by:
                    rejected.append("late_delivery")
            
            total = None
            if offer["unit_price_rub"] is not None and offer["delivery_fee_rub"] is not None:
                total = quantity * offer["unit_price_rub"] + offer["delivery_fee_rub"]
                if total != total.quantize(Decimal("0.01")):
                    incomplete.append("total_requires_rounding_policy")
                if total >= Decimal("100000000000000"):
                    rejected.append("total_exceeds_order_storage_precision")

            commercial_suitability = "rejected" if rejected else "incomplete" if incomplete else "eligible"
            missing_policy = [field for field in (
                "auto_order_limit_rub", "monthly_budget_remaining_rub", "mandatory_human_approval",
                "automatic_ordering_enabled",
            ) if policy[field] is None]
        
            if not offer["supplier_approved"]:
                new_supplier_rule = policy["approval_required_for_new_supplier"]
                if new_supplier_rule is True:
                    approval.append("new_supplier")
                elif new_supplier_rule is None:
                    missing_policy.append("approval_required_for_new_supplier")
                    incomplete.append("unknown_supplier_approval_policy")
                else:
                    rejected.append("supplier_not_approved")
            
            if policy["mandatory_human_approval"] is True:
                approval.append("mandatory_human_approval")
            
            if total is not None:
                limit = policy["auto_order_limit_rub"]
                budget = policy["monthly_budget_remaining_rub"]
                if limit is not None and total > limit:
                    approval.append("auto_order_limit_exceeded")
                if budget is not None and total > budget:
                    rejected.append("monthly_budget_exceeded")
            
            approval_unknown = any(field in missing_policy for field in (
                "auto_order_limit_rub", "mandatory_human_approval", "approval_required_for_new_supplier",
            )) or total is None
            approval_required = True if approval else (None if approval_unknown else False)
        
            suitability = ("rejected" if rejected else "incomplete" if incomplete or missing_policy
                           else "requires_approval" if approval else "eligible")
            automatic_reasons = []

            if policy["automatic_ordering_enabled"] is False:
                automatic_reasons.append("automatic_ordering_disabled")
            if approval:
                automatic_reasons.append("approval_required")
            if rejected or incomplete:
                automatic_reasons.append("offer_not_ready")
            
            automatic_check = ("blocked" if automatic_reasons else "unknown"
                               if missing_policy else "not_blocked")
            automatic_reasons.extend("unknown_" + field for field in missing_policy)

            compared.append(ComparedOffer(
                offer=offer, confirmed_available=available, total_rub=total,
                suitability=suitability, reasons=rejected + incomplete,
                approval_required=approval_required, approval_reasons=approval,
                missing_policy_fields=missing_policy, commercial_suitability=commercial_suitability,
                automatic_ordering_check=automatic_check, automatic_ordering_reasons=automatic_reasons,
            ))

    priorities = {"eligible": 0, "requires_approval": 1, "incomplete": 2, "rejected": 3}
    
    compared.sort(key=lambda row: (
        priorities[row.suitability], row.total_rub if row.total_rub is not None else Decimal("Infinity"),
        row.offer["delivery_date"] or date.max, row.offer["supplier_id"], row.offer["id"],
    ))

    # A supplier can be recommended even when automatic ordering is blocked.
    # actions.py must revalidate the offer before submitting an order.
    candidates = [row for row in compared if row.suitability in ("eligible", "requires_approval")]
    outcome = ("offers_found" if candidates else "incomplete"
               if any(row.suitability == "incomplete" for row in compared) else "no_suitable_offer")

    return ToolResult[SupplierComparison](ok=True, data=SupplierComparison(
        organization_id=organization_id, material=material, quantity=quantity,
        as_of=as_of, required_by=required_by, policy=policy, offers=compared,
        outcome=outcome, recommended_offer_id=candidates[0].offer["id"] if candidates else None,
    ))
