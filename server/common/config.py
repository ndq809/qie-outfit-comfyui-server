"""Environment-driven settings shared by data-server and ai-server.

Every peer address lives here and nowhere else, so moving from this
single-machine test layout to real separate machines (see
wardrobe-system-spec.md §2.2) is a matter of changing ${WORKSPACE}/.env, not code.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # object-storage (MinIO, S3-compatible)
    minio_endpoint: str = "127.0.0.1:9000"
    # Host:port Mobile reaches storage on. Presigned URLs are signed for this host
    # (SigV4 covers Host), while both servers keep using minio_endpoint for their own
    # traffic — on the test box that keeps internal reads off the public edge, which
    # would reject them for lack of the vast.ai edge token. Empty = same as internal.
    minio_public_endpoint: str = ""
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_raw_bucket: str = "wardrobe-raw"
    minio_items_bucket: str = "wardrobe-items"

    # queue (Redis) — job_queue / result_queue / job_queue_dead lists,
    # cancelled_jobs set
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379

    # data-server
    data_server_host: str = "127.0.0.1"
    data_server_port: int = 18080
    # Bearer tokens the API accepts, "token:accountId" pairs separated by commas. There is
    # no database: the account is only the namespace a token's batches and jobs live in
    # (Redis keys, raw/<accountId>/ in storage), and the wardrobe itself is kept on the
    # phone. Stands in for the login/session system the spec assumes upstream.
    api_tokens: str = ""

    # ai-server
    ai_server_health_port: int = 18090
    # D1 engine. "fast": d1_engine in the ai-server process (int8 W8A8 + SageAttention,
    # text encoder/VAE/transformer resident, D0b/D1/D3 pipelined across threads; no
    # ComfyUI or item_detector service needed). "comfyui": the original serial path -
    # HTTP to the item_detector service and a ComfyUI workflow round trip per photo.
    d1_engine: str = "fast"
    # Converted D1 weights (LoRAs merged, int8) are cached here: ~13GB, turns a ~25s
    # conversion at every start into a few seconds of reading.
    d1_cache_dir: str = "/workspace/.cache/d1_engine"
    # Lightning LoRA of the "fast" engine: 4 or 8 sampling steps. Measured 2026-10-14 over
    # 59 real photos x 3 seeds, garments the photo does not have drawn into the grid (by
    # eye): 4 steps 7/177 grids (33, 216, 657 - trousers under a lone shirt, a second pair
    # beside the asked one), 8 steps 2/177 (216 only, caught by the grid check), same
    # garments otherwise, 3.8s a grid instead of 2.0s. One cache file per value (~13GB).
    d1_steps: int = 8
    comfyui_url: str = "http://127.0.0.1:18188"
    item_detector_url: str = "http://127.0.0.1:18189"

    # pipeline tuning
    # D2: same garment twice in one photo (ai-server drops the copy) and across the photos
    # of one job (data-server marks it "duplicate", jobs._mark_job_duplicates). Matching
    # against the wardrobe is the phone's job now that the wardrobe lives there - the
    # confirm response hands it the embeddings, and server.common.embeddings has the rule.
    wardrobe_dedup_enabled: bool = False
    max_job_retries: int = 3

    # How long a job, its per-photo results and its unreviewed garments are kept (Redis
    # TTL, refreshed on every change, and the MinIO lifecycle on pending/ crops rounded
    # up to whole days). Past this the user has to re-run the photos.
    job_ttl_seconds: int = 7 * 24 * 3600

    presign_expires_seconds: int = 900
    read_url_expires_seconds: int = 3600

    # Test-only: keep each uploaded photo in the raw bucket after it has been processed,
    # so a wrong result can be re-run on the exact full-resolution input (the report only
    # keeps a downscaled preview, which does not always reproduce the detector's scores).
    # MUST be false in production - wardrobe-system-spec.md "Bảo mật và vòng đời dữ liệu"
    # requires the original to be deleted once extraction is done.
    keep_raw_images: bool = False

    # Test-only face reference for jobs that came without one (the phone sends the user's
    # face embedding with each job - POST /v1/jobs "faceReference", server.common.face_ref).
    # MUST be empty in production: a job without a face would be matched against a
    # stranger instead of falling back to the largest person in frame.
    test_fixed_face_ref_image: str = ""

    # Test-only: keep every pipeline stage of each photo on disk (original, the
    # SAM-isolated subject, the generated grid, the per-item crops) and build an HTML
    # page to compare them. MUST be empty in production — it keeps the user's original
    # photo, which §"Bảo mật và vòng đời dữ liệu" requires deleting once extraction is
    # done. Empty = nothing is written.
    wardrobe_report_dir: str = ""
    # The page is rendered on request (data-server GET /report/), never on the job's
    # hot path. Oldest job directories beyond max_jobs are deleted when it renders, and
    # only the newest page_jobs are put on the page, so viewing it costs the same
    # however long the instance has been up.
    wardrobe_report_max_jobs: int = 10
    wardrobe_report_page_jobs: int = 5

    @property
    def api_token_accounts(self) -> dict:
        """{token: accountId} from api_tokens."""
        out = {}
        for pair in self.api_tokens.split(","):
            token, _, account = pair.strip().partition(":")
            if token and account:
                out[token] = account
        return out

    @property
    def minio_url(self) -> str:
        return f"http://{self.minio_endpoint}"

    @property
    def minio_public_url(self) -> str:
        return f"http://{self.minio_public_endpoint or self.minio_endpoint}"

    @property
    def redis_kwargs(self) -> dict:
        return {"host": self.redis_host, "port": self.redis_port, "decode_responses": True}


@lru_cache
def get_settings() -> Settings:
    return Settings()
