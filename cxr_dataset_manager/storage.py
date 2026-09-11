"""MinIO / S3 存取。

物件路徑沿用既有慣例：
    original-sets bucket:  {original_set}/images/{version}/{file_name}
    manual-sets   bucket:  {manual_set}/annotations/{version}/{file_name}

產生某個版本的 spec 就放在後者底下，檔名固定 spec.yaml。它是一份文件——
會被人讀、被人改、被 git 管——所以本體留在物件儲存，資料庫只留 sha256。

Every image batch, annotation batch and manual-set version also has a
human-written `__meta__.md` beside its files (`cxr meta`). It is documentation,
not data: nothing in the database refers to it and nothing reads it back.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

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


def object_key_for_manual_set_file(
    manual_set_name: str, version: str, file_name: str
) -> str:
    return f"{manual_set_name}/annotations/{version}/{file_name}"


SPEC_FILE_NAME = "spec.yaml"


def spec_key(manual_set_name: str, version: str) -> str:
    """產生某個版本的 spec 的物件路徑。位置是算出來的，不需要存在資料庫裡。"""
    return object_key_for_manual_set_file(manual_set_name, version, SPEC_FILE_NAME)


META_FILE_NAME = "__meta__.md"

MetaKind = Literal["images", "annotations", "manual-set"]


def meta_location(kind: MetaKind, name: str, version: str) -> tuple[str, str]:
    """(bucket, key) of the __meta__.md describing one batch or version.

    images / annotations sit in the original-sets bucket beside the batch's
    files; a manual-set version's sits beside its spec.yaml.
    """
    if kind == "images":
        return ORIGINAL_SET_BUCKET, f"{name}/images/{version}/{META_FILE_NAME}"
    if kind == "annotations":
        return ORIGINAL_SET_BUCKET, f"{name}/annotations/{version}/{META_FILE_NAME}"
    return MANUAL_SET_BUCKET, object_key_for_manual_set_file(
        name, version, META_FILE_NAME
    )


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

    def put(
        self,
        key: str,
        payload: bytes,
        content_type: str = "application/octet-stream",
        bucket: str = ORIGINAL_SET_BUCKET,
    ) -> None:
        self.client.put_object(
            Bucket=bucket, Key=key, Body=payload, ContentType=content_type
        )

    def get(self, key: str, bucket: str = ORIGINAL_SET_BUCKET) -> bytes | None:
        try:
            return self.client.get_object(Bucket=bucket, Key=key)["Body"].read()
        except ClientError:
            return None

    def get_head(
        self, key: str, length: int, bucket: str = ORIGINAL_SET_BUCKET
    ) -> bytes | None:
        """The first `length` bytes of an object: a file header without the file."""
        try:
            return self.client.get_object(
                Bucket=bucket, Key=key, Range=f"bytes=0-{length - 1}"
            )["Body"].read()
        except ClientError:
            return None

    def delete(self, key: str, bucket: str = ORIGINAL_SET_BUCKET) -> None:
        self.client.delete_object(Bucket=bucket, Key=key)

    # -- spec ------------------------------------------------------------

    def put_spec(self, manual_set_name: str, version: str, yaml_text: str) -> str:
        key = spec_key(manual_set_name, version)
        self.put(key, yaml_text.encode(), "application/yaml", bucket=MANUAL_SET_BUCKET)
        return key

    def get_spec(self, manual_set_name: str, version: str) -> str | None:
        raw = self.get(spec_key(manual_set_name, version), bucket=MANUAL_SET_BUCKET)
        return raw.decode() if raw is not None else None

    def delete_spec(self, manual_set_name: str, version: str) -> None:
        self.delete(spec_key(manual_set_name, version), bucket=MANUAL_SET_BUCKET)

    # -- __meta__.md -----------------------------------------------------

    def put_meta(self, kind: MetaKind, name: str, version: str, markdown: str) -> str:
        """Write a __meta__.md and return where it went, as `bucket/key`."""
        bucket, key = meta_location(kind, name, version)
        self.put(key, markdown.encode(), "text/markdown; charset=utf-8", bucket=bucket)
        return f"{bucket}/{key}"

    def get_meta(self, kind: MetaKind, name: str, version: str) -> str | None:
        bucket, key = meta_location(kind, name, version)
        raw = self.get(key, bucket=bucket)
        return raw.decode() if raw is not None else None

    def delete_meta(self, kind: MetaKind, name: str, version: str) -> None:
        bucket, key = meta_location(kind, name, version)
        self.delete(key, bucket=bucket)

    def list_keys(self, prefix: str, bucket: str = ORIGINAL_SET_BUCKET) -> set[str]:
        """Every object key under a prefix — one listing instead of a HEAD per key."""
        keys: set[str] = set()
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            keys.update(obj["Key"] for obj in page.get("Contents", []))
        return keys

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
