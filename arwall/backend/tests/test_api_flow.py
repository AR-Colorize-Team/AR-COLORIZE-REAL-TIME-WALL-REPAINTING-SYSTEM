"""End-to-end API flows against a real Postgres (see conftest.py)."""
import hashlib
import hmac
import json
import time

import pytest

pytestmark = pytest.mark.asyncio


async def test_register_login_and_me(client, auth):
    headers, _ = auth
    r = await client.get("/v1/me", headers=headers)
    assert r.status_code == 200 and r.json()["email"] == "alice@example.com"

    r = await client.post("/v1/auth/login", json={"email": "ALICE@example.com", "password": "wrong-password"})
    assert r.status_code == 401 and r.json()["code"] == "unauthorized"
    r = await client.post("/v1/auth/login", json={"email": "ALICE@example.com", "password": "correct-horse-battery"})
    assert r.status_code == 200


async def test_duplicate_registration_conflicts(client, auth):
    r = await client.post("/v1/auth/register", json={"email": "Alice@Example.com", "password": "another-good-pass"})
    assert r.status_code == 409


async def test_weak_password_rejected(client):
    r = await client.post("/v1/auth/register", json={"email": "b@example.com", "password": "short"})
    assert r.status_code == 422 and r.json()["code"] == "validation_error"


async def test_lockout_after_repeated_failures(client, auth):
    for _ in range(5):
        await client.post("/v1/auth/login", json={"email": "alice@example.com", "password": "nope-nope-nope"})
    r = await client.post("/v1/auth/login", json={"email": "alice@example.com", "password": "correct-horse-battery"})
    assert r.status_code == 423 and r.json()["code"] == "account_locked"


async def test_refresh_rotation_and_reuse_detection(client, auth):
    _, tokens = auth
    r1 = await client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r1.status_code == 200
    new_refresh = r1.json()["refresh_token"]

    # Replaying the old token = theft signal → whole family revoked
    replay = await client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert replay.status_code == 401
    r2 = await client.post("/v1/auth/refresh", json={"refresh_token": new_refresh})
    assert r2.status_code == 401, "rotated descendant must also be revoked"


async def test_logout_all_invalidates_access_tokens(client, auth):
    headers, _ = auth
    assert (await client.post("/v1/auth/logout-all", headers=headers)).status_code == 204
    assert (await client.get("/v1/me", headers=headers)).status_code == 401


async def test_free_plan_project_limit_returns_402(client, auth):
    headers, _ = auth
    for i in range(3):
        r = await client.post("/v1/projects", json={"name": f"Room {i}"}, headers=headers)
        assert r.status_code == 201, r.text
    r = await client.post("/v1/projects", json={"name": "One too many"}, headers=headers)
    assert r.status_code == 402 and r.json()["code"] == "quota_exceeded"

    page = (await client.get("/v1/projects?limit=2", headers=headers)).json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    page2 = (await client.get(f"/v1/projects?limit=2&cursor={page['next_cursor']}", headers=headers)).json()
    assert len(page2["items"]) == 1 and page2["next_cursor"] is None


async def test_cannot_see_other_users_project(client, auth):
    headers, _ = auth
    pid = (await client.post("/v1/projects", json={"name": "Mine"}, headers=headers)).json()["id"]
    r = await client.post("/v1/auth/register", json={"email": "eve@example.com", "password": "eve-password-123"})
    eve = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    assert (await client.get(f"/v1/projects/{pid}", headers=eve)).status_code == 404   # 404, not 403


async def test_walls_save_and_palette_hex_normalisation(client, auth):
    headers, _ = auth
    pid = (await client.post("/v1/projects", json={"name": "Bedroom"}, headers=headers)).json()["id"]
    walls = {"walls": [{"label": "North", "polygon": [[0, 0], [1, 0], [1, 1]], "applied_hex": "a3b18a"}]}
    r = await client.put(f"/v1/projects/{pid}/walls", json=walls, headers=headers)
    assert r.status_code == 200 and r.json()[0]["applied_hex"] == "#A3B18A"

    bad = {"walls": [{"polygon": [[0, 0], [2, 0], [1, 1]]}]}      # out of 0..1
    assert (await client.put(f"/v1/projects/{pid}/walls", json=bad, headers=headers)).status_code == 422


async def test_upload_ticket_is_presigned_post(client, auth):
    headers, _ = auth
    pid = (await client.post("/v1/projects", json={"name": "Kitchen"}, headers=headers)).json()["id"]
    r = await client.post(f"/v1/projects/{pid}/assets", headers=headers,
                          json={"kind": "room_photo", "content_type": "image/jpeg", "size_bytes": 3_000_000})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["asset"]["upload_status"] == "pending"
    assert "policy" in body["upload_fields"] and body["upload_fields"]["Content-Type"] == "image/jpeg"


async def test_rbac_blocks_admin_routes(client, auth):
    headers, _ = auth
    r = await client.get("/v1/admin/users", headers=headers)
    assert r.status_code == 403 and r.json()["code"] == "forbidden"


def _stripe_sig(payload: bytes, secret: str) -> str:
    ts = str(int(time.time()))
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


async def test_stripe_webhook_signature_and_idempotency(client, auth, monkeypatch):
    from app.services import billing_service

    headers, _ = auth
    me = (await client.get("/v1/me", headers=headers)).json()
    plans = (await client.get("/v1/billing/plans")).json()
    pro_price = "price_PRO_MONTHLY_REPLACE"

    async def fake_fetch(sub_id):
        return {"id": sub_id, "status": "active", "customer": "cus_1", "cancel_at_period_end": False,
                "metadata": {"user_id": me["id"]},
                "items": {"data": [{"price": {"id": pro_price}, "quantity": 1,
                                    "current_period_start": int(time.time()),
                                    "current_period_end": int(time.time()) + 2_592_000}]}}
    monkeypatch.setattr(billing_service, "_fetch_subscription", fake_fetch)

    event = {"id": "evt_test_1", "type": "customer.subscription.updated",
             "data": {"object": {"id": "sub_test_1", "object": "subscription"}}}
    payload = json.dumps(event).encode()

    bad = await client.post("/v1/billing/webhook", content=payload, headers={"Stripe-Signature": "t=1,v1=bad"})
    assert bad.status_code == 400

    sig = _stripe_sig(payload, "whsec_test_secret")
    r = await client.post("/v1/billing/webhook", content=payload, headers={"Stripe-Signature": sig})
    assert r.status_code == 200 and r.json()["result"] == "processed"
    r = await client.post("/v1/billing/webhook", content=payload, headers={"Stripe-Signature": sig})
    assert r.json()["result"] == "duplicate"

    ent = (await client.get("/v1/me/entitlements", headers=headers)).json()
    assert ent["tier"] == "pro" and any(p["tier"] == "pro" for p in plans)
