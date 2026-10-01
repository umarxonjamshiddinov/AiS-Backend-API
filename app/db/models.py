from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(String(100), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, server_default=text("CURRENT_TIMESTAMP")
    )


class ModelRoute(Base):
    __tablename__ = "model_routes"
    alias: Mapped[str] = mapped_column(String(100), primary_key=True)
    provider: Mapped[str] = mapped_column(String(30))
    upstream_model: Mapped[str] = mapped_column(String(200))
    input_price: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    output_price: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    external_api_allowed: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    provider_transport: Mapped[str] = mapped_column(String(40), default="converse")
    capabilities: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    max_context_tokens: Mapped[int | None] = mapped_column(Integer)
    model_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    manufacturer: Mapped[str | None] = mapped_column(String(100))
    region: Mapped[str | None] = mapped_column(String(40))
    api_route: Mapped[str | None] = mapped_column(String(40))
    model_type: Mapped[str | None] = mapped_column(String(40))
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    supports_vision: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    supports_reasoning: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    reasoning_efforts: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    vision_behavior: Mapped[str] = mapped_column(String(30), default="none", server_default="none")
    catalog_metadata: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now, server_default=text("CURRENT_TIMESTAMP")
    )
    __table_args__ = (
        Index(
            "uq_catalog_model_id",
            "upstream_model",
            unique=True,
            postgresql_where=text("model_key IS NOT NULL"),
        ),
    )


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"),
        index=True,
        server_default="00000000-0000-4000-8000-000000000001",
    )
    user: Mapped[User] = relationship(lazy="selectin")
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(24))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=0)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=0)
    rpm: Mapped[int | None] = mapped_column(Integer)
    tpm: Mapped[int | None] = mapped_column(Integer)
    requests_per_day: Mapped[int | None] = mapped_column(BigInteger)
    requests_per_month: Mapped[int | None] = mapped_column(BigInteger)
    tokens_per_day: Mapped[int | None] = mapped_column(BigInteger)
    tokens_per_month: Mapped[int | None] = mapped_column(BigInteger)
    max_concurrent_requests: Mapped[int | None] = mapped_column(Integer)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safety_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    all_public_models: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class KeyModel(Base):
    __tablename__ = "key_models"
    key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id"), primary_key=True)
    alias: Mapped[str] = mapped_column(ForeignKey("model_routes.alias"), primary_key=True)


class Usage(Base):
    __tablename__ = "usage"
    __table_args__ = (
        Index("ix_usage_key_created", "key_id", "created_at"),
        Index(
            "ix_usage_active_lease",
            "lease_expires_at",
            postgresql_where=text("status = 'reserved'"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    key_id: Mapped[str | None] = mapped_column(ForeignKey("api_keys.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    alias: Mapped[str] = mapped_column(ForeignKey("model_routes.alias"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    status: Mapped[str] = mapped_column(String(30), default="reserved")
    reserved_tokens: Mapped[int] = mapped_column(Integer)
    reserved_cost: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    input_price: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    output_price: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str] = mapped_column(String(30), default="unknown", server_default="unknown")
    upstream_model: Mapped[str] = mapped_column(String(200), default="", server_default="")
    accounted_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    usage_estimated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    error_code: Mapped[str | None] = mapped_column(String(100))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model_key: Mapped[str | None] = mapped_column(String(100))
    region: Mapped[str | None] = mapped_column(String(40))
    api_route: Mapped[str | None] = mapped_column(String(40))
    purpose: Mapped[str] = mapped_column(String(30), default="chat", server_default="chat")
    execution_metadata: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
