"""Validates and prices the offers supplied in a read-only table attachment."""
from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, field_validator

from schemas import Money, Quantity

from .context import _rows


class AttachedOffer(BaseModel):
    model_config = ConfigDict(extra="ignore")
    offer_id: str
    supplier_id: str
    material: str
    available_quantity: Quantity
    order_quantity: Quantity
    unit_price_rub: Money
    delivery_fee_rub: Money
    confirmed: bool

    @field_validator("available_quantity", "order_quantity", "unit_price_rub", "delivery_fee_rub", mode="before")
    @classmethod
    def accept_json_numbers(cls, value):
        return str(value) if type(value) in (int, float) else value


def compare_offer_table(event: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
    data = event.get("data")
    if (event.get("kind") != "table" or not isinstance(data, dict)
            or data.get("format") != "json_rows" or not isinstance(data.get("table"), list)):
        return None
    try:
        offers = TypeAdapter(list[AttachedOffer]).validate_python(data["table"])
    except ValidationError:
        return None
    if (not offers or len({row.offer_id for row in offers}) != len(offers)
            or len({(row.material, row.order_quantity) for row in offers}) != 1
            or offers[0].order_quantity <= 0):
        return None
    approved = {row["id"] for row in _rows(snapshot, "suppliers") if row.get("approved") is True}
    quantity = offers[0].order_quantity
    with localcontext() as ctx:
        ctx.prec = 64
        totals = {row.offer_id: quantity * row.unit_price_rub + row.delivery_fee_rub for row in offers
                  if row.confirmed and row.available_quantity >= quantity and row.supplier_id in approved}
    if not totals:
        return None
    best = min(totals.values())
    winners = [row for row in offers if totals.get(row.offer_id) == best]
    if len(winners) != 1:
        return None
    return {"chosen_supplier": winners[0].supplier_id, "quantity": str(quantity),
            "delivered_total_rub": str(best), "source_ids": [row.offer_id for row in offers]}
