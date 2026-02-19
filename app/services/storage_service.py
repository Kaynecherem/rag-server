"""Storage Service - S3 in production, local filesystem in development."""

import json
import os
import shutil

import structlog

from app.config import get_settings

logger = structlog.get_logger()
settings = get_settings()


class StorageService:
    """Handles file storage. Uses local filesystem when no AWS keys are configured."""

    def __init__(self):
        self.use_s3 = bool(settings.aws_access_key_id and settings.aws_secret_access_key)

        if self.use_s3:
            import boto3
            self.s3 = boto3.client(
                "s3",
                aws_access_key_id=settings.aws_access_key_id,
                aws_secret_access_key=settings.aws_secret_access_key,
                region_name=settings.aws_region,
            )
            self.bucket = settings.s3_bucket_name
            logger.info("Storage: AWS S3", bucket=self.bucket)
        else:
            self.local_root = os.environ.get("LOCAL_STORAGE_PATH", "/app/storage")
            os.makedirs(self.local_root, exist_ok=True)
            logger.info("Storage: Local filesystem", path=self.local_root)

    def _local_path(self, key: str) -> str:
        path = os.path.join(self.local_root, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def _policy_key(self, tenant_id: str, policy_number: str, filename: str) -> str:
        return f"tenants/{tenant_id}/policies/{policy_number}/{filename}"

    def _communication_key(self, tenant_id: str, doc_id: str, filename: str) -> str:
        return f"tenants/{tenant_id}/communications/{doc_id}/{filename}"

    # ── Upload ────────────────────────────────────────────────────────────

    async def upload_policy(
        self, tenant_id: str, policy_number: str, file_bytes: bytes, filename: str
    ) -> str:
        key = self._policy_key(tenant_id, policy_number, filename)
        if self.use_s3:
            self.s3.put_object(
                Bucket=self.bucket, Key=key, Body=file_bytes,
                ContentType="application/pdf",
                Metadata={"tenant_id": tenant_id, "policy_number": policy_number},
            )
        else:
            with open(self._local_path(key), "wb") as f:
                f.write(file_bytes)
        logger.info("Policy uploaded", key=key, storage="s3" if self.use_s3 else "local")
        return key

    async def upload_communication(
        self, tenant_id: str, doc_id: str, file_bytes: bytes,
        filename: str, content_type: str = "application/pdf"
    ) -> str:
        key = self._communication_key(tenant_id, doc_id, filename)
        if self.use_s3:
            self.s3.put_object(
                Bucket=self.bucket, Key=key, Body=file_bytes,
                ContentType=content_type,
                Metadata={"tenant_id": tenant_id, "doc_id": doc_id},
            )
        else:
            with open(self._local_path(key), "wb") as f:
                f.write(file_bytes)
        logger.info("Communication uploaded", key=key)
        return key

    # ── Download ──────────────────────────────────────────────────────────

    async def download_file(self, s3_key: str) -> bytes:
        if self.use_s3:
            response = self.s3.get_object(Bucket=self.bucket, Key=s3_key)
            return response["Body"].read()
        else:
            with open(self._local_path(s3_key), "rb") as f:
                return f.read()

    # ── Delete ────────────────────────────────────────────────────────────

    async def delete_policy(self, tenant_id: str, policy_number: str):
        prefix = f"tenants/{tenant_id}/policies/{policy_number}/"
        if self.use_s3:
            await self._delete_s3_prefix(prefix)
        else:
            self._delete_local_prefix(prefix)

    async def delete_communication(self, tenant_id: str, doc_id: str):
        prefix = f"tenants/{tenant_id}/communications/{doc_id}/"
        if self.use_s3:
            await self._delete_s3_prefix(prefix)
        else:
            self._delete_local_prefix(prefix)

    async def _delete_s3_prefix(self, prefix: str):
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            objects = page.get("Contents", [])
            if objects:
                delete_keys = [{"Key": obj["Key"]} for obj in objects]
                self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": delete_keys})
                logger.info("Deleted S3 objects", prefix=prefix, count=len(delete_keys))

    def _delete_local_prefix(self, prefix: str):
        local_dir = os.path.join(self.local_root, prefix)
        if os.path.exists(local_dir):
            shutil.rmtree(local_dir)
            logger.info("Deleted local files", prefix=prefix)

    # ── Metadata ──────────────────────────────────────────────────────────

    async def save_chunks_metadata(
        self, tenant_id: str, document_type: str, doc_id: str, chunks: list[dict]
    ):
        if document_type == "policy":
            key = f"tenants/{tenant_id}/policies/{doc_id}/chunks.json"
        else:
            key = f"tenants/{tenant_id}/communications/{doc_id}/chunks.json"

        data = json.dumps(chunks, default=str).encode()
        if self.use_s3:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType="application/json")
        else:
            with open(self._local_path(key), "wb") as f:
                f.write(data)