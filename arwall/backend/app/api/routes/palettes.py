"""/palettes — user palettes, curated/public palettes, and AI-saved palettes."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Query, status
from sqlalchemy import func, or_, select

from app.api.deps import DB, CurrentEntitlements, CurrentUser, load_palette
from app.api.schemas import PaletteIn, PaletteOut
from app.core.errors import QuotaExceeded
from app.db.models import Palette, PaletteColor
from app.services.catalog_service import nearest_paints
from app.services.entitlements import UNLIMITED

router = APIRouter(prefix="/palettes", tags=["palettes"])

AUTO_MATCH_MAX_DELTA_E = 5.0   # don't attach a paint that is visibly different


async def _build_colors(db, user, body: PaletteIn) -> list[PaletteColor]:
    colors: list[PaletteColor] = []
    for pos, c in enumerate(body.colors):
        paint_id = c.paint_color_id
        if paint_id is None and body.auto_match_paints:
            matches = await nearest_paints(db, c.hex, organization_id=user.organization_id, limit=1)
            if matches and matches[0].delta_e <= AUTO_MATCH_MAX_DELTA_E:
                paint_id = matches[0].paint.id
        colors.append(PaletteColor(position=pos, hex=c.hex, paint_color_id=paint_id,
                                   role=c.role, label=c.label))
    return colors


@router.get("", response_model=list[PaletteOut])
async def list_palettes(user: CurrentUser, db: DB,
                        scope: str = Query("mine", pattern="^(mine|public|all)$"),
                        mood: str | None = None,
                        limit: int = Query(50, ge=1, le=100)):
    mine = Palette.owner_user_id == user.id
    public = or_(Palette.is_public.is_(True), Palette.source.in_(("system", "brand")))
    where = {"mine": mine, "public": public, "all": or_(mine, public)}[scope]
    stmt = (select(Palette).where(where, Palette.deleted_at.is_(None))
            .order_by(Palette.created_at.desc()).limit(limit))
    if mood:
        stmt = stmt.where(Palette.mood_tags.any(mood.lower()))
    return [PaletteOut.model_validate(p) for p in (await db.execute(stmt)).scalars().all()]


@router.post("", response_model=PaletteOut, status_code=status.HTTP_201_CREATED)
async def create_palette(body: PaletteIn, user: CurrentUser, ent: CurrentEntitlements, db: DB):
    limit = ent.limit("custom_palettes")
    if limit != UNLIMITED:
        count = (await db.execute(select(func.count()).select_from(Palette).where(
            Palette.owner_user_id == user.id, Palette.deleted_at.is_(None)))).scalar_one()
        if count >= limit:
            raise QuotaExceeded(f"The {ent.tier} plan allows {limit} saved palettes",
                                extra={"metric": "palettes", "limit": limit, "tier": ent.tier})
    palette = Palette(owner_user_id=user.id, organization_id=user.organization_id, name=body.name,
                      description=body.description, source="manual",
                      mood_tags=[t.lower()[:30] for t in body.mood_tags], is_public=body.is_public,
                      colors=await _build_colors(db, user, body))
    db.add(palette)
    await db.commit()
    await db.refresh(palette, attribute_names=["colors"])
    return PaletteOut.model_validate(palette)


@router.get("/{palette_id}", response_model=PaletteOut)
async def get_palette(palette_id: uuid.UUID, user: CurrentUser, db: DB):
    return PaletteOut.model_validate(await load_palette(db, user, palette_id))


@router.put("/{palette_id}", response_model=PaletteOut)
async def replace_palette(palette_id: uuid.UUID, body: PaletteIn, user: CurrentUser, db: DB):
    palette = await load_palette(db, user, palette_id, write=True)
    palette.name, palette.description, palette.is_public = body.name, body.description, body.is_public
    palette.mood_tags = [t.lower()[:30] for t in body.mood_tags]
    palette.colors.clear()
    await db.flush()   # delete old rows first so (palette_id, position) stays unique
    palette.colors.extend(await _build_colors(db, user, body))
    await db.commit()
    await db.refresh(palette, attribute_names=["colors"])
    return PaletteOut.model_validate(palette)


@router.delete("/{palette_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_palette(palette_id: uuid.UUID, user: CurrentUser, db: DB):
    palette = await load_palette(db, user, palette_id, write=True)
    palette.deleted_at = datetime.now(timezone.utc)
    await db.commit()
