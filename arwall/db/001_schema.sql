-- =============================================================================
--  AR Wall Painting SaaS — PostgreSQL schema  (migration 001, PostgreSQL 15+)
-- =============================================================================
--  Design principles
--  * UUID primary keys (gen_random_uuid) — safe to expose in URLs, shard-friendly.
--  * TIMESTAMPTZ everywhere; all times stored in UTC.
--  * Soft delete (deleted_at) on user-facing content so "undo" and GDPR
--    grace periods are possible; hard purge runs as a scheduled job.
--  * Entitlements live in data (plans.limits JSONB), not in code, so pricing
--    changes never need a deploy.
--  * Stripe is the source of truth for billing; we mirror state via webhooks
--    and keep an idempotency ledger (stripe_events).
--  * Large binaries never touch the DB — only S3 object keys + metadata.
-- =============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS citext;     -- case-insensitive email

-- -----------------------------------------------------------------------------
-- Enumerations
-- -----------------------------------------------------------------------------
CREATE TYPE user_role AS ENUM (
    'user',          -- end customer (homeowner, designer)
    'org_member',    -- member of an Enterprise organization
    'org_admin',     -- manages an organization's seats, brand catalogs
    'support',       -- read-only access to customer data for support
    'admin'          -- platform administrator
);

CREATE TYPE plan_tier AS ENUM ('free', 'pro', 'enterprise');

-- Mirrors Stripe subscription.status values 1:1
CREATE TYPE subscription_status AS ENUM (
    'trialing', 'active', 'past_due', 'canceled',
    'unpaid', 'incomplete', 'incomplete_expired', 'paused'
);

CREATE TYPE project_status AS ENUM ('draft', 'processing', 'ready', 'archived');

CREATE TYPE asset_kind AS ENUM (
    'room_photo',    -- original high-res capture
    'wall_mask',     -- segmentation output (PNG, 8-bit)
    'render',        -- painted result
    'thumbnail',
    'ar_snapshot'    -- screenshot captured from live AR session
);

CREATE TYPE upload_status AS ENUM ('pending', 'uploaded', 'verified', 'failed');

CREATE TYPE ai_job_type AS ENUM ('wall_segmentation', 'palette_recommendation', 'render');

CREATE TYPE ai_job_status AS ENUM ('queued', 'running', 'succeeded', 'failed', 'canceled');

CREATE TYPE palette_source AS ENUM ('manual', 'ai', 'brand', 'system');

CREATE TYPE paint_finish AS ENUM ('flat', 'matte', 'eggshell', 'satin', 'semi_gloss', 'gloss');

-- -----------------------------------------------------------------------------
-- Generic helpers
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- HEX colour domain: '#RRGGBB', stored upper-case.
CREATE DOMAIN hex_color AS CHAR(7)
    CHECK (VALUE ~ '^#[0-9A-F]{6}$');

-- -----------------------------------------------------------------------------
-- Organizations (Enterprise tenants: paint brands, design studios, contractors)
-- -----------------------------------------------------------------------------
CREATE TABLE organizations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT        NOT NULL CHECK (length(name) BETWEEN 2 AND 120),
    slug                CITEXT      NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]{2,60}$'),
    stripe_customer_id  TEXT        UNIQUE,
    seat_limit          INTEGER     NOT NULL DEFAULT 5 CHECK (seat_limit > 0),
    settings            JSONB       NOT NULL DEFAULT '{}'::jsonb,   -- white-label config, SSO, etc.
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ
);
CREATE TRIGGER trg_organizations_updated BEFORE UPDATE ON organizations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- Users
-- -----------------------------------------------------------------------------
CREATE TABLE users (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email               CITEXT      NOT NULL,
    password_hash       TEXT,                       -- NULL for social-login-only accounts
    full_name           TEXT        CHECK (length(full_name) <= 120),
    avatar_url          TEXT,
    role                user_role   NOT NULL DEFAULT 'user',
    organization_id     UUID        REFERENCES organizations(id) ON DELETE SET NULL,
    stripe_customer_id  TEXT        UNIQUE,
    email_verified_at   TIMESTAMPTZ,
    last_login_at       TIMESTAMPTZ,
    failed_login_count  INTEGER     NOT NULL DEFAULT 0,
    locked_until        TIMESTAMPTZ,                -- brute-force lockout
    token_version       INTEGER     NOT NULL DEFAULT 0,  -- bump to revoke all JWTs instantly
    locale              TEXT        NOT NULL DEFAULT 'en',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ,
    -- org roles require an organization
    CONSTRAINT chk_org_role CHECK (
        role NOT IN ('org_member', 'org_admin') OR organization_id IS NOT NULL
    )
);
-- Email unique among live accounts only (lets a deleted account's email be reused)
CREATE UNIQUE INDEX uq_users_email_live ON users (email) WHERE deleted_at IS NULL;
CREATE INDEX ix_users_org ON users (organization_id) WHERE organization_id IS NOT NULL;
CREATE TRIGGER trg_users_updated BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Federated identities (Sign in with Apple / Google)
CREATE TABLE user_identities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider        TEXT        NOT NULL CHECK (provider IN ('google', 'apple')),
    provider_uid    TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_uid)
);

