"""Environment-driven settings shared by data-server and ai-server.

Every peer address lives here and nowhere else, so moving from this
single-machine test layout to real separate machines (see
wardrobe-system-spec.md §2.2) is a matter of changing ${WORKSPACE}/.env, not code.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # postgres-db (data-server only; ai-server must never read these)
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_db: str = "wardrobe"
    postgres_user: str = "wardrobe_app"
    postgres_password: str = ""

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

    # ai-server
    ai_server_health_port: int = 18090
    comfyui_url: str = "http://127.0.0.1:18188"
    item_detector_url: str = "http://127.0.0.1:18189"

    # pipeline tuning
    wardrobe_dedup_threshold: float = 0.90
    max_job_retries: int = 3

    presign_expires_seconds: int = 900
    read_url_expires_seconds: int = 3600

    # Test-only shared face reference (wardrobe-system-spec.md §2.3.7).
    # MUST be empty in production.
    test_fixed_face_ref_image: str = ""

    # Test-only: keep every pipeline stage of each photo on disk (original, the
    # SAM-isolated subject, the generated grid, the per-item crops) and build an HTML
    # page to compare them. MUST be empty in production — it keeps the user's original
    # photo, which §"Bảo mật và vòng đời dữ liệu" requires deleting once extraction is
    # done. Empty = nothing is written.
    wardrobe_report_dir: str = ""
    # build_index() rescans every job ever kept under wardrobe_report_dir on every
    # call, so on an instance that stays up across many real batches this is what keeps
    # a 60-photo upload from turning into an O(months of history) disk/CPU spike.
    # Oldest job directories beyond this count are deleted (by mtime) before each scan.
    wardrobe_report_max_jobs: int = 30
    # build_index() is called once per photo from ai-server (worker.py) and again from
    # data-server's result_consumer thread — a burst upload otherwise triggers a full
    # rescan-and-rewrite twice per photo, back to back, in the same process serving the
    # public API. Calls within this window collapse into a single trailing rebuild.
    wardrobe_report_debounce_seconds: float = 5.0

    @property
    def postgres_dsn(self) -> str:
        return (
            f"host={self.postgres_host} port={self.postgres_port} "
            f"dbname={self.postgres_db} user={self.postgres_user} "
            f"password={self.postgres_password}"
        )

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
