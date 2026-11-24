"""
SQLAlchemy 2.0 ORM models mapping `db/001_schema.sql`.

The SQL file is the source of truth (applied by migrations); these models never
call `create_all`. Postgres ENUM types are declared with `create_type=False`.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, ForeignKey, Identity, Integer, Numeric, Real,
                        SmallInteger, String, Text, text)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, ENUM, INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _enum(name: str, *values: str) -> ENUM:
    return ENUM(*values, name=name, create_type=False)


UserRoleT = _enum("user_role", "user", "org_member", "org_admin", "support", "admin")
PlanTierT = _enum("plan_tier", "free", "pro", "enterprise")
SubStatusT = _enum("subscription_status", "trialing", "active", "past_due", "canceled",
                   "unpaid", "incomplete", "incomplete_expired", "paused")
ProjectStatusT = _enum("project_status", "draft", "processing", "ready", "archived")
AssetKindT = _enum("asset_kind", "room_photo", "wall_mask", "render", "thumbnail", "ar_snapshot")
UploadStatusT = _enum("upload_status", "pending", "uploaded", "verified", "failed")
JobTypeT = _enum("ai_job_type", "wall_segmentation", "palette_recommendation", "render")
JobStatusT = _enum("ai_job_status", "queued", "running", "succeeded", "failed", "canceled")
PaletteSourceT = _enum("palette_source", "manual", "ai", "brand", "system")
FinishT = _enum("paint_finish", "flat", "matte", "eggshell", "satin", "semi_gloss", "gloss")

def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


def _updated() -> Mapped[datetime]:
    # Value maintained by DB trigger; server_onupdate tells the ORM to refetch.
    return mapped_column(DateTime(timezone=True), server_default=text("now()"),
                         server_onupdate=text("now()"), nullable=False)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, uuid.UUID: UUID(as_uuid=True)}


# --------------------------------------------------------------------------- #
class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(CITEXT, unique=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(Text, unique=True)
    seat_limit: Mapped[int] = mapped_column(Integer, server_default=text("5"))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(CITEXT)
    password_hash: Mapped[str | None] = mapped_column(Text)
    full_name: Mapped[str | None] = mapped_column(Text)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(UserRoleT, server_default=text("'user'"))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id"))
    stripe_customer_id: Mapped[str | None] = mapped_column(Text, unique=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_version: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    locale: Mapped[str] = mapped_column(Text, server_default=text("'en'"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organization: Mapped[Organization | None] = relationship(lazy="raise")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    device_label: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(INET)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("refresh_tokens.id"))
    created_at: Mapped[datetime] = _created()


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[uuid.UUID] = _pk()
    tier: Mapped[str] = mapped_column(PlanTierT)
    name: Mapped[str] = mapped_column(Text)
    billing_interval: Mapped[str] = mapped_column(Text)
    stripe_price_id: Mapped[str | None] = mapped_column(Text, unique=True)
    price_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plans.id"))
    stripe_subscription_id: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(SubStatusT)
    seats: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    trial_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class StripeEvent(Base):
    __tablename__ = "stripe_events"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = _created()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class PaintBrand(Base):
    __tablename__ = "paint_brands"
    id: Mapped[uuid.UUID] = _pk()
    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(CITEXT)
    country_code: Mapped[str | None] = mapped_column(String(2))
    website_url: Mapped[str | None] = mapped_column(Text)
    logo_s3_key: Mapped[str | None] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class PaintColor(Base):
    __tablename__ = "paint_colors"
    id: Mapped[uuid.UUID] = _pk()
    brand_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paint_brands.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    hex: Mapped[str] = mapped_column(String(7))
    lab_l: Mapped[float] = mapped_column(Real)
    lab_a: Mapped[float] = mapped_column(Real)
    lab_b: Mapped[float] = mapped_column(Real)
    lrv: Mapped[float | None] = mapped_column(Real)
    collection: Mapped[str | None] = mapped_column(Text)
    finishes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    is_discontinued: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    brand: Mapped[PaintBrand] = relationship(lazy="joined")


class Palette(Base):
    __tablename__ = "palettes"
    id: Mapped[uuid.UUID] = _pk()
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    brand_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("paint_brands.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(PaletteSourceT, server_default=text("'manual'"))
    mood_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    ai_rationale: Mapped[str | None] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    colors: Mapped[list["PaletteColor"]] = relationship(
        back_populates="palette", cascade="all, delete-orphan",
        order_by="PaletteColor.position", lazy="selectin",
    )


class PaletteColor(Base):
    __tablename__ = "palette_colors"
    id: Mapped[uuid.UUID] = _pk()
    palette_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("palettes.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(SmallInteger)
    hex: Mapped[str] = mapped_column(String(7))
    paint_color_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("paint_colors.id", ondelete="SET NULL"))
    role: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text)

    palette: Mapped[Palette] = relationship(back_populates="colors")


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[uuid.UUID] = _pk()
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(Text)
    room_type: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(ProjectStatusT, server_default=text("'draft'"))
    active_palette_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("palettes.id", ondelete="SET NULL"))
    cover_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("project_assets.id", ondelete="SET NULL",
                                                                         use_alter=True))
    room_analysis: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    walls: Mapped[list["ProjectWall"]] = relationship(
        back_populates="project", cascade="all, delete-orphan",
        order_by="ProjectWall.sort_order", lazy="raise",
    )


class ProjectAsset(Base):
    __tablename__ = "project_assets"
    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(AssetKindT)
    s3_key: Mapped[str] = mapped_column(Text, unique=True)
    content_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    width_px: Mapped[int | None] = mapped_column(Integer)
    height_px: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    upload_status: Mapped[str] = mapped_column(UploadStatusT, server_default=text("'pending'"))
    parent_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("project_assets.id", ondelete="CASCADE"))
    capture_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class ProjectWall(Base):
    __tablename__ = "project_walls"
    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    source_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("project_assets.id", ondelete="SET NULL"))
    mask_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("project_assets.id", ondelete="SET NULL"))
    label: Mapped[str] = mapped_column(Text, server_default=text("'Wall'"))
    polygon: Mapped[Any | None] = mapped_column(JSONB)
    ar_anchor: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    area_m2: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    confidence: Mapped[float | None] = mapped_column(Real)
    applied_hex: Mapped[str | None] = mapped_column(String(7))
    paint_color_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("paint_colors.id", ondelete="SET NULL"))
    finish: Mapped[str | None] = mapped_column(FinishT)
    opacity: Mapped[float] = mapped_column(Real, server_default=text("1.0"))
    sort_order: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    project: Mapped[Project] = relationship(back_populates="walls")


class AIJob(Base):
    __tablename__ = "ai_jobs"
    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(JobTypeT)
    status: Mapped[str] = mapped_column(JobStatusT, server_default=text("'queued'"))
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    model_version: Mapped[str | None] = mapped_column(Text)
    cost_micros: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UsageCounter(Base):
    __tablename__ = "usage_counters"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    metric: Mapped[str] = mapped_column(Text, primary_key=True)
    period_start: Mapped[date] = mapped_column(Date, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created()
