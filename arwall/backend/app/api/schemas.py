"""Request/response DTOs. Never return ORM objects directly — explicit contracts only."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.security import validate_password_strength
from app.utils.color import normalize_hex

HexColor = Annotated[str, AfterValidator(normalize_hex)]
Password = Annotated[str, AfterValidator(validate_password_strength)]

RoomType = Literal["living_room", "bedroom", "kitchen", "bathroom", "office",
                   "dining_room", "kids_room", "exterior", "other"]
Finish = Literal["flat", "matte", "eggshell", "satin", "semi_gloss", "gloss"]
ImageType = Literal["image/jpeg", "image/png", "image/heic", "image/webp"]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
class RegisterIn(BaseModel):
    email: EmailStr
    password: Password
    full_name: str | None = Field(None, max_length=120)
    device_label: str | None = Field(None, max_length=100)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)
    device_label: str | None = Field(None, max_length=100)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=200)


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int


class UserOut(ORM):
    id: uuid.UUID
    email: str
    full_name: str | None
    avatar_url: str | None
    role: str
    organization_id: uuid.UUID | None
    email_verified: bool = False
    locale: str
    created_at: datetime

    @classmethod
    def from_user(cls, u: Any) -> "UserOut":
        return cls(id=u.id, email=u.email, full_name=u.full_name, avatar_url=u.avatar_url,
                   role=u.role, organization_id=u.organization_id,
                   email_verified=u.email_verified_at is not None, locale=u.locale,
                   created_at=u.created_at)


class AuthOut(BaseModel):
    user: UserOut
    tokens: TokenOut


class UserUpdateIn(BaseModel):
    full_name: str | None = Field(None, max_length=120)
    avatar_url: str | None = Field(None, max_length=500)
    locale: str | None = Field(None, pattern=r"^[a-z]{2}(-[A-Z]{2})?$")


class ChangePasswordIn(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: Password


# --------------------------------------------------------------------------- #
# Entitlements
# --------------------------------------------------------------------------- #
class EntitlementsOut(BaseModel):
    tier: str
    limits: dict[str, Any]
    usage_this_month: dict[str, int]
    subscription_status: str | None
    current_period_end: datetime | None


# --------------------------------------------------------------------------- #
# Projects, walls, assets
# --------------------------------------------------------------------------- #
class ProjectCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    room_type: RoomType | None = None


class ProjectUpdateIn(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    room_type: RoomType | None = None
    status: Literal["draft", "ready", "archived"] | None = None
    active_palette_id: uuid.UUID | None = None
    cover_asset_id: uuid.UUID | None = None


class WallIn(BaseModel):
    id: uuid.UUID | None = None                   # present = update existing, absent = create
    label: str = Field("Wall", max_length=60)
    source_asset_id: uuid.UUID | None = None
    mask_asset_id: uuid.UUID | None = None
    polygon: list[list[float]] | None = Field(None, max_length=2000)
    ar_anchor: dict[str, Any] | None = None
    area_m2: Decimal | None = Field(None, ge=0, le=10000)
    confidence: float | None = Field(None, ge=0, le=1)
    applied_hex: HexColor | None = None
    paint_color_id: uuid.UUID | None = None
    finish: Finish | None = None
    opacity: float = Field(1.0, ge=0, le=1)
    sort_order: int = Field(0, ge=0, le=100)

    @field_validator("polygon")
    @classmethod
    def _normalised(cls, v: list[list[float]] | None):
        if v is None:
            return v
        if len(v) < 3:
            raise ValueError("polygon needs at least 3 points")
        for pt in v:
            if len(pt) != 2 or not all(0.0 <= c <= 1.0 for c in pt):
                raise ValueError("polygon points must be [x, y] normalised to 0..1")
        return v


class WallsReplaceIn(BaseModel):
    walls: list[WallIn] = Field(max_length=50)


class WallOut(ORM):
    id: uuid.UUID
    label: str
    source_asset_id: uuid.UUID | None
    mask_asset_id: uuid.UUID | None
    polygon: Any | None
    ar_anchor: dict[str, Any] | None
    area_m2: Decimal | None
    confidence: float | None
    applied_hex: str | None
    paint_color_id: uuid.UUID | None
    finish: str | None
    opacity: float
    sort_order: int


class AssetOut(ORM):
    id: uuid.UUID
    kind: str
    content_type: str
    size_bytes: int | None
    width_px: int | None
    height_px: int | None
    upload_status: str
    parent_asset_id: uuid.UUID | None
    created_at: datetime


class ProjectOut(ORM):
    id: uuid.UUID
    name: str
    room_type: str | None
    status: str
    active_palette_id: uuid.UUID | None
    cover_asset_id: uuid.UUID | None
    room_analysis: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ProjectDetailOut(ProjectOut):
    walls: list[WallOut]
    assets: list[AssetOut]


class Page(BaseModel):
    items: list[Any]
    next_cursor: str | None


class UploadRequestIn(BaseModel):
    kind: Literal["room_photo", "ar_snapshot", "thumbnail"]   # masks/renders are written by workers
    content_type: ImageType
    size_bytes: int = Field(gt=1024, description="Declared size; S3 policy enforces it as the maximum")
    width_px: int | None = Field(None, gt=0, le=20000)
    height_px: int | None = Field(None, gt=0, le=20000)
    sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    capture_meta: dict[str, Any] = Field(default_factory=dict)


class UploadTicketOut(BaseModel):
    asset: AssetOut
    upload_url: str
    upload_fields: dict[str, str]
    expires_in: int


class DownloadUrlOut(BaseModel):
    url: str
    expires_in: int


# --------------------------------------------------------------------------- #
# Palettes & catalog
# --------------------------------------------------------------------------- #
class PaletteColorIn(BaseModel):
    hex: HexColor
    paint_color_id: uuid.UUID | None = None
    role: Literal["primary", "secondary", "accent", "trim", "ceiling"] | None = None
    label: str | None = Field(None, max_length=60)


class PaletteIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(None, max_length=1000)
    mood_tags: list[str] = Field(default_factory=list, max_length=10)
    colors: list[PaletteColorIn] = Field(min_length=1, max_length=12)
    is_public: bool = False
    auto_match_paints: bool = True   # fill paint_color_id with nearest real paint when missing


class PaletteColorOut(ORM):
    position: int
    hex: str
    paint_color_id: uuid.UUID | None
    role: str | None
    label: str | None


class PaletteOut(ORM):
    id: uuid.UUID
    name: str
    description: str | None
    source: str
    mood_tags: list[str]
    ai_rationale: str | None
    is_public: bool
    owner_user_id: uuid.UUID | None
    colors: list[PaletteColorOut]
    created_at: datetime


class BrandOut(ORM):
    id: uuid.UUID
    name: str
    slug: str
    country_code: str | None
    website_url: str | None
    is_private: bool = False


class PaintColorOut(ORM):
    id: uuid.UUID
    brand_id: uuid.UUID
    code: str
    name: str
    hex: str
    lrv: float | None
    collection: str | None
    finishes: list[str]


class ColorMatchOut(BaseModel):
    paint: PaintColorOut
    brand_name: str
    delta_e: float
    match_quality: Literal["exact", "very_close", "close", "approximate"]


class PaintColorImportIn(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    hex: HexColor
    lrv: float | None = Field(None, ge=0, le=100)
    collection: str | None = Field(None, max_length=80)
    finishes: list[Finish] = Field(default_factory=list)


class CatalogImportIn(BaseModel):
    colors: list[PaintColorImportIn] = Field(min_length=1, max_length=5000)


# --------------------------------------------------------------------------- #
# AI jobs
# --------------------------------------------------------------------------- #
class SegmentIn(BaseModel):
    asset_id: uuid.UUID
    exclude_classes: list[Literal["door", "window", "switchboard", "furniture", "artwork", "ceiling"]] = \
        Field(default_factory=lambda: ["door", "window", "switchboard", "furniture", "artwork"])


class RecommendIn(BaseModel):
    asset_id: uuid.UUID
    style_preferences: list[str] = Field(default_factory=list, max_length=8)
    mood: str | None = Field(None, max_length=60)
    brand_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    palette_count: int = Field(3, ge=1, le=5)


class AIJobOut(ORM):
    id: uuid.UUID
    type: str
    status: str
    project_id: uuid.UUID | None
    output: dict[str, Any] | None
    error: str | None
    queued_at: datetime
    finished_at: datetime | None


class JobResultIn(BaseModel):
    """Posted by the AI worker to the internal callback."""
    status: Literal["running", "succeeded", "failed"]
    output: dict[str, Any] | None = None
    error: str | None = Field(None, max_length=4000)
    model_version: str | None = None
    cost_micros: int = Field(0, ge=0)


# --------------------------------------------------------------------------- #
# Billing
# --------------------------------------------------------------------------- #
class PlanOut(ORM):
    id: uuid.UUID
    tier: str
    name: str
    billing_interval: str
    price_cents: int
    currency: str
    limits: dict[str, Any]


class CheckoutIn(BaseModel):
    plan_id: uuid.UUID


class RedirectOut(BaseModel):
    url: str


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
class RoleUpdateIn(BaseModel):
    role: Literal["user", "org_member", "org_admin", "support", "admin"]
    organization_id: uuid.UUID | None = None
