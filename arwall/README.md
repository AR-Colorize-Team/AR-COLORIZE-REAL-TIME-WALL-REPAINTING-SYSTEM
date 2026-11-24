# AR Wall Painting SaaS — Parts 1 & 2: Database + Backend API

```
arwall/
├── db/001_schema.sql            # Part 1 — PostgreSQL schema (migration 001 + plan seeds)
├── docker-compose.yml           # Postgres + LocalStack (S3/SQS) + API for local dev
└── backend/                     # Part 2 — FastAPI service
    ├── app/
    │   ├── main.py              # app factory, JSON logging, request-id + security headers
    │   ├── core/                # config, security (Argon2id, JWT), rbac, errors (RFC 9457)
    │   ├── db/                  # async engine, SQLAlchemy 2.0 models
    │   ├── services/            # auth, entitlements/quotas, billing (Stripe), storage (S3),
    │   │                        # jobs (SQS), catalog (ΔE2000 matching), audit
    │   ├── api/                 # deps (auth/RBAC/ownership), schemas, routes/*
    │   └── utils/               # colour science, keyset pagination
    ├── tests/                   # SQL integration checks, unit tests, API flow tests
    ├── Dockerfile               # multi-stage, non-root, gunicorn + uvicorn workers
    └── requirements*.txt
```

## Why FastAPI (not Express)

The AI engine (Part 3) is Python: OpenCV/PyTorch segmentation and the Vision-LLM
client. One language across the API and the GPU workers lets them share Pydantic
contracts and colour utilities. FastAPI's async I/O matches Express on
I/O-bound API work, and Pydantic gives typed request validation and OpenAPI docs
at no extra cost.

## Quick start

```bash
docker compose up -d db localstack
cd backend && cp .env.example .env && pip install -r requirements-dev.txt
uvicorn app.main:app --reload                        # http://localhost:8000/docs

createdb arwall_test && psql -d arwall_test -f ../db/001_schema.sql
psql -d arwall_test -f tests/test_schema.sql         # 14 schema/SQL checks
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost/arwall_test pytest
```

## Endpoints (`/v1`)

| Area | Endpoint | Notes |
|---|---|---|
| Auth | `POST /auth/register` · `/login` · `/refresh` · `/logout` · `/logout-all` | Argon2id; 5-strike lockout; refresh rotation with reuse detection |
| Me | `GET/PATCH/DELETE /me` · `GET /me/entitlements` · `POST /me/password` | entitlements drive the paywall UI |
| Projects | `GET/POST /projects` · `GET/PATCH/DELETE /projects/{id}` | keyset pagination; plan limit → `402 quota_exceeded` |
| Walls | `PUT /projects/{id}/walls` | idempotent "save scene"; normalised polygons and/or AR anchors |
| Assets | `POST /projects/{id}/assets` → S3 → `POST /assets/{id}/complete` · `GET /assets/{id}/download-url` | presigned **POST** enforces size server-side |
| Palettes | `GET/POST /palettes` · `GET/PUT/DELETE /palettes/{id}` | HEX normalised; auto-matched to real paints (ΔE ≤ 5) |
| Catalog | `GET /catalog/brands` · `/brands/{id}/colors` · `/match?hex=` · `POST /brands/{id}/colors:import` | CIEDE2000 nearest paint; private Enterprise catalogs |
| AI | `POST /projects/{id}/ai/segment` · `/ai/recommend-palettes` · `GET /ai/jobs/{id}` | async via SQS; atomic quota; `Idempotency-Key` |
| Billing | `GET /billing/plans` · `POST /checkout-session` · `/portal-session` · `/webhook` | Stripe Checkout + Portal; verified, idempotent webhooks |
| Admin | `GET /admin/users` · `PATCH /admin/users/{id}/role` | permission-gated; role change revokes sessions |
| Internal | `POST /internal/ai/jobs/{id}` | worker callback, shared-secret header, VPC-only |

## Key design decisions

* **Entitlements are data.** Plan limits are stored in `plans.limits` (JSONB, `-1` means unlimited) and resolved by `v_user_entitlements`. Precedence is org subscription, then personal subscription, then free. Changing a price or quota needs no deploy.
* **Race-free quotas.** Monthly AI usage is counted with one conditional `INSERT … ON CONFLICT DO UPDATE … WHERE count + n <= limit`, so concurrent requests can't overshoot. Project and storage caps use a per-user advisory lock. If an AI job fails, its quota unit is refunded.
* **Token model.** Access tokens are 15-minute JWTs. Refresh tokens are opaque, stored only as SHA-256 hashes, and rotated on every use; replaying an old one revokes the whole session family. A `token_version` column gives instant "log out everywhere" and role changes. Production signs with ES256, so workers can verify tokens without being able to mint them.
* **RBAC.** Endpoints ask for *permissions*, never roles (`core/rbac.py`). Ownership is a separate check. Other tenants' resources return 404, not 403.
* **Stripe is the source of truth.** Webhooks are signature-verified, written to `stripe_events` for idempotency, and the subscription is re-fetched from Stripe so out-of-order delivery is harmless. Since Stripe API version `2025-03-31`, billing-period dates sit on the subscription item, and the code reads them there with a fallback.
* **App-store rule.** Digital subscriptions bought inside iOS/Android apps generally have to go through Apple IAP or Google Play Billing. Stripe here covers web sign-up and Enterprise. Plan to feed RevenueCat (or store server notifications) into the same `subscriptions` table.

## Verification status

* ✅ Schema applied to PostgreSQL 16, and `tests/test_schema.sql` passes all 14 checks: quota upsert, subscription upsert, entitlement precedence, constraints, triggers, and the raw SQL the services run.
* ✅ `tests/test_color_and_rbac.py` passes 17 tests, including CIEDE2000 against the Sharma et al. reference data.
* ✅ All modules compile; internal imports resolve; no undefined names.
* ⚠️ `tests/test_api_flow.py` (12 end-to-end flows) was **not run**: the build sandbox couldn't install FastAPI/SQLAlchemy. Run it first after `pip install -r requirements-dev.txt`.
