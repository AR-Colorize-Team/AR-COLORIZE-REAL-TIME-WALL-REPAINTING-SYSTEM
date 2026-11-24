"""
Integration-test fixtures. Requires a Postgres database with db/001_schema.sql applied:

    createdb arwall_test && psql -d arwall_test -f ../db/001_schema.sql
    TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost/arwall_test pytest
"""
import os

import pytest

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", os.getenv(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/arwall_test"))
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-0123456789")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "internal-test-token")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")          # presigning is local; no AWS call
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

_TABLES = ("audit_logs, usage_counters, ai_jobs, project_walls, project_assets, projects, "
           "palette_colors, palettes, paint_colors, paint_brands, stripe_events, subscriptions, "
           "refresh_tokens, user_identities, users, organizations")


@pytest.fixture
async def client():
    import httpx
    from sqlalchemy import text

    from app.db.session import engine
    from app.main import app

    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def auth(client):
    """Registers a user and returns (headers, tokens)."""
    r = await client.post("/v1/auth/register",
                          json={"email": "alice@example.com", "password": "correct-horse-battery"})
    assert r.status_code == 201, r.text
    tokens = r.json()["tokens"]
    return {"Authorization": f"Bearer {tokens['access_token']}"}, tokens
