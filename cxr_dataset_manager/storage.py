"""MinIO / S3 存取。

物件路徑沿用既有慣例：
    original-sets bucket:  {original_set}/images/{version}/{file_name}
    manual-sets   bucket:  {manual_set}/annotations/{version}/{file_name}
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from cxr_dataset_manager.settings import (
    MANUAL_SET_BUCKET,
    ORIGINAL_SET_BUCKET,
    settings,
)


def object_key_for_image(original_set_name: str, version: str, file_name: str) -> str:
    return f"{original_set_name}/images/{version}/{file_name}"


def object_key_for_manual_set_file(manual_set_name: str, version: str, file_name: str) -> str:
    return f"{manual_set_name}/annotations/{version}/{file_name}"


class ObjectStore:
    def __init__(self, client=None) -> None:
        self.client = client or boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            config=Config(signature_version="s3v4", retries={"max_attempts": 2}),
        )

    def ensure_buckets(self) -> list[str]:
        created = []
        for bucket in (ORIGINAL_SET_BUCKET, MANUAL_SET_BUCKET):
            try:
                self.client.head_bucket(Bucket=bucket)
            except ClientError:
                self.client.create_bucket(Bucket=bucket)
                created.append(bucket)
        return created

    def put(self, key: str, payload: bytes, content_type: str = "application/octet-stream",
            bucket: str = ORIGINAL_SET_BUCKET) -> None:
        self.client.put_object(Bucket=bucket, Key=key, Body=payload, ContentType=content_type)

    def get(self, key: str, bucket: str = ORIGINAL_SET_BUCKET) -> Optional[bytes]:
        try:
            return self.client.get_object(Bucket=bucket, Key=key)["Body"].read()
        except ClientError:
            return None

    def presigned_url(self, key: str, bucket: str = ORIGINAL_SET_BUCKET) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=settings.presigned_url_expire_seconds,
        )

    def alive(self) -> bool:
        try:
            self.client.list_buckets()
            return True
        except Exception:
            return False


@lru_cache
def get_store() -> ObjectStore:
    return ObjectStore()
