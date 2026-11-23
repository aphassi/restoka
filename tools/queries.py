"""Read organization-specific business data without modifying database state."""

from datetime import date, datetime
from decimal import Decimal
from functools import wraps
from inspect import signature

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from typing_extensions import TypedDict

from database.models import (
    Appointment, ConsumptionRule, Inventory, Organization, OrganizationPolicy,
    PurchaseOrder as StoredOrder, Supplier, SupplierOffer,
)
from schemas import PurchaseOrder
from .contracts import ToolError, ToolErrorCode, ToolResult
from .runtime import injected_failure

__all__ = [
    "get_inventory", "get_appointments", "get_supplier_offers",
    "get_pending_orders", "get_organization_policy", "get_consumption_rules",
]


class InventoryRow(TypedDict):
    organization_id: str
    material: str
    quantity: Decimal
    as_of: date


class AppointmentRow(TypedDict):
    organization_id: str
    id: str
    date: date
    service: str
    count: int
    external_id: str | None
    starts_at: datetime | None
    status: str | None
    source_updated_at: datetime | None


class SupplierOfferRow(TypedDict):
    organization_id: str
    id: str
    supplier_id: str
    supplier_approved: bool
    material: str
    quantity: Decimal
    unit_price_rub: Decimal | None
    delivery_fee_rub: Decimal | None
    delivery_date: date | None
    confirmed: bool
    confirmed_quantity: Decimal | None


class PolicyRow(TypedDict):
    organization_id: str
    auto_order_limit_rub: Decimal | None
    monthly_budget_remaining_rub: Decimal | None
    approval_required_for_new_supplier: bool | None
    automatic_ordering_enabled: bool | None
    mandatory_human_approval: bool | None


class ConsumptionRuleRow(TypedDict):
    organization_id: str
    id: int
    material: str
    service: str | None
    units_per_appointment: Decimal


class _InvalidArguments(ValueError):
    pass


def _error(code: ToolErrorCode, message: str) -> ToolResult:
    return ToolResult(ok=False, error=ToolError(
        code=code, message=message, operation_outcome="not_started",
    ))


def _text(name: str, value: str | None, max_length: int | None = None) -> None:
    if value is not None and (
        not isinstance(value, str) or not value.strip()
        or (max_length is not None and len(value) > max_length)
    ):
        raise _InvalidArguments(f"{name} must be a nonblank string within its length limit")


def _dates(date_from: date | None, date_to: date | None) -> None:
    for value in (date_from, date_to):
        if value is not None and type(value) is not date:
            raise _InvalidArguments("Date filters must be date objects, not timestamps or strings")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise _InvalidArguments("date_from must not be later than date_to")


def _read_only(function):
    call_signature = signature(function)

    @wraps(function)
    def wrapped(*args, **kwargs) -> ToolResult:
        failure = injected_failure(function.__name__)
        if failure is not None:
            return failure

        try:
            arguments = call_signature.bind(*args, **kwargs).arguments
        except TypeError:
            return _error(ToolErrorCode.INVALID_ARGUMENTS, "Arguments do not match this query's signature")

        session = arguments["session"]
        organization_id = arguments["organization_id"]
        try:
            if not isinstance(session, Session):
                raise _InvalidArguments("session must be a SQLAlchemy Session")
    
            if organization_id is None:
                raise _InvalidArguments("organization_id is required from trusted context")
    
            _text("organization_id", organization_id, 100)
        
            # Column projections read persisted values even if the identity map
            # contains dirty ORM objects. Suppress implicit writes from autoflush.
            with session.no_autoflush:
                return function(*args, **kwargs)
    
        except _InvalidArguments as exc:
            return _error(ToolErrorCode.INVALID_ARGUMENTS, str(exc))
        except SQLAlchemyError:
            return _error(ToolErrorCode.DATABASE_ERROR, "Unable to read database data")

    return wrapped


