"""
Stripe billing.

Principles
* Stripe is the source of truth. Our `subscriptions` table is a projection kept
  current by webhooks; we never grant access based on a client-side "success" page.
* Webhooks are verified (signature + timestamp tolerance), recorded in
  `stripe_events` for idempotency, then processed. Re-delivery is a no-op.
* Plan changes / cancellations / payment methods go through the Stripe Customer
  Portal — we don't re-implement that UI.
* Mobile store rules: digital subscriptions sold *inside* an iOS/Android app
  generally must use Apple IAP / Google Play Billing. Stripe Checkout is used for
  web sign-up, Enterprise invoicing, and regions/programs where external
  purchase links are permitted. Plan for RevenueCat (or StoreKit/Play Billing
  server notifications) feeding this same `subscriptions` table.

The stripe-python client is synchronous; calls are pushed to a thread pool.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import stripe
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.errors import AppError, Conflict, NotFound, ServiceUnavailable
from app.db.models import Plan, StripeEvent, Subscription, User
from app.services.audit import audit

log = logging.getLogger("arwall.billing")

LIVE_STATUSES = ("trialing", "active", "past_due", "incomplete", "paused")


# Exported at top level in stripe-python ≥ 8, under stripe.error in older releases.
_SignatureError = getattr(stripe, "SignatureVerificationError", None) or stripe.error.SignatureVerificationError


class InvalidWebhook(AppError):
    status_code, code, title = 400, "invalid_webhook", "Invalid webhook"


def _key() -> str:
    s = get_settings()
    if not s.stripe_secret_key:
        raise ServiceUnavailable("Billing is not configured")
    return s.stripe_secret_key.get_secret_value()


def _ts(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, tz=timezone.utc) if value else None


# --------------------------------------------------------------------------- #
# Checkout & portal
# --------------------------------------------------------------------------- #
async def ensure_customer(db: AsyncSession, user: User) -> str:
    if user.stripe_customer_id:
        return user.stripe_customer_id
    customer = await run_in_threadpool(
        stripe.Customer.create,
        api_key=_key(),
        email=user.email,
        name=user.full_name or None,
        metadata={"user_id": str(user.id)},
        # Idempotency key: a retried request can't create a duplicate customer.
        idempotency_key=f"customer-create-{user.id}",
    )
    user.stripe_customer_id = customer["id"]
    await db.commit()
    return customer["id"]


async def create_checkout_session(db: AsyncSession, user: User, plan_id: uuid.UUID) -> str:
    plan = await db.get(Plan, plan_id)
    if plan is None or not plan.is_active or not plan.stripe_price_id:
        raise NotFound("Plan not found or not purchasable")

    live = (await db.execute(
        select(Subscription.id).where(Subscription.user_id == user.id,
                                      Subscription.status.in_(LIVE_STATUSES))
    )).first()
    if live:
        # Upgrades/downgrades on an existing sub go through the portal (proration handled by Stripe).
        raise Conflict("You already have an active subscription; manage it in the billing portal",
                       extra={"action": "open_billing_portal"})

    s = get_settings()
    customer_id = await ensure_customer(db, user)
    session = await run_in_threadpool(
        stripe.checkout.Session.create,
        api_key=_key(),
        mode="subscription",
        customer=customer_id,
        client_reference_id=str(user.id),
        line_items=[{"price": plan.stripe_price_id, "quantity": 1}],
        subscription_data={"metadata": {"user_id": str(user.id), "plan_id": str(plan.id)}},
        allow_promotion_codes=True,
        automatic_tax={"enabled": True},          # Stripe Tax: GST/VAT/sales tax
        customer_update={"address": "auto"},
        success_url=s.checkout_success_url,
        cancel_url=s.checkout_cancel_url,
    )
    await audit(db, "billing.checkout_started", actor_id=user.id, target_type="plan", target_id=plan.id)
    await db.commit()
    return session["url"]


async def create_portal_session(db: AsyncSession, user: User) -> str:
    if not user.stripe_customer_id:
        raise NotFound("No billing account yet")
    portal = await run_in_threadpool(
        stripe.billing_portal.Session.create,
        api_key=_key(),
        customer=user.stripe_customer_id,
        return_url=get_settings().billing_portal_return_url,
    )
    return portal["url"]


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #
def verify_webhook(payload: bytes, signature: str | None) -> dict[str, Any]:
    s = get_settings()
    if not s.stripe_webhook_secret or not signature:
        raise InvalidWebhook("Missing signature")
    try:
        event = stripe.Webhook.construct_event(
            payload, signature, s.stripe_webhook_secret.get_secret_value(), tolerance=300
        )
    except (ValueError, _SignatureError) as exc:
        raise InvalidWebhook(str(exc)) from exc
    # Signature verified → the raw payload *is* the event. Parsing it ourselves gives
    # plain dicts regardless of stripe-python's StripeObject internals.
    import json
    return json.loads(payload)


async def handle_webhook_event(db: AsyncSession, event: dict[str, Any]) -> str:
    """Idempotent processing. Returns 'processed' | 'duplicate' | 'ignored'."""
    inserted = (await db.execute(
        text("INSERT INTO stripe_events (id, type, payload) VALUES (:id, :type, CAST(:payload AS jsonb)) "
             "ON CONFLICT (id) DO NOTHING RETURNING id"),
        {"id": event["id"], "type": event["type"], "payload": _json(event)},
    )).first()
    if inserted is None:
        existing = await db.get(StripeEvent, event["id"])
        if existing and existing.processed_at:
            return "duplicate"
        # Previously received but failed — fall through and retry.

    handler = _HANDLERS.get(event["type"])
    if handler is None:
        await _mark(db, event["id"])
        await db.commit()
        return "ignored"

    try:
        await handler(db, event["data"]["object"])
        await _mark(db, event["id"])
        await db.commit()
        return "processed"
    except Exception as exc:
        await db.rollback()
        await db.execute(text("UPDATE stripe_events SET error = :e WHERE id = :id"),
                         {"e": repr(exc)[:2000], "id": event["id"]})
        await db.commit()
        raise  # 500 → Stripe retries with exponential backoff for up to 3 days


async def _mark(db: AsyncSession, event_id: str) -> None:
    await db.execute(text("UPDATE stripe_events SET processed_at = now(), error = NULL WHERE id = :id"),
                     {"id": event_id})


def _json(obj: Any) -> str:
    import json
    return json.dumps(obj, default=str)


async def _fetch_subscription(sub_id: str) -> dict[str, Any]:
    sub = await run_in_threadpool(stripe.Subscription.retrieve, sub_id, api_key=_key())
    return _plain(sub)


def _plain(obj: Any) -> dict[str, Any]:
    """StripeObject → plain nested dict (method name differs across stripe-python majors)."""
    import json
    for name in ("to_dict_recursive", "to_dict"):
        if hasattr(obj, name):
            return getattr(obj, name)()
    return json.loads(str(obj))


async def _on_checkout_completed(db: AsyncSession, session: dict[str, Any]) -> None:
    if session.get("mode") != "subscription" or not session.get("subscription"):
        return
    await _upsert_subscription(db, await _fetch_subscription(session["subscription"]))


async def _on_subscription_changed(db: AsyncSession, sub: dict[str, Any]) -> None:
    # Webhooks can arrive out of order (an old "updated" after "deleted").
    # Re-reading the current object from Stripe makes every event converge on
    # the latest state, so ordering no longer matters.
    await _upsert_subscription(db, await _fetch_subscription(sub["id"]))


async def _on_payment_failed(db: AsyncSession, invoice: dict[str, Any]) -> None:
    # Status flips to past_due via customer.subscription.updated; here we just
    # notify (push + email) so the user can fix their card during dunning.
    user = await _user_for_customer(db, invoice.get("customer"))
    if user:
        await audit(db, "billing.payment_failed", actor_id=user.id, target_type="invoice",
                    target_id=invoice.get("id"), amount_due=invoice.get("amount_due"))
        # TODO: enqueue notification (SES email + FCM push).


_HANDLERS = {
    "checkout.session.completed": _on_checkout_completed,
    "customer.subscription.created": _on_subscription_changed,
    "customer.subscription.updated": _on_subscription_changed,
    "customer.subscription.deleted": _on_subscription_changed,
    "customer.subscription.paused": _on_subscription_changed,
    "customer.subscription.resumed": _on_subscription_changed,
    "invoice.payment_failed": _on_payment_failed,
}


async def _user_for_customer(db: AsyncSession, customer_id: str | None) -> User | None:
    if not customer_id:
        return None
    return (await db.execute(select(User).where(User.stripe_customer_id == customer_id))).scalar_one_or_none()


async def _upsert_subscription(db: AsyncSession, sub: dict[str, Any]) -> None:
    """
    Project a Stripe Subscription object onto our table.

    Note: since Stripe API version 2025-03-31 ("basil"), current_period_start/end
    live on each subscription *item*, not the subscription. We read the item
    first and fall back to the top level for older pinned API versions.
    """
    items = (sub.get("items") or {}).get("data") or []
    if not items:
        log.warning("subscription_without_items", extra={"sub": sub.get("id")})
        return
    item = items[0]
    price_id = item["price"]["id"]
    plan = (await db.execute(select(Plan).where(Plan.stripe_price_id == price_id))).scalar_one_or_none()
    if plan is None:
        raise RuntimeError(f"Unknown Stripe price {price_id}; add it to the plans table")

    meta = sub.get("metadata") or {}
    user_id: uuid.UUID | None = uuid.UUID(meta["user_id"]) if meta.get("user_id") else None
    org_id: uuid.UUID | None = uuid.UUID(meta["organization_id"]) if meta.get("organization_id") else None
    if not user_id and not org_id:
        user = await _user_for_customer(db, sub.get("customer"))
        if user is None:
            raise RuntimeError(f"Cannot resolve owner for subscription {sub.get('id')}")
        user_id = user.id

    period_start = item.get("current_period_start") or sub.get("current_period_start")
    period_end = item.get("current_period_end") or sub.get("current_period_end")

    await db.execute(text("""
        INSERT INTO subscriptions (user_id, organization_id, plan_id, stripe_subscription_id, status,
                                   seats, current_period_start, current_period_end,
                                   cancel_at_period_end, trial_end, canceled_at)
        VALUES (:user_id, :org_id, :plan_id, :sid, CAST(:status AS subscription_status), :seats,
                :ps, :pe, :cape, :trial_end, :canceled_at)
        ON CONFLICT (stripe_subscription_id) DO UPDATE SET
            plan_id = EXCLUDED.plan_id,
            status = EXCLUDED.status,
            seats = EXCLUDED.seats,
            current_period_start = EXCLUDED.current_period_start,
            current_period_end = EXCLUDED.current_period_end,
            cancel_at_period_end = EXCLUDED.cancel_at_period_end,
            trial_end = EXCLUDED.trial_end,
            canceled_at = EXCLUDED.canceled_at
    """), {
        "user_id": None if org_id else user_id,
        "org_id": org_id,
        "plan_id": plan.id,
        "sid": sub["id"],
        "status": sub["status"],
        "seats": int(item.get("quantity") or 1),
        "ps": _ts(period_start),
        "pe": _ts(period_end),
        "cape": bool(sub.get("cancel_at_period_end")),
        "trial_end": _ts(sub.get("trial_end")),
        "canceled_at": _ts(sub.get("canceled_at")),
    })
    await audit(db, "billing.subscription_synced", actor_id=None if org_id else user_id,
                target_type="subscription", target_id=sub["id"], status=sub["status"], tier=plan.tier)
