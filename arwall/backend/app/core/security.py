"""
Cryptographic primitives: password hashing, JWT access tokens, opaque refresh tokens.

Token model
-----------
* Access token  — short-lived (15 min) JWT, sent as `Authorization: Bearer`.
                  Carries `sub`, `role`, `org`, `ver` (token_version). Stateless check,
                  plus a cheap `ver` comparison against the user row so that
                  "log out everywhere" / role changes take effect immediately.
* Refresh token — long-lived opaque random string. Only its SHA-256 is stored.
                  Rotated on every use; reuse of an already-rotated token revokes
                  the whole family (stolen-token detection).
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import get_settings

# OWASP 2024+ guidance for Argon2id: m=19 MiB t=2 p=1 minimum. We go higher.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)

# Pre-computed hash used to equalise timing when the email does not exist,
# so login response time doesn't reveal which emails are registered.
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-real-password")


class TokenError(Exception):
    """Raised for any invalid/expired/malformed token."""


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str | None) -> bool:
    try:
        return _hasher.verify(hashed or _DUMMY_HASH, plain) and hashed is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(hashed: str) -> bool:
    return _hasher.check_needs_rehash(hashed)


def validate_password_strength(plain: str) -> str:
    """Length-first policy (NIST SP 800-63B): ≥10 chars, not trivially weak."""
    if len(plain) < 10:
        raise ValueError("Password must be at least 10 characters")
    if len(plain) > 128:
        raise ValueError("Password must be at most 128 characters")
    if len(set(plain)) < 4:
        raise ValueError("Password is too repetitive")
    return plain


# --------------------------------------------------------------------------- #
# Access tokens (JWT)
# --------------------------------------------------------------------------- #
def create_access_token(
    *,
    user_id: uuid.UUID,
    role: str,
    token_version: int,
    organization_id: uuid.UUID | None = None,
) -> tuple[str, int]:
    """Return (jwt, expires_in_seconds)."""
    s = get_settings()
    now = datetime.now(timezone.utc)
    claims: dict[str, Any] = {
        "iss": s.jwt_issuer,
        "aud": s.jwt_audience,
        "sub": str(user_id),
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(seconds=s.access_token_ttl_seconds),
        "jti": uuid.uuid4().hex,
        "typ": "access",
        "role": role,
        "ver": token_version,
    }
    if organization_id:
        claims["org"] = str(organization_id)
    token = jwt.encode(claims, s.jwt_signing_key, algorithm=s.jwt_algorithm)
    return token, s.access_token_ttl_seconds


def decode_access_token(token: str) -> dict[str, Any]:
    s = get_settings()
    try:
        claims = jwt.decode(
            token,
            s.jwt_verify_key,
            algorithms=[s.jwt_algorithm],          # pin algorithm: blocks alg-confusion attacks
            audience=s.jwt_audience,
            issuer=s.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "jti", "typ"]},
            leeway=10,
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if claims.get("typ") != "access":
        raise TokenError("wrong token type")
    return claims


# --------------------------------------------------------------------------- #
# Refresh tokens (opaque)
# --------------------------------------------------------------------------- #
def generate_refresh_token() -> tuple[str, str]:
    """Return (plaintext_token_for_client, sha256_hex_for_db)."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
