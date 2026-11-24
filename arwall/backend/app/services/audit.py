"""Append-only audit trail for security- and billing-relevant actions."""
from __future__ import annotations

import ipaddress
import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


def client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    # Behind ALB/CloudFront the real client is the left-most X-Forwarded-For entry.
    # Uvicorn must run with --proxy-headers --forwarded-allow-ips=<ALB CIDR>.
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


async def audit(db: AsyncSession, action: str, *, actor_id: uuid.UUID | None = None,
                target_type: str | None = None, target_id: Any = None,
                request: Request | None = None, **metadata: Any) -> None:
    """Adds to the current transaction; committed together with the business change."""
    db.add(AuditLog(
        actor_user_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        ip_address=client_ip(request),
        user_agent=(request.headers.get("user-agent", "")[:512] if request else None),
        metadata_=metadata,
    ))
