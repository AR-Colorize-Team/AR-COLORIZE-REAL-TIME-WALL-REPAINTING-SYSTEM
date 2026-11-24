-- Integration checks for the schema and the raw SQL used by the API services.
-- Run: psql -v ON_ERROR_STOP=1 -d arwall_test -f tests/test_schema.sql
-- Every block ROLLBACKs, so the database is left untouched.
\set QUIET on

BEGIN;
DO $$
DECLARE
    u UUID; o UUID; pro UUID; n INT; t plan_tier; lim JSONB; ok BOOLEAN;
BEGIN
    -- 1. New user resolves to the free plan via the entitlement view ---------
    INSERT INTO users (email, password_hash) VALUES ('Alice@Example.com', 'x') RETURNING id INTO u;
    SELECT tier, limits INTO t, lim FROM v_user_entitlements WHERE user_id = u;
    ASSERT t = 'free', 'expected free tier, got ' || t;
    ASSERT (lim->>'ai_recommendations_per_month')::int = 3;

    -- 2. CITEXT email uniqueness among live users -----------------------------
    BEGIN
        INSERT INTO users (email) VALUES ('alice@EXAMPLE.com');
        RAISE EXCEPTION 'duplicate email accepted';
    EXCEPTION WHEN unique_violation THEN NULL;
    END;

    -- 3. Atomic quota upsert (exact SQL from services/entitlements.py), limit 3
    FOR i IN 1..4 LOOP
        WITH r AS (
            INSERT INTO usage_counters (user_id, metric, period_start, count)
            VALUES (u, 'ai_recommendations', date_trunc('month', now())::date, 1)
            ON CONFLICT (user_id, metric, period_start)
            DO UPDATE SET count = usage_counters.count + EXCLUDED.count
            WHERE 3 = -1 OR usage_counters.count + EXCLUDED.count <= 3
            RETURNING count)
        SELECT count(*) INTO n FROM r;
        IF i <= 3 THEN ASSERT n = 1, 'call ' || i || ' should succeed';
        ELSE ASSERT n = 0, '4th call must be rejected by the limit'; END IF;
    END LOOP;
    SELECT count INTO n FROM usage_counters WHERE user_id = u AND metric = 'ai_recommendations';
    ASSERT n = 3, 'counter must stop at the limit, got ' || n;

    -- 3b. Unlimited (-1) never blocks
    FOR i IN 1..5 LOOP
        INSERT INTO usage_counters (user_id, metric, period_start, count)
        VALUES (u, 'segmentations', date_trunc('month', now())::date, 1)
        ON CONFLICT (user_id, metric, period_start)
        DO UPDATE SET count = usage_counters.count + EXCLUDED.count
        WHERE -1 = -1 OR usage_counters.count + EXCLUDED.count <= -1;
    END LOOP;
    SELECT count INTO n FROM usage_counters WHERE user_id = u AND metric = 'segmentations';
    ASSERT n = 5;

    -- 4. Stripe subscription upsert (billing_service._upsert_subscription) ----
    SELECT id INTO pro FROM plans WHERE tier = 'pro' AND billing_interval = 'month';
    FOR i IN 1..2 LOOP   -- second pass = webhook re-delivery / update
        INSERT INTO subscriptions (user_id, organization_id, plan_id, stripe_subscription_id, status, seats,
                                   current_period_start, current_period_end, cancel_at_period_end, trial_end, canceled_at)
        VALUES (u, NULL, pro, 'sub_123', CAST(CASE WHEN i = 1 THEN 'incomplete' ELSE 'active' END AS subscription_status),
                1, now(), now() + interval '1 month', false, NULL, NULL)
        ON CONFLICT (stripe_subscription_id) DO UPDATE SET
            plan_id = EXCLUDED.plan_id, status = EXCLUDED.status, seats = EXCLUDED.seats,
            current_period_start = EXCLUDED.current_period_start, current_period_end = EXCLUDED.current_period_end,
            cancel_at_period_end = EXCLUDED.cancel_at_period_end, trial_end = EXCLUDED.trial_end,
            canceled_at = EXCLUDED.canceled_at;
    END LOOP;
    SELECT count(*) INTO n FROM subscriptions WHERE user_id = u;
    ASSERT n = 1, 'upsert must not duplicate';
    SELECT tier INTO t FROM v_user_entitlements WHERE user_id = u;
    ASSERT t = 'pro', 'active sub should grant pro, got ' || t;

    -- 4b. Only one live subscription per user
    BEGIN
        INSERT INTO subscriptions (user_id, plan_id, stripe_subscription_id, status)
        VALUES (u, pro, 'sub_456', 'active');
        RAISE EXCEPTION 'second live subscription accepted';
    EXCEPTION WHEN unique_violation THEN NULL;
    END;

    -- 4c. Cancelled → falls back to free
    UPDATE subscriptions SET status = 'canceled' WHERE stripe_subscription_id = 'sub_123';
    SELECT tier INTO t FROM v_user_entitlements WHERE user_id = u;
    ASSERT t = 'free';

    -- 5. Org subscription overrides personal plan ----------------------------
    INSERT INTO organizations (name, slug) VALUES ('Acme Paints', 'acme-paints') RETURNING id INTO o;
    UPDATE users SET organization_id = o, role = 'org_member' WHERE id = u;
    INSERT INTO subscriptions (organization_id, plan_id, stripe_subscription_id, status, seats)
    SELECT o, id, 'sub_org', 'active', 10 FROM plans WHERE tier = 'enterprise';
    SELECT tier, limits INTO t, lim FROM v_user_entitlements WHERE user_id = u;
    ASSERT t = 'enterprise' AND (lim->>'max_projects')::int = -1;

    -- 6. Org role requires an organization -----------------------------------
    BEGIN
        INSERT INTO users (email, role) VALUES ('bob@example.com', 'org_admin');
        RAISE EXCEPTION 'org_admin without org accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;

    -- 7. Subscription must have exactly one owner ----------------------------
    BEGIN
        INSERT INTO subscriptions (user_id, organization_id, plan_id, status) VALUES (u, o, pro, 'canceled');
        RAISE EXCEPTION 'two owners accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;

    -- 8. HEX domain ----------------------------------------------------------
    BEGIN
        PERFORM '#abc123'::hex_color;
        RAISE EXCEPTION 'lowercase hex accepted (API normalises to upper)';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
    PERFORM '#ABC123'::hex_color;

    -- 9. Stripe event idempotency ledger -------------------------------------
    INSERT INTO stripe_events (id, type, payload) VALUES ('evt_1', 'x', '{}') ON CONFLICT (id) DO NOTHING;
    WITH r AS (INSERT INTO stripe_events (id, type, payload) VALUES ('evt_1', 'x', '{}')
               ON CONFLICT (id) DO NOTHING RETURNING id)
    SELECT count(*) INTO n FROM r;
    ASSERT n = 0, 'duplicate event must be ignored';

    -- 10. Plan limits must contain every required key -------------------------
    BEGIN
        INSERT INTO plans (tier, name, billing_interval, limits) VALUES ('pro', 'Bad', 'none', '{"max_projects":1}');
        RAISE EXCEPTION 'incomplete limits accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;

    -- 11. Palette: manual palette needs an owner ------------------------------
    BEGIN
        INSERT INTO palettes (name, source) VALUES ('Orphan', 'manual');
        RAISE EXCEPTION 'ownerless manual palette accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;

    -- 12. Keyset pagination predicate + advisory lock function used by the API
    PERFORM 1 FROM projects WHERE (updated_at, id) < (now(), gen_random_uuid());
    PERFORM pg_advisory_xact_lock(hashtextextended('projects:' || u::text, 0));

    -- 13. updated_at trigger fires
    -- (now() is frozen inside a transaction, so back-date created_at first)
    UPDATE users SET created_at = now() - interval '1 day' WHERE id = u;
    UPDATE users SET full_name = 'Alice', updated_at = now() - interval '2 days' WHERE id = u;
    SELECT updated_at > created_at INTO ok FROM users WHERE id = u;
    ASSERT ok, 'updated_at trigger did not fire';

    -- 14. Wall polygon / opacity constraints
    INSERT INTO projects (owner_user_id, name) VALUES (u, 'Living room');
    BEGIN
        INSERT INTO project_walls (project_id, opacity) SELECT id, 1.5 FROM projects WHERE owner_user_id = u;
        RAISE EXCEPTION 'opacity > 1 accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;

    RAISE NOTICE 'ALL SCHEMA CHECKS PASSED';
END $$;
ROLLBACK;
