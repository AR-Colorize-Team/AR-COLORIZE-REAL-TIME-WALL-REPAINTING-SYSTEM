"""
Entitlements & quota enforcement.

* Effective plan comes from the `v_user_entitlements` view (org sub > personal sub > free).
* Metered quotas use a single atomic UPSERT so two concurrent requests can never
  both slip past the limit (no read-then-write race).
* A limit value of -1 means unlimited.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import FeatureNotInPlan, QuotaExceeded

UNLIMITED = -1

# metric name in usage_counters → key in plans.limits
METRIC_LIMIT_KEYS = {
    "ai_recommendations": "ai_recommendations_per_month",
    "segmentations": "segmentations_per_month",
}


@dataclass(frozen=True)
class Entitlements:
    tier: str
    limits: dict[str, Any]
    subscription_id: uuid.UUID | None
    subscription_status: str | None
    current_period_end: datetime | None

    def limit(self, key: str) -> int:
        return int(self.limits.get(key, 0))

    def has_feature(self, key: str) -> bool:
        return bool(self.limits.get(key, False))


def current_period_start(now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    return date(now.year, now.month, 1)


async def get_entitlements(db: AsyncSession, user_id: uuid.UUID) -> Entitlements:
    row = (await db.execute(
        text("SELECT tier, limits, subscription_id, subscription_status, current_period_end "
             "FROM v_user_entitlements WHERE user_id = :uid"),
        {"uid": user_id},
    )).mappings().one()
    return Entitlements(**row)


def require_feature(ent: Entitlements, feature: str) -> None:
    if not ent.has_feature(feature):
        raise FeatureNotInPlan(f"'{feature}' is not included in the {ent.tier} plan",
                               extra={"feature": feature, "tier": ent.tier})


async def consume_quota(db: AsyncSession, user_id: uuid.UUID, ent: Entitlements,
                        metric: str, amount: int = 1) -> int:
    """
    Atomically increment a monthly counter if (and only if) it stays within the
    plan limit. Returns the new count. Raises QuotaExceeded otherwise.
    The caller commits — so if the request later fails, the increment rolls back.
    """
    limit = ent.limit(METRIC_LIMIT_KEYS[metric])
    if limit != UNLIMITED and amount > limit:
        raise QuotaExceeded(f"Monthly {metric} limit is {limit} on the {ent.tier} plan",
                            extra={"metric": metric, "limit": limit, "tier": ent.tier})

    row = (await db.execute(
        text("""
            INSERT INTO usage_counters (user_id, metric, period_start, count)
            VALUES (:uid, :metric, :period, :amt)
            ON CONFLICT (user_id, metric, period_start)
            DO UPDATE SET count = usage_counters.count + EXCLUDED.count
            WHERE :limit = -1 OR usage_counters.count + EXCLUDED.count <= :limit
            RETURNING count
        """),
        {"uid": user_id, "metric": metric, "period": current_period_start(),
         "amt": amount, "limit": limit},
    )).first()
    if row is None:
        raise QuotaExceeded(f"Monthly {metric} limit of {limit} reached on the {ent.tier} plan",
                            extra={"metric": metric, "limit": limit, "tier": ent.tier})
    return int(row[0])


async def refund_quota(db: AsyncSession, user_id: uuid.UUID, metric: str,
                       period: date, amount: int = 1) -> None:
    """Give a unit back when an AI job fails (users shouldn't pay for our errors)."""
    await db.execute(
        text("UPDATE usage_counters SET count = GREATEST(count - :amt, 0) "
             "WHERE user_id = :uid AND metric = :metric AND period_start = :period"),
        {"uid": user_id, "metric": metric, "period": period, "amt": amount},
    )


async def usage_summary(db: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    rows = (await db.execute(
        text("SELECT metric, count FROM usage_counters WHERE user_id = :uid AND period_start = :p"),
        {"uid": user_id, "p": current_period_start()},
    )).all()
    return {m: c for m, c in rows}


async def count_active_projects(db: AsyncSession, user_id: uuid.UUID) -> int:
    return (await db.execute(
        text("SELECT count(*) FROM projects WHERE owner_user_id = :uid AND deleted_at IS NULL"),
        {"uid": user_id},
    )).scalar_one()


async def storage_used_bytes(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Includes pending uploads so a burst of presign requests can't exceed the cap."""
    return (await db.execute(
        text("""
            SELECT COALESCE(SUM(COALESCE(a.size_bytes, 0)), 0)
            FROM project_assets a JOIN projects p ON p.id = a.project_id
            WHERE p.owner_user_id = :uid AND p.deleted_at IS NULL
              AND a.upload_status IN ('pending', 'uploaded', 'verified')
        """),
        {"uid": user_id},
    )).scalar_one()


async def enforce_project_limit(db: AsyncSession, user_id: uuid.UUID, ent: Entitlements) -> None:
    limit = ent.limit("max_projects")
    if limit == UNLIMITED:
        return
    # Serialise concurrent "create project" calls for this user until commit,
    # so two parallel requests can't both see count = limit - 1.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                     {"k": f"projects:{user_id}"})
    if await count_active_projects(db, user_id) >= limit:
        raise QuotaExceeded(f"The {ent.tier} plan allows {limit} projects",
                            extra={"metric": "projects", "limit": limit, "tier": ent.tier})


async def enforce_storage_limit(db: AsyncSession, user_id: uuid.UUID, ent: Entitlements,
                                incoming_bytes: int) -> None:
    limit_mb = ent.limit("max_storage_mb")
    if limit_mb == UNLIMITED:
        return
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                     {"k": f"storage:{user_id}"})
    used =await storage_used_bytes(db, user_id)
    if used + incoming_bytes > limit_mb * 1024 * 1024:
        raise QuotaExceeded(f"Storage limit of {limit_mb} MB reached on the {ent.tier} plan",
                            extra={"metric": "storage", "limit_mb": limit_mb,
                                   "used_mb": round(used / 1048576, 1), "tier": ent.tier})