-- Refresh tokens: stored as SHA-256 hashes, rotated on every use.
-- family_id groups a rotation chain; re-use of a revoked token revokes the family
-- (OAuth 2.0 Security BCP refresh-token-reuse detection).
CREATE TABLE refresh_tokens (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    family_id       UUID        NOT NULL,
    token_hash      CHAR(64)    NOT NULL UNIQUE,
    device_label    TEXT,
    ip_address      INET,
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    replaced_by     UUID        REFERENCES refresh_tokens(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_refresh_tokens_user ON refresh_tokens (user_id) WHERE revoked_at IS NULL;
CREATE INDEX ix_refresh_tokens_family ON refresh_tokens (family_id);

-- -----------------------------------------------------------------------------
-- Plans & Subscriptions
-- -----------------------------------------------------------------------------
CREATE TABLE plans (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tier                    plan_tier   NOT NULL,
    name                    TEXT        NOT NULL,
    billing_interval        TEXT        NOT NULL CHECK (billing_interval IN ('none', 'month', 'year')),
    stripe_price_id         TEXT        UNIQUE,     -- NULL for free tier
    price_cents             INTEGER     NOT NULL DEFAULT 0 CHECK (price_cents >= 0),
    currency                CHAR(3)     NOT NULL DEFAULT 'USD',
    -- Entitlements. A value of -1 means "unlimited".
    limits                  JSONB       NOT NULL,
    is_active               BOOLEAN     NOT NULL DEFAULT true,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tier, billing_interval),
    CONSTRAINT chk_limits_keys CHECK (
        limits ?& ARRAY['max_projects', 'max_storage_mb', 'ai_recommendations_per_month',
                        'segmentations_per_month', 'hd_export', 'custom_palettes', 'brand_catalogs']
    )
);
CREATE TRIGGER trg_plans_updated BEFORE UPDATE ON plans
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- A subscription belongs to exactly one billing owner: a user OR an organization.
CREATE TABLE subscriptions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID        REFERENCES users(id) ON DELETE CASCADE,
    organization_id         UUID        REFERENCES organizations(id) ON DELETE CASCADE,
    plan_id                 UUID        NOT NULL REFERENCES plans(id),
    stripe_subscription_id  TEXT        UNIQUE,
    status                  subscription_status NOT NULL,
    seats                   INTEGER     NOT NULL DEFAULT 1 CHECK (seats > 0),
    current_period_start    TIMESTAMPTZ,
    current_period_end      TIMESTAMPTZ,
    cancel_at_period_end    BOOLEAN     NOT NULL DEFAULT false,
    trial_end               TIMESTAMPTZ,
    canceled_at             TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_sub_owner CHECK (num_nonnulls(user_id, organization_id) = 1)
);
-- At most one live subscription per owner.
CREATE UNIQUE INDEX uq_sub_live_user ON subscriptions (user_id)
    WHERE user_id IS NOT NULL AND status IN ('trialing', 'active', 'past_due', 'incomplete', 'paused');
CREATE UNIQUE INDEX uq_sub_live_org ON subscriptions (organization_id)
    WHERE organization_id IS NOT NULL AND status IN ('trialing', 'active', 'past_due', 'incomplete', 'paused');
