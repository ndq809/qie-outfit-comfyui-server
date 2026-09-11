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
    # Mobile must PUT straight to object-storage (§1.2 D0), so presigned URLs have
    # to carry a host the phone can reach — but sigv4 signs the Host header, and the
    # reverse proxy in front rewrites Host to its upstream value. So the two are
    # configured separately:
    #   minio_presign_endpoint — host:port the signature is computed against; must
    #       equal what object-storage actually receives in the Host header (i.e. the
    #       proxy's upstream address). Defaults to minio_endpoint.
    #   minio_public_url — origin substituted into the returned URL for the client.
    #       Empty means "hand back the signing endpoint unchanged" (direct access,
    #       no proxy), which is the production shape.
    minio_presign_endpoint: str = ""
    minio_public_url: str = ""
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

    # TEST DEPLOYMENT ONLY (wardrobe-system-spec.md §2.3.7). Path to an image on the
    # data-server box used as the D0b reference face for every account that has not
    # registered one of its own. Lets the test client skip the /v1/face-reference
    # upload entirely and start at POST /v1/uploads/presign. Leave EMPTY in
    # production: there the reference face is per-user and must come from the user.
    test_fixed_face_ref_image: str = ""

    presign_expires_seconds: int = 900
    read_url_expires_seconds: int = 3600

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
    def minio_signing_url(self) -> str:
        return f"http://{self.minio_presign_endpoint or self.minio_endpoint}"

    @property
    def minio_client_url(self) -> str:
        return self.minio_public_url or self.minio_signing_url

    @property
    def redis_kwargs(self) -> dict:
        return {"host": self.redis_host, "port": self.redis_port, "decode_responses": True}


@lru_cache
def get_settings() -> Settings:
    return Settings()
