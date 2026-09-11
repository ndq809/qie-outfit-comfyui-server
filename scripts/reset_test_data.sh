#!/bin/bash
# Wipes wardrobe/job data from the TEST deployment so a client integration starts from a
# clean slate. Keeps the seeded account + bearer token and the D0b face fixture, so no
# reconfiguration is needed afterwards.
#
# Why this exists: D2 dedup (wardrobe-system-spec.md §2.1) suppresses any garment that
# matches one already in the wardrobe, so re-uploading a photo that was processed during
# server testing completes with zero new items. That is correct behaviour but looks like
# a client bug during a first integration.
#
#   ./scripts/reset_test_data.sh --dry-run   # show what would be deleted
#   ./scripts/reset_test_data.sh             # actually delete
set -euo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

set -a; . "${WORKSPACE:-/workspace}/.env"; set +a

psql_q() { PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" \
             -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$1"; }

echo "Current state:"
echo "  wardrobe_items : $(psql_q 'SELECT count(*) FROM wardrobe_items;')"
echo "  jobs           : $(psql_q 'SELECT count(*) FROM jobs;')"
echo "  job_items      : $(psql_q 'SELECT count(*) FROM job_items;')"
echo "  accounts       : $(psql_q 'SELECT count(*) FROM accounts;')  (kept)"

if $DRY_RUN; then
    echo
    echo "--dry-run: nothing deleted. Re-run without the flag to apply."
    exit 0
fi

# Order matters: wardrobe_items -> job_items -> jobs -> upload_batch_items -> upload_batches,
# because each references the next via a foreign key.
psql_q "DELETE FROM wardrobe_items;" >/dev/null
psql_q "DELETE FROM job_items;"      >/dev/null
psql_q "DELETE FROM jobs;"           >/dev/null
psql_q "DELETE FROM upload_batch_items;" >/dev/null
psql_q "DELETE FROM upload_batches;" >/dev/null
echo "database cleared (accounts + api_tokens kept)"

# Extracted garment crops. The raw bucket is left alone apart from stale uploads: it
# should only ever hold the face fixture plus images still awaiting processing.
mc alias set _reset "http://${MINIO_ENDPOINT}" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1
mc rm --recursive --force "_reset/${MINIO_ITEMS_BUCKET}/" >/dev/null 2>&1 || true
mc rm --recursive --force "_reset/${MINIO_RAW_BUCKET}/raw/" >/dev/null 2>&1 || true
echo "object-storage cleared (face/_test_fixture/reference.jpg kept)"

# Leftover queue state from interrupted runs.
redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" del job_queue result_queue job_queue_dead cancelled_jobs >/dev/null
echo "queues cleared"

echo
echo "Remaining in object-storage:"
mc ls --recursive "_reset/${MINIO_RAW_BUCKET}" 2>/dev/null || true
echo "Bearer token still valid: ${TEST_BEARER_TOKEN:0:12}..."
