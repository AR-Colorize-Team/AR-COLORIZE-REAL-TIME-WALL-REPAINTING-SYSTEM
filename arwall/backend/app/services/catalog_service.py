"""
Paint catalog: map any HEX (from the colour picker or the AI assistant) to the
closest *purchasable* paint, so every recommendation ends in a real product code.

Algorithm: SQL box pre-filter in CIELAB (uses ix_paint_colors_lab), then exact
CIEDE2000 ranking in Python. A ±12 box comfortably contains every colour within
ΔE2000 ≈ 5; if nothing is found we widen once. For catalogs > ~1M rows, swap
the box query for a pgvector / cube KNN index on (L, a, b).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PaintBrand, PaintColor
from app.utils.color import delta_e_2000, hex_to_lab, normalize_hex


@dataclass(frozen=True)
class ColorMatch:
    paint: PaintColor
    delta_e: float


def visible_brands_clause(organization_id: uuid.UUID | None):
    """Public global brands + the caller's organization's private catalog."""
    clauses = [(PaintBrand.organization_id.is_(None)) & (PaintBrand.is_public.is_(True))]
    if organization_id:
        clauses.append(PaintBrand.organization_id == organization_id)
    return or_(*clauses)


async def nearest_paints(db: AsyncSession, hex_color: str, *, organization_id: uuid.UUID | None,
                         brand_ids: list[uuid.UUID] | None = None, limit: int = 5) -> list[ColorMatch]:
    target = hex_to_lab(normalize_hex(hex_color))
    L, a, b = target

    for radius in (12.0, 30.0):
        stmt = (
            select(PaintColor)
            .join(PaintBrand, PaintBrand.id == PaintColor.brand_id)
            .where(
                visible_brands_clause(organization_id),
                PaintColor.is_discontinued.is_(False),
                PaintColor.lab_l.between(L - radius, L + radius),
                PaintColor.lab_a.between(a - radius, a + radius),
                PaintColor.lab_b.between(b - radius, b + radius),
            )
            .limit(2000)
        )
        if brand_ids:
            stmt = stmt.where(PaintColor.brand_id.in_(brand_ids))
        candidates = (await db.execute(stmt)).scalars().unique().all()
        if candidates:
            ranked = sorted(
                (ColorMatch(p, delta_e_2000(target, (p.lab_l, p.lab_a, p.lab_b))) for p in candidates),
                key=lambda m: m.delta_e,
            )
            return ranked[:limit]
    return []