def _organization_exists(session: Session, organization_id: str) -> bool:
    return session.scalar(select(Organization.id).where(Organization.id == organization_id)) is not None


def _rows(session: Session, statement) -> list[dict]:
    return [dict(row) for row in session.execute(statement).mappings()]


@_read_only
def get_inventory(
    session: Session, organization_id: str, *, material: str | None = None,
) -> ToolResult[list[InventoryRow]]:
    """Return current inventory records.
    Missing inventory records are treated as missing data, not zero stock."""

    _text("material", material)
    if not _organization_exists(session, organization_id):
        return _error(ToolErrorCode.NOT_FOUND, "Organization not found")

    statement = select(*Inventory.__table__.columns).where(Inventory.organization_id == organization_id)
    if material is not None:
        statement = statement.where(Inventory.material == material)

    rows = _rows(session, statement.order_by(Inventory.material))
    if not rows:
        return _error(ToolErrorCode.MISSING_DATA, "No inventory observations for the requested scope")

    return ToolResult[list[InventoryRow]](ok=True, data=rows)


@_read_only
def get_appointments(
    session: Session, organization_id: str, *, date_from: date | None = None,
    date_to: date | None = None, service: str | None = None,
    appointment_id: str | None = None,
) -> ToolResult[list[AppointmentRow]]:
    """Return appointments filtered by date, service, or appointment ID.

    Date boundaries are inclusive. Without date filters, appointments
    from all dates are returned.
    """

    _dates(date_from, date_to)
    _text("service", service)
    _text("appointment_id", appointment_id, 100)
    if not _organization_exists(session, organization_id):
        return _error(ToolErrorCode.NOT_FOUND, "Organization not found")

    statement = select(*Appointment.__table__.columns).where(Appointment.organization_id == organization_id)

    if date_from is not None:
        statement = statement.where(Appointment.date >= date_from)
    if date_to is not None:
        statement = statement.where(Appointment.date <= date_to)
    if service is not None:
        statement = statement.where(Appointment.service == service)
    if appointment_id is not None:
        statement = statement.where(Appointment.id == appointment_id)

    rows = _rows(session, statement.order_by(Appointment.date, Appointment.id))
    if appointment_id is not None and not rows:
        return _error(ToolErrorCode.NOT_FOUND, "Appointment not found in the requested scope")

    return ToolResult[list[AppointmentRow]](ok=True, data=rows)


@_read_only
def get_supplier_offers(
    session: Session, organization_id: str, *, material: str | None = None,
    supplier_id: str | None = None, offer_id: str | None = None,
    confirmed: bool | None = None,
) -> ToolResult[list[SupplierOfferRow]]:
    """Read offers and supplier approval without excluding incomplete offers."""
    _text("material", material)
    _text("supplier_id", supplier_id, 100)
    _text("offer_id", offer_id, 100)

    if confirmed is not None and type(confirmed) is not bool:
        raise _InvalidArguments("confirmed must be bool or None")

    if not _organization_exists(session, organization_id):
        return _error(ToolErrorCode.NOT_FOUND, "Organization not found")

    if supplier_id is not None and session.scalar(select(Supplier.id).where(
        Supplier.organization_id == organization_id, Supplier.id == supplier_id,
    )) is None:
        return _error(ToolErrorCode.NOT_FOUND, "Supplier not found")

    statement = select(
        *SupplierOffer.__table__.columns, Supplier.approved.label("supplier_approved"),
    ).join(Supplier, and_(
        Supplier.organization_id == SupplierOffer.organization_id,
        Supplier.id == SupplierOffer.supplier_id,
    )).where(SupplierOffer.organization_id == organization_id)

    if material is not None:
        statement = statement.where(SupplierOffer.material == material)
    if supplier_id is not None:
        statement = statement.where(SupplierOffer.supplier_id == supplier_id)
    if offer_id is not None:
        statement = statement.where(SupplierOffer.id == offer_id)
    if confirmed is not None:
        statement = statement.where(SupplierOffer.confirmed == confirmed)
    
    rows = _rows(session, statement.order_by(SupplierOffer.supplier_id, SupplierOffer.id))
    if offer_id is not None and not rows:
        return _error(ToolErrorCode.NOT_FOUND, "Offer not found in the requested scope")

    return ToolResult[list[SupplierOfferRow]](ok=True, data=rows)


