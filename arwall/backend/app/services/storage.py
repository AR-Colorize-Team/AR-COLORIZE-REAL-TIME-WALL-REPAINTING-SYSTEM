"""
S3 storage service.

Upload flow (the API never proxies image bytes — saves bandwidth and latency):
    1. Client → POST /projects/{id}/assets          → API returns presigned POST
    2. Client → S3 (multipart form POST, direct)     → S3 enforces size & type
    3. Client → POST /assets/{id}/complete           → API HEADs object, marks verified

Bucket configuration (Terraform):
    * Block all public access; SSE-KMS default encryption; versioning on.
    * Lifecycle: abort incomplete multipart uploads after 1 day; delete objects
      under `tmp/` after 7 days; transition renders to S3 Intelligent-Tiering.
    * CORS: allow POST/GET from the app origins only.
    * S3 event → EventBridge → AV/EXIF-strip Lambda on `users/*/originals/`.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings

_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/heic": "heic", "image/webp": "webp"}


@lru_cache
def _s3():
    s = get_settings()
    return boto3.client(
        "s3",
        region_name=s.aws_region,
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "adaptive"}),
    )


@dataclass(frozen=True)
class PresignedUpload:
    url: str
    fields: dict[str, str]
    s3_key: str
    expires_in: int


@dataclass(frozen=True)
class ObjectInfo:
    size_bytes: int
    content_type: str
    etag: str


def build_asset_key(user_id: uuid.UUID, project_id: uuid.UUID, asset_id: uuid.UUID,
                    kind: str, content_type: str) -> str:
    """
    Deterministic, tenant-prefixed key. The user-id prefix lets IAM policies
    and S3 Access Points scope access per tenant if ever required.
    """
    return f"users/{user_id}/projects/{project_id}/{kind}/{asset_id}.{_EXT[content_type]}"


def presign_upload(s3_key: str, content_type: str, max_bytes: int) -> PresignedUpload:
    """
    Presigned *POST* (not PUT) because only POST policies can enforce
    `content-length-range` server-side — a PUT URL would accept a 5 GB file.
    Pure local signing, no network call.
    """
    s = get_settings()
    post = _s3().generate_presigned_post(
        Bucket=s.s3_bucket,
        Key=s3_key,
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", 1024, max_bytes],
        ],
        ExpiresIn=s.s3_presign_ttl_seconds,
    )
    return PresignedUpload(url=post["url"], fields=post["fields"], s3_key=s3_key,
                           expires_in=s.s3_presign_ttl_seconds)


def presign_download(s3_key: str, expires_in: int = 300) -> str:
    """Short-lived GET URL. At scale, swap for CloudFront signed URLs (edge caching)."""
    s = get_settings()
    return _s3().generate_presigned_url(
        "get_object", Params={"Bucket": s.s3_bucket, "Key": s3_key}, ExpiresIn=expires_in
    )


async def head_object(s3_key: str) -> ObjectInfo | None:
    s = get_settings()

    def _head():
        try:
            r = _s3().head_object(Bucket=s.s3_bucket, Key=s3_key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return ObjectInfo(size_bytes=r["ContentLength"], content_type=r.get("ContentType", ""),
                          etag=r["ETag"].strip('"'))

    return await run_in_threadpool(_head)


async def delete_objects(keys: list[str]) -> None:
    """Batch delete (max 1000 per call). Used by the account-purge job."""
    if not keys:
        return
    s = get_settings()

    def _delete():
        for i in range(0, len(keys), 1000):
            chunk = keys[i:i + 1000]
            _s3().delete_objects(Bucket=s.s3_bucket,
                                 Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True})

    await run_in_threadpool(_delete)
