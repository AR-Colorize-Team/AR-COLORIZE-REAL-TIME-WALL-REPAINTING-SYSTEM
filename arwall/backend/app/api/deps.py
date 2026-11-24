"""
FastAPI dependencies: authentication, RBAC, entitlements, resource ownership.

Ownership rule of thumb: a resource the caller may not see returns 404, not 403,
so IDs of other tenants' projects can't be probed.
"""
from __future__ import annotations

import hmac
import uuid
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import Forbidden, NotFound, Unauthorized
from app.core.rbac import Perm, has_permission
from app.core.security import TokenError, decode_access_token
from app.db.models import Palette, Project, ProjectAsset, User
from app.db.session import get_db
from app.services.entitlements import Entitlements, get_entitlements

DB = Annotated[AsyncSession, Depends(get_db)]
_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    db: DB,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise Unauthorized()
    try:
        claims = decode_access_token(creds.credentials)
        user_id = uuid.UUID(claims["sub"])
    except (TokenError, ValueError, KeyError) as exc:
        raise Unauthorized("Invalid or expired token") from exc

    # One indexed PK lookup per request. Gives instant revocation (token_version),
    # role changes and account deletion — worth far more than pure statelessness.
    # At high RPS, cache (user_id → token_version, role, deleted) in Redis for ~30 s.
    user = await db.get(User, user_id)
    if user is None or user.deleted_at is not None or user.token_version != claims.get("ver"):
        raise Unauthorized("Session is no longer valid")

    request.state.user_id = user.id   # for structured logs
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require(perm: Perm):
    """Dependency factory: `Depends(require(Perm.USERS_MANAGE))`."""
    async def _check(user: CurrentUser) -> User:
        if not has_permission(user.role, perm):
            raise Forbidden(f"Missing permission: {perm.value}")
        return user
    return _check


async def current_entitlements(user: CurrentUser, db: DB) -> Entitlements:
    return await get_entitlements(db, user.id)


CurrentEntitlements = Annotated[Entitlements, Depends(current_entitlements)]


# --------------------------------------------------------------------------- #
# Ownership-aware loaders
# --------------------------------------------------------------------------- #
def _can_read_project(user: User, project: Project) -> bool:
    if project.owner_user_id == user.id:
        return True
    if has_permission(user.role, Perm.PROJECTS_READ_ANY):
        return True
    return (has_permission(user.role, Perm.PROJECTS_READ_ORG)
            and project.organization_id is not None
            and project.organization_id == user.organization_id)


async def load_project(db: AsyncSession, user: User, project_id: uuid.UUID, *,
                       write: bool = False, lock: bool = False) -> Project:
    stmt = select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
    if lock:
        stmt = stmt.with_for_update()
    project = (await db.execute(stmt)).scalar_one_or_none()
    if project is None or not _can_read_project(user, project):
        raise NotFound("Project not found")
    # Writes are owner-only. Admins/support/org admins get read access, not edit.
    if write and project.owner_user_id != user.id:
        raise Forbidden("Only the project owner can modify it")
    return project


async def load_asset(db: AsyncSession, user: User, asset_id: uuid.UUID, *,
                     write: bool = False) -> tuple[ProjectAsset, Project]:
    asset = await db.get(ProjectAsset, asset_id)
    if asset is None:
        raise NotFound("Asset not found")
    project = await load_project(db, user, asset.project_id, write=write)
    return asset, project


async def load_palette(db: AsyncSession, user: User, palette_id: uuid.UUID, *,
                       write: bool = False) -> Palette:
    palette = (await db.execute(
        select(Palette).where(Palette.id == palette_id, Palette.deleted_at.is_(None))
    )).scalar_one_or_none()
    if palette is None:
        raise NotFound("Palette not found")
    is_owner = palette.owner_user_id == user.id
    visible = (is_owner or palette.is_public or palette.source in ("system", "brand")
               or (palette.organization_id and palette.organization_id == user.organization_id))
    if not visible:
        raise NotFound("Palette not found")
    if write and not is_owner:
        raise Forbidden("Only the palette owner can modify it")
    return palette


# --------------------------------------------------------------------------- #
# Internal service-to-service auth (AI workers → API)
# --------------------------------------------------------------------------- #
async def require_internal_service(
    x_internal_token: Annotated[str | None, Header()] = None,
) -> None:
    """
    Shared-secret header for worker callbacks, compared in constant time.
    The /internal routes are additionally blocked at the ALB/WAF from the public
    internet and only reachable from the worker security group.
    """
    expected = get_settings().internal_service_token
    if expected is None or x_internal_token is None or not hmac.compare_digest(
        x_internal_token.encode(), expected.get_secret_value().encode()
    ):
        raise Unauthorized("Invalid service credentials")