CREATE TRIGGER trg_subscriptions_updated BEFORE UPDATE ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Webhook idempotency ledger: Stripe delivers at-least-once.
CREATE TABLE stripe_events (
    id              TEXT PRIMARY KEY,               -- evt_...
    type            TEXT        NOT NULL,
    payload         JSONB       NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at    TIMESTAMPTZ,
    error           TEXT
);
CREATE INDEX ix_stripe_events_unprocessed ON stripe_events (received_at) WHERE processed_at IS NULL;

-- -----------------------------------------------------------------------------
-- Paint brand catalogs
-- -----------------------------------------------------------------------------
CREATE TABLE paint_brands (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID        REFERENCES organizations(id) ON DELETE CASCADE, -- NULL = global catalog
    name            TEXT        NOT NULL,
    slug            CITEXT      NOT NULL,
    country_code    CHAR(2),
    website_url     TEXT,
    logo_s3_key     TEXT,
    is_public       BOOLEAN     NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (organization_id, slug)
);
CREATE TRIGGER trg_paint_brands_updated BEFORE UPDATE ON paint_brands
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Individual paint colours. CIELAB is stored so the API can find the nearest
-- real paint to any AI-suggested HEX via ΔE (perceptual distance), not RGB distance.
CREATE TABLE paint_colors (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id        UUID        NOT NULL REFERENCES paint_brands(id) ON DELETE CASCADE,
    code            TEXT        NOT NULL,           -- manufacturer code, e.g. "SW 7008"
    name            TEXT        NOT NULL,
    hex             hex_color   NOT NULL,
    lab_l           REAL        NOT NULL CHECK (lab_l BETWEEN 0 AND 100),
    lab_a           REAL        NOT NULL,
    lab_b           REAL        NOT NULL,
    lrv             REAL        CHECK (lrv BETWEEN 0 AND 100),  -- Light Reflectance Value
    collection      TEXT,
    finishes        TEXT[]      NOT NULL DEFAULT '{}'
                    CHECK (finishes <@ ARRAY['flat','matte','eggshell','satin','semi_gloss','gloss']),
    is_discontinued BOOLEAN     NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (brand_id, code)
);
CREATE INDEX ix_paint_colors_hex ON paint_colors (hex);
-- Coarse box pre-filter for nearest-colour search before exact ΔE in app code.
CREATE INDEX ix_paint_colors_lab ON paint_colors (lab_l, lab_a, lab_b) WHERE NOT is_discontinued;
CREATE INDEX ix_paint_colors_name ON paint_colors (lower(name) text_pattern_ops);  -- prefix search
CREATE TRIGGER trg_paint_colors_updated BEFORE UPDATE ON paint_colors
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- Palettes (user-made, AI-generated, or curated brand collections)
-- -----------------------------------------------------------------------------
CREATE TABLE palettes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id   UUID        REFERENCES users(id) ON DELETE CASCADE,     -- NULL for system/brand
    organization_id UUID        REFERENCES organizations(id) ON DELETE CASCADE,
    brand_id        UUID        REFERENCES paint_brands(id) ON DELETE SET NULL,
    name            TEXT        NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    description     TEXT,
    source          palette_source NOT NULL DEFAULT 'manual',
    mood_tags       TEXT[]      NOT NULL DEFAULT '{}',    -- e.g. {calm, warm, scandinavian}
    ai_rationale    TEXT,                                 -- justification from the AI assistant
    is_public       BOOLEAN     NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ,
    CONSTRAINT chk_palette_owner CHECK (
        source IN ('system', 'brand') OR owner_user_id IS NOT NULL
    )
);
CREATE INDEX ix_palettes_owner ON palettes (owner_user_id, created_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX ix_palettes_public ON palettes (created_at DESC) WHERE is_public AND deleted_at IS NULL;
CREATE INDEX ix_palettes_mood ON palettes USING GIN (mood_tags);
CREATE TRIGGER trg_palettes_updated BEFORE UPDATE ON palettes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE palette_colors (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    palette_id      UUID        NOT NULL REFERENCES palettes(id) ON DELETE CASCADE,
    position        SMALLINT    NOT NULL CHECK (position BETWEEN 0 AND 11),
    hex             hex_color   NOT NULL,
    paint_color_id  UUID        REFERENCES paint_colors(id) ON DELETE SET NULL, -- matched real paint
    role            TEXT        CHECK (role IN ('primary', 'secondary', 'accent', 'trim', 'ceiling')),
    label           TEXT,
    UNIQUE (palette_id, position)
);

-- -----------------------------------------------------------------------------
-- Projects (a room being redesigned)
-- -----------------------------------------------------------------------------
CREATE TABLE projects (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id   UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    organization_id UUID        REFERENCES organizations(id) ON DELETE SET NULL,
    name            TEXT        NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    room_type       TEXT        CHECK (room_type IN ('living_room', 'bedroom', 'kitchen', 'bathroom',
                                                     'office', 'dining_room', 'kids_room', 'exterior', 'other')),
    status          project_status NOT NULL DEFAULT 'draft',
    active_palette_id UUID      REFERENCES palettes(id) ON DELETE SET NULL,
    cover_asset_id  UUID,                           -- FK added after project_assets exists
    -- Room analysis cache (lighting temp in Kelvin, estimated dimensions, dominant furniture colours)
    room_analysis   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);
CREATE INDEX ix_projects_owner ON projects (owner_user_id, updated_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX ix_projects_org ON projects (organization_id, updated_at DESC)
    WHERE organization_id IS NOT NULL AND deleted_at IS NULL;
CREATE TRIGGER trg_projects_updated BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Files in S3. Upload flow: API creates row (pending) + presigned PUT →
-- client uploads directly to S3 → client confirms → API HEADs object → verified.
CREATE TABLE project_assets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind            asset_kind  NOT NULL,
    s3_key          TEXT        NOT NULL UNIQUE,
    content_type    TEXT        NOT NULL CHECK (content_type IN ('image/jpeg', 'image/png', 'image/heic', 'image/webp')),
    size_bytes      BIGINT      CHECK (size_bytes >= 0),
    width_px        INTEGER     CHECK (width_px > 0),
    height_px       INTEGER     CHECK (height_px > 0),
    sha256          CHAR(64),
    upload_status   upload_status NOT NULL DEFAULT 'pending',
    parent_asset_id UUID        REFERENCES project_assets(id) ON DELETE CASCADE, -- mask/render → source photo
    capture_meta    JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- EXIF, AR camera intrinsics, device
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_assets_project ON project_assets (project_id, kind);
CREATE INDEX ix_assets_pending ON project_assets (created_at) WHERE upload_status = 'pending';
CREATE TRIGGER trg_assets_updated BEFORE UPDATE ON project_assets
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE projects ADD CONSTRAINT fk_projects_cover
    FOREIGN KEY (cover_asset_id) REFERENCES project_assets(id) ON DELETE SET NULL;

-- Each detected (or AR-anchored) wall surface and the paint applied to it.
CREATE TABLE project_walls (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_asset_id UUID        REFERENCES project_assets(id) ON DELETE SET NULL,
    mask_asset_id   UUID        REFERENCES project_assets(id) ON DELETE SET NULL,
    label           TEXT        NOT NULL DEFAULT 'Wall',
    -- Normalised [0..1] image-space polygon, and/or AR plane anchor (pose + extent)
    polygon         JSONB,
    ar_anchor       JSONB,
    area_m2         NUMERIC(8,2) CHECK (area_m2 >= 0),
    confidence      REAL        CHECK (confidence BETWEEN 0 AND 1),
    applied_hex     hex_color,
    paint_color_id  UUID        REFERENCES paint_colors(id) ON DELETE SET NULL,
    finish          paint_finish,
    opacity         REAL        NOT NULL DEFAULT 1.0 CHECK (opacity BETWEEN 0 AND 1),
    sort_order      SMALLINT    NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_walls_project ON project_walls (project_id, sort_order);
CREATE TRIGGER trg_walls_updated BEFORE UPDATE ON project_walls
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- AI jobs (async: API enqueues → SQS → GPU worker → updates row)
-- -----------------------------------------------------------------------------
CREATE TABLE ai_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id      UUID        REFERENCES projects(id) ON DELETE CASCADE,
    type            ai_job_type NOT NULL,
    status          ai_job_status NOT NULL DEFAULT 'queued',
    idempotency_key TEXT,
    input           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    output          JSONB,
    error           TEXT,
    attempts        SMALLINT    NOT NULL DEFAULT 0,
    model_version   TEXT,
    cost_micros     BIGINT      NOT NULL DEFAULT 0,  -- LLM/GPU cost tracking, USD × 1e6
    queued_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    UNIQUE (user_id, idempotency_key)
);
CREATE INDEX ix_ai_jobs_project ON ai_jobs (project_id, queued_at DESC);
CREATE INDEX ix_ai_jobs_active ON ai_jobs (status, queued_at) WHERE status IN ('queued', 'running');

-- -----------------------------------------------------------------------------
-- Metered usage for quota enforcement (per calendar month, UTC)
-- -----------------------------------------------------------------------------
CREATE TABLE usage_counters (
    user_id         UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    metric          TEXT        NOT NULL CHECK (metric IN ('ai_recommendations', 'segmentations', 'hd_exports')),
    period_start    DATE        NOT NULL,          -- first day of month
    count           INTEGER     NOT NULL DEFAULT 0 CHECK (count >= 0),
    PRIMARY KEY (user_id, metric, period_start)
);

-- -----------------------------------------------------------------------------
-- Audit log (append-only; partition by month at scale)
-- -----------------------------------------------------------------------------
CREATE TABLE audit_logs (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_user_id   UUID        REFERENCES users(id) ON DELETE SET NULL,
    action          TEXT        NOT NULL,          -- e.g. 'auth.login', 'billing.plan_changed'
    target_type     TEXT,
    target_id       TEXT,
    ip_address      INET,
    user_agent      TEXT,
    metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_actor ON audit_logs (actor_user_id, created_at DESC);
CREATE INDEX ix_audit_action ON audit_logs (action, created_at DESC);

-- -----------------------------------------------------------------------------
-- Entitlement view: the effective plan for every live user.
-- Precedence: org subscription (Enterprise) > personal subscription > free.
-- 'past_due' keeps access during Stripe's dunning window.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_user_entitlements AS
WITH free_plan AS (
    SELECT id, tier, limits FROM plans WHERE tier = 'free' AND is_active ORDER BY created_at LIMIT 1
),
live_subs AS (
    SELECT s.*, p.tier, p.limits
    FROM subscriptions s JOIN plans p ON p.id = s.plan_id
    WHERE s.status IN ('trialing', 'active', 'past_due')
)
SELECT
    u.id AS user_id,
    COALESCE(os.tier,   us.tier,   fp.tier)   AS tier,
    COALESCE(os.limits, us.limits, fp.limits) AS limits,
    COALESCE(os.id,     us.id)                AS subscription_id,
    COALESCE(os.status, us.status)            AS subscription_status,
    COALESCE(os.current_period_end, us.current_period_end) AS current_period_end
FROM users u
CROSS JOIN free_plan fp
LEFT JOIN live_subs us ON us.user_id = u.id
LEFT JOIN live_subs os ON os.organization_id = u.organization_id AND u.organization_id IS NOT NULL
WHERE u.deleted_at IS NULL;

-- -----------------------------------------------------------------------------
-- Seed plans (replace stripe_price_id values with your live Price IDs)
-- -----------------------------------------------------------------------------
INSERT INTO plans (tier, name, billing_interval, stripe_price_id, price_cents, limits) VALUES
('free', 'Free', 'none', NULL, 0,
 '{"max_projects":3,"max_storage_mb":250,"ai_recommendations_per_month":3,"segmentations_per_month":10,"hd_export":false,"custom_palettes":5,"brand_catalogs":1}'),
('pro', 'Pro Monthly', 'month', 'price_PRO_MONTHLY_REPLACE', 1299,
 '{"max_projects":100,"max_storage_mb":10240,"ai_recommendations_per_month":100,"segmentations_per_month":500,"hd_export":true,"custom_palettes":-1,"brand_catalogs":-1}'),
('pro', 'Pro Yearly', 'year', 'price_PRO_YEARLY_REPLACE', 12900,
 '{"max_projects":100,"max_storage_mb":10240,"ai_recommendations_per_month":100,"segmentations_per_month":500,"hd_export":true,"custom_palettes":-1,"brand_catalogs":-1}'),
('enterprise', 'Enterprise', 'month', 'price_ENTERPRISE_REPLACE', 9900,
 '{"max_projects":-1,"max_storage_mb":-1,"ai_recommendations_per_month":-1,"segmentations_per_month":-1,"hd_export":true,"custom_palettes":-1,"brand_catalogs":-1,"white_label":true,"private_catalogs":true,"sso":true}');

COMMIT;
