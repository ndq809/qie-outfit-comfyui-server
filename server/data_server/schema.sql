-- wardrobe-system-spec.md §2.1 data model, test-deployment schema.
-- Applied once by scripts/init_db.py. Not meant to be hand-edited after go-live —
-- add a migration file instead if this ever needs to grow past the test box.

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

CREATE TABLE IF NOT EXISTS accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- D0b's reference face for this account (wardrobe-system-spec.md §2.3.7). NULL means
-- fall back to TEST_FIXED_FACE_REF_IMAGE, then to "largest person in frame".
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS face_ref_key TEXT;

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES accounts(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Stands in for the login/session system the spec assumes exists upstream
-- (out of scope for this test deployment) — a static bearer token per test user.
CREATE TABLE IF NOT EXISTS api_tokens (
    token TEXT PRIMARY KEY,
    account_id UUID NOT NULL REFERENCES accounts(id),
    user_id UUID NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Upload batches, jobs and extraction results are not stored here: they live in Redis
-- until the user reviews them (server/data_server/jobs.py). These drops retire the
-- tables an earlier version kept them in.
ALTER TABLE IF EXISTS wardrobe_items DROP CONSTRAINT IF EXISTS wardrobe_items_job_id_fkey;
ALTER TABLE IF EXISTS wardrobe_items DROP CONSTRAINT IF EXISTS wardrobe_items_job_item_id_fkey;
ALTER TABLE IF EXISTS wardrobe_items DROP COLUMN IF EXISTS job_item_id;
DROP TABLE IF EXISTS job_items, jobs, upload_batch_items, upload_batches;

-- Only garments the user confirmed. job_id/source_local_id say which job and photo it came
-- from (no FK - the job itself is gone from Redis after its TTL). source_garment_id makes
-- confirming idempotent. ai_tags is what D3 predicted; tags is what the user accepted,
-- possibly edited (tags_edited) - in which case text_embedding still reflects ai_tags.
CREATE TABLE IF NOT EXISTS wardrobe_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES accounts(id),
    user_id UUID NOT NULL REFERENCES users(id),
    job_id UUID NOT NULL,
    object_key TEXT NOT NULL,
    tags JSONB NOT NULL,
    description TEXT NOT NULL,
    visual_embedding DOUBLE PRECISION[] NOT NULL,
    text_embedding DOUBLE PRECISION[] NOT NULL,
    duplicate_of UUID REFERENCES wardrobe_items(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE wardrobe_items ADD COLUMN IF NOT EXISTS source_local_id TEXT;
ALTER TABLE wardrobe_items ADD COLUMN IF NOT EXISTS source_garment_id TEXT;
ALTER TABLE wardrobe_items ADD COLUMN IF NOT EXISTS ai_tags JSONB;
ALTER TABLE wardrobe_items ADD COLUMN IF NOT EXISTS tags_edited BOOLEAN NOT NULL DEFAULT false;
CREATE UNIQUE INDEX IF NOT EXISTS idx_wardrobe_items_source_garment
    ON wardrobe_items(source_garment_id) WHERE source_garment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_wardrobe_items_account_id ON wardrobe_items(account_id, id);
CREATE INDEX IF NOT EXISTS idx_wardrobe_items_job_id ON wardrobe_items(job_id);
