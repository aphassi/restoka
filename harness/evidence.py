"""Observed state and deltas only; no expected outcomes or scoring."""

from sqlalchemy import select
from database.models import Base
from .trace import json_value

def snapshot(engine, *, include_history=True):
    with engine.connect() as conn:
        return {table.name: {"count": len(rows), "rows": json_value(rows)}
                for table in Base.metadata.sorted_tables if include_history or table.name not in ("conversations", "messages")
                for rows in [[dict(row) for row in conn.execute(
                    select(table).order_by(*table.primary_key.columns)).mappings()]]}

def business_changes(before, after):
    def rows(state, table):
        return {r["id"]: r for r in (state or {}).get(table, {}).get("rows", [])}
    
    old_orders, new_orders = rows(before,"purchase_orders"), rows(after,"purchase_orders")
    old_approvals, new_approvals = rows(before,"approval_requests"), rows(after,"approval_requests")
    old_offers, new_offers = rows(before,"supplier_offers"), rows(after,"supplier_offers")
    old_events, new_events = rows(before,"business_events"), rows(after,"business_events")

    observations = [dict(event_id=k, type=e["type"], date=e["date"], details=e.get("details"))
        for k,e in new_events.items() if k not in old_events and
        e["type"] in ("order_submission_started","order_submission_result","supplier_message_started",
                      "supplier_message_result","daily_review_started","daily_review_result")]
    
    unknown = [dict(event_id=k, type=e["type"], details=e.get("details")) for k,e in new_events.items()
        if e["type"] in ("order_submission","supplier_message","daily_review")
        and (e.get("details") or {}).get("state") in ("sending","unknown")]
    
    return dict(orders_before=list(old_orders.values()),
        orders_created=[v for k,v in new_orders.items() if k not in old_orders],
        order_status_changes=[dict(order_id=k, before=old_orders[k]["status"], after=v["status"])
                              for k,v in new_orders.items() if k in old_orders and old_orders[k]["status"]!=v["status"]],
        approvals_created=[v for k,v in new_approvals.items() if k not in old_approvals],
        decisions=[v for k,v in new_events.items() if k not in old_events and v["type"]=="approval_decided"],
        offers_changed=[dict(offer_id=k,before=old_offers.get(k),after=v)
                        for k,v in new_offers.items() if old_offers.get(k)!=v],
        external_observations=observations,
        external_completed=[o for o in observations if (o.get("details") or {}).get("state") in ("sent", "delivered")],
        unknown_operations=unknown)


class StateAccess:
    def __init__(self, engine, trace):
        self._engine, self._trace = engine, trace

    def load(self):
        state = snapshot(self._engine, include_history=False)
        self._trace.event("recovery", source="postgresql")
        return state
