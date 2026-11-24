"""
Keyset (cursor) pagination. Unlike OFFSET, cost stays constant on page 500 and
results don't shift when rows are inserted while the user scrolls.
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime

from app.core.errors import AppError


class BadCursor(AppError):
    status_code, code, title = 400, "bad_cursor", "Invalid pagination cursor"


def encode_cursor(ts: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps([ts.isoformat(), str(row_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        ts, rid = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromisoformat(ts), uuid.UUID(rid)
    except Exception as exc:  # noqa: BLE001
        raise BadCursor() from exc
