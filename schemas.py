"""Pydantic schemas for structured data exchanged between the LLM and Python tools.

The LLM extracts information from user requests and supplier messages,
selects actions through native tool calling, and explains decisions.
These schemas define and validate the structure of extracted data.

Python tools independently verify the extracted information, calculate
costs and material needs, enforce business rules, and create actual
orders. PurchaseOrder represents a verified order returned by Python,
not an order generated or authorized by the LLM.

Pydantic validates data exchanged between components, while SQLAlchemy
models define how business data is stored in PostgreSQL. Valid JSON
does not guarantee factual correctness or authorize an action.

Unknown values are represented as null and must not automatically
overwrite existing database values. Organization identity comes from
trusted application context. Monetary values and quantities are
represented as decimal strings in JSON and Decimal in Python.
"""

from datetime import date
from decimal import Decimal
from functools import partial
import re
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, WithJsonSchema

__all__ = [
    "SupplierOffer", "PurchaseOrder", "ApprovalRequest",
    "DecisionExplanation", "Money", "Quantity", "DecisionAction",
]


def _exact_decimal(value: object, *, pattern: str) -> str:
    if isinstance(value, Decimal):
        value = format(value, "f")

    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError("Use a nonnegative fixed-point decimal string within the field precision")

    return value


_MONEY_PATTERN = r"(0|[1-9][0-9]{0,13})(\.[0-9]{1,2})?"
_QUANTITY_PATTERN = r"(0|[1-9][0-9]{0,12})(\.[0-9]{1,3})?"
Money = Annotated[
    Decimal,
    Field(strict=False, ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False),
    BeforeValidator(partial(_exact_decimal, pattern=_MONEY_PATTERN)),
    PlainSerializer(lambda value: format(value, "f"), return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "pattern": "^" + _MONEY_PATTERN + "$"}),
]
Quantity = Annotated[
    Decimal,
    Field(strict=False, ge=0, max_digits=16, decimal_places=3, allow_inf_nan=False),
    BeforeValidator(partial(_exact_decimal, pattern=_QUANTITY_PATTERN)),
    PlainSerializer(lambda value: format(value, "f"), return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "pattern": "^" + _QUANTITY_PATTERN + "$"}),
]
_Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
_Id = Annotated[_Text, Field(max_length=100)]
_OperationId = Annotated[_Text, Field(max_length=255)]
_Status = Annotated[_Text, Field(max_length=40)]
DecisionAction = Literal["act", "clarify", "escalate", "refuse"]


class _LLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SupplierOffer(_LLMResponse):
    """Supplier claims, not a verified offer or automatic database update.

    confirmed and confirmed_quantity describe what the supplier appears to claim.
    Even confirmed=True requires tool-side verification of the source, meaning
    and current offer before persisting confirmation or using it for ordering.
    None means unknown, not false, zero or an instruction to clear stored data.
    """

    id: _Id | None
    supplier_id: _Id | None
    material: _Text | None
    quantity: Quantity | None
    unit_price_rub: Money | None
    delivery_fee_rub: Money | None
    delivery_date: date | None
    confirmed: bool | None
    confirmed_quantity: Quantity | None


class PurchaseOrder(BaseModel):
    """Checked order returned by Python tools, never an LLM-generated command.

    Code supplies id, total_rub, status, operation_id and idempotency_key after
    checking state and permissions. This transfer object is not the ORM record;
    unit price and delivery fee can accompany the tool's checked calculation.
    Unknown historical details remain None. Status uses the storage vocabulary
    (draft, pending_approval, submitted, confirmed, delivered) and permits other
    historical statuses. A draft is not evidence of submission.

    Construct this from trusted tool results, e.g. ToolResult[PurchaseOrder].
    Parsing an arbitrary JSON object as this type does not make it trusted.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    id: _Id | None
    supplier_id: _Id | None
    material: _Text | None
    quantity: Quantity | None
    unit_price_rub: Money | None
    delivery_fee_rub: Money | None
    total_rub: Money | None
    eta: date | None
    status: _Status | None
    operation_id: _OperationId | None
    idempotency_key: _OperationId | None


class ApprovalRequest(_LLMResponse):
    """Proposed escalation; contains no manager decision or authorization flag.

    order_id is an untrusted reference to an existing order or draft. It can be
    null before a draft exists; reason and source_ids then describe the proposal.
    Tools resolve the order and determine whether approval is required. A checked
    PurchaseOrder is supplied separately by code, never nested in LLM input.
    This object cannot record approval, a manager identity or a final order cost.
    """

    order_id: _Id | None
    reason: _Text
    source_ids: list[_Text]


class DecisionExplanation(_LLMResponse):
    """LLM explanation of a code-checked decision, not a dispatch instruction.

    Source IDs and factual claims must be checked against trusted tool results.

    act alone does not distinguish preparation from actual submission. The
    trusted tool result and persisted order status provide that evidence.
    """

    action: DecisionAction
    explanation: _Text
    source_ids: list[_Text]
    missing_information: list[_Text]
    clarification_question: _Text | None
