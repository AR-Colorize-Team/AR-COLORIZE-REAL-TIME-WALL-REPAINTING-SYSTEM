"""/billing — plans, Stripe Checkout, Customer Portal, webhooks."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select

from app.api.deps import DB, require
from app.api.schemas import CheckoutIn, PlanOut, RedirectOut
from app.core.rbac import Perm
from app.db.models import Plan, User
from app.services import billing_service

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(db: DB):
    """Public — powers the pricing screen before sign-up."""
    plans = (await db.execute(select(Plan).where(Plan.is_active.is_(True))
                              .order_by(Plan.price_cents))).scalars().all()
    return [PlanOut.model_validate(p) for p in plans]


@router.post("/checkout-session", response_model=RedirectOut)
async def checkout(body: CheckoutIn, db: DB, user: User = Depends(require(Perm.BILLING_MANAGE_OWN))):
    """Returns a Stripe-hosted Checkout URL; the app opens it in a browser tab."""
    return RedirectOut(url=await billing_service.create_checkout_session(db, user, body.plan_id))


@router.post("/portal-session", response_model=RedirectOut)
async def portal(db: DB, user: User = Depends(require(Perm.BILLING_MANAGE_OWN))):
    """Upgrade/downgrade, cancel, update card, download invoices — all Stripe-hosted."""
    return RedirectOut(url=await billing_service.create_portal_session(db, user))


@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, db: DB,
                         stripe_signature: str | None = Header(None, alias="Stripe-Signature")):
    # Signature is computed over the *raw* bytes — never parse JSON first.
    payload = await request.body()
    event = billing_service.verify_webhook(payload, stripe_signature)
    result = await billing_service.handle_webhook_event(db, event)
    return {"received": True, "result": result}
