"""/admin — platform administration (RBAC-gated)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from app.api.deps import DB, require
from app.api.schemas import RoleUpdateIn, UserOut
from app.core.errors import Forbidden, NotFound
from app.core.rbac import Perm
from app.db.models import Organization, User
from app.services.audit import audit

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=list[UserOut])
async def search_users(db: DB, _: User = Depends(require(Perm.USERS_READ_ANY)),
                       email: str | None = Query(None, min_length=3, max_length=254),
                       limit: int = Query(50, ge=1, le=200)):
    stmt = select(User).where(User.deleted_at.is_(None)).order_by(User.created_at.desc()).limit(limit)
    if email:
        stmt = stmt.where(User.email.ilike(f"{email}%"))
    return [UserOut.from_user(u) for u in (await db.execute(stmt)).scalars().all()]


@router.patch("/users/{user_id}/role", response_model=UserOut)
async def set_role(user_id: uuid.UUID, body: RoleUpdateIn, request: Request, db: DB,
                   actor: User = Depends(require(Perm.USERS_MANAGE))):
    target = await db.get(User, user_id)
    if target is None or target.deleted_at is not None:
        raise NotFound("User not found")
    if target.id == actor.id:
        raise Forbidden("You cannot change your own role")   # prevents accidental lock-out
    if body.organization_id and await db.get(Organization, body.organization_id) is None:
        raise NotFound("Organization not found")

    old_role = target.role
    target.role = body.role
    if body.role in ("org_member", "org_admin"):
        org_id = body.organization_id or target.organization_id
        if org_id is None:
            raise NotFound("Organization roles require organization_id")
        target.organization_id = org_id
    target.token_version += 1          # new role takes effect on the next request, not in 15 min
    await audit(db, "admin.role_changed", actor_id=actor.id, target_type="user", target_id=target.id,
                request=request, old_role=old_role, new_role=body.role)
    await db.commit()
    await db.refresh(target)
    return UserOut.from_user(target)