@_read_only
def get_pending_orders(
    session: Session, organization_id: str, *, material: str | None = None,
    statuses: tuple[str, ...] | list[str] = ("submitted", "confirmed"),
    order_id: str | None = None,
) -> ToolResult[list[PurchaseOrder]]:
    """Return orders filtered by status, material, or order ID.

    By default, only submitted and confirmed orders are returned.
    Other statuses can be requested explicitly.
    """

    _text("material", material)
    _text("order_id", order_id, 100)

    if not isinstance(statuses, (tuple, list)) or not statuses:
        raise _InvalidArguments("statuses must be a nonempty list or tuple of status strings")

    for status in statuses:
        if status is None:
            raise _InvalidArguments("statuses must contain nonblank strings")
        _text("status", status, 40)
    
    if not _organization_exists(session, organization_id):
        return _error(ToolErrorCode.NOT_FOUND, "Organization not found")

    statement = select(*[
        c for c in StoredOrder.__table__.columns if c.name != "organization_id"
    ]).where(StoredOrder.organization_id == organization_id, StoredOrder.status.in_(statuses))

    if material is not None:
        statement = statement.where(StoredOrder.material == material)
    if order_id is not None:
        statement = statement.where(StoredOrder.id == order_id)
    
    rows = _rows(session, statement.order_by(StoredOrder.eta.asc().nulls_last(), StoredOrder.id))
    if order_id is not None and not rows:
        return _error(ToolErrorCode.NOT_FOUND, "Order not found in the requested scope")

    return ToolResult[list[PurchaseOrder]](ok=True, data=[
        PurchaseOrder(**row, unit_price_rub=None, delivery_fee_rub=None) for row in rows
    ])


@_read_only
def get_organization_policy(
    session: Session, organization_id: str,
) -> ToolResult[PolicyRow]:
    """Read stored policy, preserving unspecified rules as None."""
    if not _organization_exists(session, organization_id):
        return _error(ToolErrorCode.NOT_FOUND, "Organization not found")

    rows = _rows(session, select(*OrganizationPolicy.__table__.columns).where(
        OrganizationPolicy.organization_id == organization_id,
    ))

    if not rows:
        return _error(ToolErrorCode.MISSING_DATA, "Organization policy is not available")

    return ToolResult[PolicyRow](ok=True, data=rows[0])


@_read_only
def get_consumption_rules(
    session: Session, organization_id: str, *, material: str | None = None,
    service: str | None = None,
) -> ToolResult[list[ConsumptionRuleRow]]:
    
    """Return material consumption rules for services.

    Filtering by service includes both service-specific and general rules.
    Missing rules are treated as missing data, not zero consumption.
    """

    _text("material", material)
    _text("service", service)
    if not _organization_exists(session, organization_id):
        return _error(ToolErrorCode.NOT_FOUND, "Organization not found")

    statement = select(*ConsumptionRule.__table__.columns).where(
        ConsumptionRule.organization_id == organization_id,
    )

    if material is not None:
        statement = statement.where(ConsumptionRule.material == material)
    if service is not None:
        statement = statement.where(or_(ConsumptionRule.service == service, ConsumptionRule.service.is_(None)))
    
    rows = _rows(session, statement.order_by(
        ConsumptionRule.material, ConsumptionRule.service.asc().nulls_first(), ConsumptionRule.id,
    ))
    if not rows:
        return _error(ToolErrorCode.MISSING_DATA, "No consumption rules for the requested scope")

    return ToolResult[list[ConsumptionRuleRow]](ok=True, data=rows)
