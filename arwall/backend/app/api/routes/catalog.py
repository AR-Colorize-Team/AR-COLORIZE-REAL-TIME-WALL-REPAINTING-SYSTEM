"""/catalog — paint brands, colours, nearest-paint matching, private catalog import."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert

from app.api.deps import DB, CurrentUser, require
from app.api.schemas import BrandOut, CatalogImportIn, ColorMatchOut, HexColor, PaintColorOut
from app.core.errors import Forbidden, NotFound
from app.core.rbac import Perm, has_permission
from app.db.models import PaintBrand, PaintColor, User
from app.services.catalog_service import nearest_paints, visible_brands_clause
from app.utils.color import hex_to_lab

router = APIRouter(prefix="/catalog", tags=["catalog"])


def _quality(de: float) -> str:
    return "exact" if de < 1 else "very_close" if de < 2.3 else "close" if de < 5 else "approximate"


@router.get("/brands", response_model=list[BrandOut])
async def list_brands(user: CurrentUser, db: DB):
    rows = (await db.execute(select(PaintBrand).where(visible_brands_clause(user.organization_id))
                             .order_by(PaintBrand.name))).scalars().all()
    return [BrandOut(id=b.id, name=b.name, slug=b.slug, country_code=b.country_code,
                     website_url=b.website_url, is_private=b.organization_id is not None) for b in rows]


@router.get("/brands/{brand_id}/colors", response_model=list[PaintColorOut])
async def list_brand_colors(brand_id: uuid.UUID, user: CurrentUser, db: DB,
                            q: str | None = Query(None, min_length=1, max_length=60),
                            collection: str | None = None,
                            offset: int = Query(0, ge=0, le=20000),
                            limit: int = Query(100, ge=1, le=500)):
    brand = (await db.execute(select(PaintBrand).where(
        PaintBrand.id == brand_id, visible_brands_clause(user.organization_id)))).scalar_one_or_none()
    if brand is None:
        raise NotFound("Brand not found")
    stmt = (select(PaintColor).where(PaintColor.brand_id == brand.id, PaintColor.is_discontinued.is_(False))
            .order_by(PaintColor.lab_l.desc()).offset(offset).limit(limit))
    if q:
        like = f"{q.lower()}%"
        stmt = stmt.where(or_(PaintColor.name.ilike(like), PaintColor.code.ilike(like)))
    if collection:
        stmt = stmt.where(PaintColor.collection == collection)
    return [PaintColorOut.model_validate(c) for c in (await db.execute(stmt)).scalars().unique().all()]


@router.get("/match", response_model=list[ColorMatchOut])
async def match_color(hex: HexColor, user: CurrentUser, db: DB,
                      brand_id: list[uuid.UUID] | None = Query(None),
                      limit: int = Query(5, ge=1, le=20)):
    """Nearest real paints to a HEX (ΔE2000). Powers 'Buy this colour' in the AR view."""
    matches = await nearest_paints(db, hex, organization_id=user.organization_id,
                                   brand_ids=brand_id, limit=limit)
    return [ColorMatchOut(paint=PaintColorOut.model_validate(m.paint), brand_name=m.paint.brand.name,
                          delta_e=round(m.delta_e, 2), match_quality=_quality(m.delta_e))
            for m in matches]


@router.post("/brands/{brand_id}/colors:import", status_code=200)
async def import_colors(brand_id: uuid.UUID, body: CatalogImportIn, db: DB,
                        user: User = Depends(require(Perm.CATALOG_MANAGE_ORG))):
    """
    Bulk upsert a brand's colour list (Enterprise: paint manufacturers & retailers).
    Org admins may only write to their own organization's private brands; platform
    admins may write to global brands. Lab values are computed server-side.
    """
    brand = await db.get(PaintBrand, brand_id)
    if brand is None:
        raise NotFound("Brand not found")
    if brand.organization_id is None:
        if not has_permission(user.role, Perm.CATALOG_MANAGE_GLOBAL):
            raise Forbidden("Global catalogs are managed by platform admins")
    elif brand.organization_id != user.organization_id:
        raise NotFound("Brand not found")

    rows = []
    for c in body.colors:
        L, a, b = hex_to_lab(c.hex)
        rows.append({"brand_id": brand.id, "code": c.code, "name": c.name, "hex": c.hex,
                     "lab_l": L, "lab_a": a, "lab_b": b, "lrv": c.lrv,
                     "collection": c.collection, "finishes": c.finishes, "is_discontinued": False})
    stmt = insert(PaintColor).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[PaintColor.brand_id, PaintColor.code],
        set_={k: getattr(stmt.excluded, k) for k in
              ("name", "hex", "lab_l", "lab_a", "lab_b", "lrv", "collection", "finishes", "is_discontinued")},
    )
    await db.execute(stmt)
    await db.commit()
    return {"upserted": len(rows)}
