"""
Authentication business logic (kept out of the route layer so it is unit-testable
and reusable by the Apple/Google sign-in flows).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AccountLocked, Conflict, Unauthorized
from app.core.security import (create_access_token, generate_refresh_token, hash_password, hash_token,
                               password_needs_rehash, verify_password)
from app.db.models import RefreshToken, User
from app.services.audit import audit, client_ip


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str = "Bearer"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def issue_tokens(db: AsyncSession, user: User, *, family_id: uuid.UUID | None = None,
                       device_label: str | None = None, request: Request | None = None) -> tuple[TokenPair, RefreshToken]:
    s = get_settings()
    access, ttl = create_access_token(user_id=user.id, role=user.role,
                                      token_version=user.token_version,
                                      organization_id=user.organization_id)
    raw, digest = generate_refresh_token()
    rt = RefreshToken(
        user_id=user.id,
        family_id=family_id or uuid.uuid4(),
        token_hash=digest,
        device_label=(device_label or "")[:100] or None,
        ip_address=client_ip(request),
        expires_at=_now() + timedelta(days=s.refresh_token_ttl_days),
    )
    db.add(rt)
    await db.flush()
    return TokenPair(access_token=access, refresh_token=raw, expires_in=ttl), rt


async def register(db: AsyncSession, *, email: str, password: str, full_name: str | None,
                   device_label: str | None, request: Request) -> tuple[User, TokenPair]:
    user = User(email=email.strip().lower(), password_hash=hash_password(password), full_name=full_name)
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        # Note: for stricter anti-enumeration, return 202 and email the owner instead.
        raise Conflict("An account with this email already exists") from exc
    await db.refresh(user)
    pair, _ = await issue_tokens(db, user, device_label=device_label, request=request)
    await audit(db, "auth.register", actor_id=user.id, target_type="user", target_id=user.id, request=request)
    await db.commit()
    return user, pair


async def login(db: AsyncSession, *, email: str, password: str, device_label: str | None,
                request: Request) -> tuple[User, TokenPair]:
    s = get_settings()
    user = (await db.execute(
        select(User).where(User.email == email.strip().lower(), User.deleted_at.is_(None)).with_for_update()
    )).scalar_one_or_none()

    if user and user.locked_until and user.locked_until > _now():
        raise AccountLocked("Too many failed attempts. Try again later.",
                            extra={"retry_after": int((user.locked_until - _now()).total_seconds())})

    # verify_password runs a real Argon2 verify even when user is None (timing-safe).
    if not verify_password(password, user.password_hash if user else None):
        if user:
            user.failed_login_count += 1
            if user.failed_login_count >= s.max_failed_logins:
                user.locked_until = _now() + timedelta(minutes=s.lockout_minutes)
                user.failed_login_count = 0
                await audit(db, "auth.locked", actor_id=user.id, request=request)
            await audit(db, "auth.login_failed", actor_id=user.id, request=request)
            await db.commit()
        raise Unauthorized("Invalid email or password")

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = _now()
    if password_needs_rehash(user.password_hash):  # transparent upgrade when Argon2 params change
        user.password_hash = hash_password(password)
    pair, _ = await issue_tokens(db, user, device_label=device_label, request=request)
    await audit(db, "auth.login", actor_id=user.id, request=request)
    await db.commit()
    return user, pair


async def rotate_refresh_token(db: AsyncSession, raw_token: str, request: Request) -> TokenPair:
    """
    Exchange a refresh token for a new pair. Row is locked FOR UPDATE so two
    concurrent refreshes with the same token can't both succeed.
    """
    current = (await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token)).with_for_update()
    )).scalar_one_or_none()
    if current is None:
        raise Unauthorized("Invalid refresh token")

    if current.revoked_at is not None:
        # Token reuse ⇒ likely theft. Kill the entire session family.
        await db.execute(update(RefreshToken)
                         .where(RefreshToken.family_id == current.family_id, RefreshToken.revoked_at.is_(None))
                         .values(revoked_at=_now()))
        await audit(db, "auth.refresh_reuse_detected", actor_id=current.user_id,
                    target_id=current.family_id, request=request)
        await db.commit()
        raise Unauthorized("Refresh token reuse detected; please sign in again")

    if current.expires_at <= _now():
        raise Unauthorized("Refresh token expired")

    user = await db.get(User, current.user_id)
    if user is None or user.deleted_at is not None:
        raise Unauthorized("Account unavailable")

    pair, new_rt = await issue_tokens(db, user, family_id=current.family_id,
                                      device_label=current.device_label, request=request)
    current.revoked_at = _now()
    current.replaced_by = new_rt.id
    await db.commit()
    return pair


async def revoke_refresh_family(db: AsyncSession, raw_token: str, user_id: uuid.UUID) -> None:
    """Log out this device (the whole rotation chain of the presented token)."""
    rt = (await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token),
                                   RefreshToken.user_id == user_id)
    )).scalar_one_or_none()
    if rt:
        await db.execute(update(RefreshToken)
                         .where(RefreshToken.family_id == rt.family_id, RefreshToken.revoked_at.is_(None))
                         .values(revoked_at=_now()))
    await db.commit()


async def revoke_all_sessions(db: AsyncSession, user: User, request: Request | None = None) -> None:
    """Log out everywhere: bump token_version (kills access JWTs) + revoke all refresh tokens."""
    user.token_version += 1
    await db.execute(update(RefreshToken)
                     .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
                     .values(revoked_at=_now()))
    await audit(db, "auth.logout_all", actor_id=user.id, request=request)
    await db.commit()
