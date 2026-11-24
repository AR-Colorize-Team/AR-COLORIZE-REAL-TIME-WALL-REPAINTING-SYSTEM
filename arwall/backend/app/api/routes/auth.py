"""
/auth — registration, login, token refresh, logout.

Rate limiting: in addition to per-account lockout (DB), these routes have an
AWS WAF rate-based rule (e.g. 20 req / 5 min / IP on /v1/auth/*).
"""
from __future__ import annotations

from fastapi import APIRouter, Request, status

from app.api.deps import DB, CurrentUser
from app.api.schemas import AuthOut, LoginIn, RefreshIn, RegisterIn, TokenOut, UserOut
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _tokens(pair: auth_service.TokenPair) -> TokenOut:
    return TokenOut(access_token=pair.access_token, refresh_token=pair.refresh_token,
                    expires_in=pair.expires_in, token_type=pair.token_type)


@router.post("/register", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn, request: Request, db: DB):
    user, pair = await auth_service.register(
        db, email=body.email, password=body.password, full_name=body.full_name,
        device_label=body.device_label, request=request,
    )
    # TODO(roadmap): send verification email via SES with a signed, single-use link.
    return AuthOut(user=UserOut.from_user(user), tokens=_tokens(pair))


@router.post("/login", response_model=AuthOut)
async def login(body: LoginIn, request: Request, db: DB):
    user, pair = await auth_service.login(
        db, email=body.email, password=body.password, device_label=body.device_label, request=request,
    )
    return AuthOut(user=UserOut.from_user(user), tokens=_tokens(pair))


@router.post("/refresh", response_model=TokenOut)
async def refresh(body: RefreshIn, request: Request, db: DB):
    """Rotate: the presented refresh token is invalidated and a new pair returned."""
    return _tokens(await auth_service.rotate_refresh_token(db, body.refresh_token, request))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshIn, user: CurrentUser, db: DB):
    """Log out this device. The short-lived access token simply expires."""
    await auth_service.revoke_refresh_family(db, body.refresh_token, user.id)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(user: CurrentUser, request: Request, db: DB):
    """Log out every device immediately (access tokens included, via token_version)."""
    await auth_service.revoke_all_sessions(db, user, request)
