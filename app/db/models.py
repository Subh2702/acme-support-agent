"""SQLAlchemy 2.x models. Money columns are integer paise; timestamps are UTC."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.timeutil import ensure_utc, utcnow


class UTCDateTime(TypeDecorator[datetime]):
    """Always hands back timezone-aware UTC datetimes, on SQLite and Postgres alike."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return ensure_utc(value) if value is not None else None


class Base(DeclarativeBase):
    pass


class OrderStatus:
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    ALL = (PROCESSING, SHIPPED, DELIVERED, CANCELLED, REFUNDED)


class PaymentMethod:
    UPI = "upi"
    CREDIT_CARD = "credit_card"
    DEBIT_CARD = "debit_card"
    NET_BANKING = "net_banking"
    COD = "cod"
    ALL = (UPI, CREDIT_CARD, DEBIT_CARD, NET_BANKING, COD)
    LABELS: ClassVar[dict[str, str]] = {
        UPI: "UPI",
        CREDIT_CARD: "Credit card",
        DEBIT_CARD: "Debit card",
        NET_BANKING: "Net banking",
        COD: "Cash on delivery",
    }


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(20))
    city: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    orders: Mapped[list[Order]] = relationship(back_populates="customer")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_number: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    status: Mapped[str] = mapped_column(String(20))
    payment_method: Mapped[str] = mapped_column(String(20))
    total_paise: Mapped[int] = mapped_column(Integer)
    ship_name: Mapped[str] = mapped_column(String(120))
    ship_line1: Mapped[str] = mapped_column(String(200))
    ship_line2: Mapped[str] = mapped_column(String(200), default="")
    ship_city: Mapped[str] = mapped_column(String(80))
    ship_state: Mapped[str] = mapped_column(String(80))
    ship_pin: Mapped[str] = mapped_column(String(6))
    ship_phone: Mapped[str] = mapped_column(String(20))
    courier: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tracking_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    shipped_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    address_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    customer: Mapped[Customer] = relationship(back_populates="orders")
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.id"
    )
    refunds: Mapped[list[Refund]] = relationship(back_populates="order", order_by="Refund.id")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    sku: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_paise: Mapped[int] = mapped_column(Integer)

    order: Mapped[Order] = relationship(back_populates="items")


class RefundStatus:
    ISSUED = "issued"
    PENDING_APPROVAL = "pending_approval"
    REJECTED = "rejected"


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    amount_paise: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text, default="")
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    order: Mapped[Order] = relationship(back_populates="refunds")


class TicketStatus:
    OPEN = "open"
    RESOLVED = "resolved"


class Ticket(Base):
    """A human-escalation ticket, with the structured handoff summary."""

    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    reason_code: Mapped[str] = mapped_column(String(60))
    reason: Mapped[str] = mapped_column(Text)
    urgency: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default=TicketStatus.OPEN)
    order_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    client_ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", order_by="Message.id", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class TraceKind:
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    POLICY = "policy_decision"
    GUARDRAIL = "guardrail"
    ROUTER = "router"
    ESCALATION = "escalation"
    ERROR = "error"
    TURN = "turn"


class TraceEvent(Base):
    """One step of an agent turn. Payloads are PII-masked before they are written."""

    __tablename__ = "trace_events"
    __table_args__ = (Index("ix_trace_conv_id", "conversation_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64))
    turn_index: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_paise: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class AppMeta(Base):
    """Small key/value table (e.g. when the demo data was seeded)."""

    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
