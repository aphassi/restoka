"""Database models for Restoka."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Index,
    text,
    func,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint("count > 0", name="ck_appointments_positive_count"),
        UniqueConstraint("organization_id", "external_id", name="uq_appointments_external_id"),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    service: Mapped[str] = mapped_column(Text, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)

    # Optional fields for future CRM integration.
    # Not populated from the current eval fixtures.
    external_id: Mapped[str | None] = mapped_column(String(255))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(String(40))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConsumptionRule(Base):
    __tablename__ = "consumption_rules"
    __table_args__ = (
        CheckConstraint("units_per_appointment >= 0", name="ck_rules_nonnegative_units"),
        UniqueConstraint("organization_id", "material", "service", name="uq_rule_service"),
        Index("uq_rule_default", "organization_id", "material", unique=True,
              postgresql_where=text("service IS NULL"), sqlite_where=text("service IS NULL")),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    material: Mapped[str] = mapped_column(Text, nullable=False)

    # Optional per-service rules for future use.
    # Current eval fixtures define consumption at the material level.
    service: Mapped[str | None] = mapped_column(Text)

    units_per_appointment: Mapped[Decimal] = mapped_column(Numeric(16, 3), nullable=False)


class Inventory(Base):
    __tablename__ = "inventory"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_inventory_nonnegative_quantity"),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    material: Mapped[str] = mapped_column(Text, primary_key=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(16, 3), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)


class Supplier(Base):
    __tablename__ = "suppliers"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)


class SupplierOffer(Base):
    __tablename__ = "supplier_offers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            name="fk_offers_supplier_per_organization",
        ),
        CheckConstraint("quantity >= 0", name="ck_offers_nonnegative_quantity"),
        CheckConstraint("confirmed_quantity >= 0 AND confirmed_quantity <= quantity",
                        name="ck_offers_confirmed_quantity"),
        CheckConstraint("unit_price_rub >= 0", name="ck_offers_nonnegative_price"),
        CheckConstraint("delivery_fee_rub >= 0", name="ck_offers_nonnegative_fee"),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    supplier_id: Mapped[str] = mapped_column(String(100), nullable=False)
    material: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(16, 3), nullable=False)
    unit_price_rub: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delivery_fee_rub: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # A supplier may confirm only part of the originally offered quantity later.
    confirmed_quantity: Mapped[Decimal | None] = mapped_column(Numeric(16, 3))


class OrganizationPolicy(Base):
    __tablename__ = "organization_policies"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    auto_order_limit_rub: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    monthly_budget_remaining_rub: Mapped[Decimal | None] = mapped_column(
        Numeric(16, 2)
    )
    approval_required_for_new_supplier: Mapped[bool | None] = mapped_column(
        Boolean
    )
    automatic_ordering_enabled: Mapped[bool | None] = mapped_column(Boolean)
    mandatory_human_approval: Mapped[bool | None] = mapped_column(Boolean)


class PurchaseOrder(Base):
    """One lifecycle: draft, pending_approval, submitted, confirmed, delivered.

    Status is extensible. A draft is not a sent order or an incoming shipment.
    Nullable details support historical imports; submission must validate them.
    """

    __tablename__ = "purchase_orders"
    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "supplier_id"],
                             ["suppliers.organization_id", "suppliers.id"]),
        CheckConstraint("quantity >= 0", name="ck_orders_quantity"),
        CheckConstraint("total_rub >= 0", name="ck_orders_total"),
        UniqueConstraint(
            "organization_id", "operation_id", name="uq_purchase_order_operation"
        ),
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_purchase_order_idempotency"
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    operation_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    supplier_id: Mapped[str | None] = mapped_column(String(100))
    material: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(16, 3))
    total_rub: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))

    # Optional metadata for new orders.
    # Existing eval orders may not contain these fields.
    idempotency_key: Mapped[str | None] = mapped_column(String(255))

    eta: Mapped[date | None] = mapped_column(Date)


class BusinessEvent(Base):
    __tablename__ = "business_events"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Optional details for newly recorded business events.
    # Not present in the current eval action_log.
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "order_id"],
                             ["purchase_orders.organization_id", "purchase_orders.id"]),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    order_id: Mapped[str | None] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    decision: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_owner", "organization_id", "user_id"),)

    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "conversation_id"],
                             ["conversations.organization_id", "conversations.id"]),
        UniqueConstraint("organization_id", "conversation_id", "sequence", name="uq_message_sequence"),
        CheckConstraint("role IN ('user', 'assistant', 'tool')", name="ck_message_role"),
        CheckConstraint("sequence > 0", name="ck_message_sequence"),
    )

    organization_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(100), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    group_start: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[Any] = mapped_column(JSON, nullable=True)
    tool_calls: Mapped[list | None] = mapped_column(JSON)
    tool_call_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
