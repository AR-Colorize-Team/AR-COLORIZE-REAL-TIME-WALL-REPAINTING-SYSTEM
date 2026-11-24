"""/me — the authenticated user's profile, entitlements and account lifecycle."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request, status
from sqlalchemy import update

from app.api.deps import DB, CurrentEntitlements, CurrentUser
from app.api.schemas import ChangePasswordIn, EntitlementsOut, UserOut, UserUpdateIn
from app.core.errors import Unauthorized
from app.core.security import hash_password, verify_password
from app.db.models import Project
from app.services import auth_service
from app.services.audit import audit
from app.services.entitlements import usage_summary

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=UserOut)
async def get_me(user: CurrentUser):
    return UserOut.from_user(user)


@router.patch("", response_model=UserOut)
async def update_me(body: UserUpdateIn, user: CurrentUser, db: DB):
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    await db.commit()
    await db.refresh(user)
    return UserOut.from_user(user)


@router.get("/entitlements", response_model=EntitlementsOut)
async def my_entitlements(user: CurrentUser, ent: CurrentEntitlements, db: DB):
    """Drives the paywall UI: what the plan allows and how much is used this month."""
    return EntitlementsOut(tier=ent.tier, limits=ent.limits,
                           usage_this_month=await usage_summary(db, user.id),
                           subscription_status=ent.subscription_status,
                           current_period_end=ent.current_period_end)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(body: ChangePasswordIn, user: CurrentUser, request: Request, db: DB):
    if not verify_password(body.current_password, user.password_hash):
        raise Unauthorized("Current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    await audit(db, "auth.password_changed", actor_id=user.id, request=request)
    # Revokes every other session too (commits).
    await auth_service.revoke_all_sessions(db, user, request)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(user: CurrentUser, request: Request, db: DB):
    """
    GDPR/DPDP-style deletion: soft-delete now (immediate lock-out), hard purge of
    rows + S3 objects by a scheduled job after a 30-day grace period. Active
    Stripe subscriptions are cancelled by that job before purge.
    """
    now = datetime.now(timezone.utc)
    user.deleted_at = now
    await db.execute(update(Project).where(Project.owner_user_id == user.id, Project.deleted_at.is_(None))
                     .values(deleted_at=now))
    await audit(db, "account.deleted", actor_id=user.id, request=request)
    await auth_service.revoke_all_sessions(db, user, request)
