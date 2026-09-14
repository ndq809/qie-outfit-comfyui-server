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

CREATE TABLE IF NOT EXISTS upload_batches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES accounts(id),
    user_id UUID NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS upload_batch_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id UUID NOT NULL REFERENCES upload_batches(id),
    local_id TEXT NOT NULL,
    object_key TEXT NOT NULL,
    content_type TEXT NOT NULL,
    checksum TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (batch_id, local_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES accounts(id),
    user_id UUID NOT NULL REFERENCES users(id),
    batch_id UUID NOT NULL REFERENCES upload_batches(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','processing','cancelling','cancelled','completed','failed')),
    total_items INTEGER NOT NULL DEFAULT 0,
    processed_items INTEGER NOT NULL DEFAULT 0,
    failed_items INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS job_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES jobs(id),
    local_id TEXT NOT NULL,
    object_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','success','failed')),
    error_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_job_items_job_id ON job_items(job_id);

CREATE TABLE IF NOT EXISTS wardrobe_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES accounts(id),
    user_id UUID NOT NULL REFERENCES users(id),
    job_id UUID NOT NULL REFERENCES jobs(id),
    job_item_id UUID NOT NULL REFERENCES job_items(id),
    object_key TEXT NOT NULL,
    tags JSONB NOT NULL,
    description TEXT NOT NULL,
    visual_embedding DOUBLE PRECISION[] NOT NULL,
    text_embedding DOUBLE PRECISION[] NOT NULL,
    duplicate_of UUID REFERENCES wardrobe_items(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wardrobe_items_account_id ON wardrobe_items(account_id, id);
CREATE INDEX IF NOT EXISTS idx_wardrobe_items_job_id ON wardrobe_items(job_id);
