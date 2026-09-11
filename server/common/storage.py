"""Object-storage helpers (MinIO, S3-compatible) shared by data-server and
ai-server. Both raw uploads and extracted item crops live here; see
wardrobe-system-spec.md §2.1 for why Mobile/ai-server talk to storage directly
instead of proxying bytes through either server.
"""
import uuid
from pathlib import Path

import boto3
from botocore.client import Config as BotoConfig

from .config import get_settings


def s3_client():
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.minio_url,
        aws_access_key_id=s.minio_access_key,
        aws_secret_access_key=s.minio_secret_key,
        config=BotoConfig(signature_version="s3v4"),
        region_name="us-east-1",
    )


def s3_presign_client():
    """Separate client pinned to the signing endpoint. Only used to generate
    presigned URLs — every server-side get/put still goes over s3_client()."""
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.minio_signing_url,
        aws_access_key_id=s.minio_access_key,
        aws_secret_access_key=s.minio_secret_key,
        config=BotoConfig(signature_version="s3v4"),
        region_name="us-east-1",
    )


def ensure_buckets():
    s = get_settings()
    client = s3_client()
    existing = {b["Name"] for b in client.list_buckets().get("Buckets", [])}
    for bucket in (s.minio_raw_bucket, s.minio_items_bucket):
        if bucket not in existing:
            client.create_bucket(Bucket=bucket)


def raw_object_key(account_id: str, batch_id: str, local_id: str, content_type: str) -> str:
    ext = _ext_from_content_type(content_type)
    return f"raw/{account_id}/{batch_id}/{local_id}{ext}"


def face_ref_object_key(account_id: str, content_type: str) -> str:
    """The account's D0b reference selfie. Overwritten in place on re-register,
    so an account only ever has one — the old object doesn't linger in the raw
    bucket the way a uuid-suffixed key would."""
    return f"face/{account_id}/reference{_ext_from_content_type(content_type)}"


# Where the test-deployment fixed reference face is parked. Deliberately outside the
# per-account face/<account_id>/ namespace: it belongs to no account, it is shared by
# all of them, and keeping it separate means it is never mistaken for one a real user
# registered (and never served by GET /v1/face-reference).
FIXED_FACE_REF_KEY = "face/_test_fixture/reference.jpg"


def item_object_key(job_id: str, item_id: str, index: int) -> str:
    # No account_id here deliberately: ai-server only ever knows jobId/itemId/objectKey
    # from the job-queue ticket (wardrobe-system-spec.md §3.3) and must never be given
    # postgres's address (§2.3.3), so it can't look account_id up either.
    return f"items/{job_id}/{item_id}/{index:02d}.png"


def _ext_from_content_type(content_type: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/heic": ".heic",
    }.get((content_type or "").lower(), ".bin")


def _to_client_url(url: str) -> str:
    """Swap the signing origin for the one the client can reach. Only the origin
    changes — path and every X-Amz-* query parameter are left byte-for-byte as
    signed, so the signature still verifies at the far end (the proxy restores the
    signing Host on the way through)."""
    s = get_settings()
    signing, client = s.minio_signing_url, s.minio_client_url
    return client + url[len(signing):] if client != signing and url.startswith(signing) else url


def presign_put(bucket: str, key: str, content_type: str, expires: int) -> str:
    return _to_client_url(s3_presign_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=expires,
    ))


def presign_get(bucket: str, key: str, expires: int) -> str:
    return _to_client_url(s3_presign_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires,
    ))


def download_to(bucket: str, key: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    s3_client().download_file(bucket, key, str(dest))
    return dest


def upload_file(bucket: str, key: str, path: Path, content_type: str = "image/png"):
    s3_client().upload_file(str(path), bucket, key, ExtraArgs={"ContentType": content_type})


def delete_object(bucket: str, key: str):
    try:
        s3_client().delete_object(Bucket=bucket, Key=key)
    except Exception:
        pass


def new_id() -> str:
    return uuid.uuid4().hex
