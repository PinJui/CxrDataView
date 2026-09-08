"""全域設定。

只有「真的會因部署環境而異」的東西才是環境變數；bucket 名稱之類的
架構常數直接寫死在這裡。所有變數用 CXR_ 前綴，可放在專案根目錄的 .env。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

# Bucket 命名是系統架構的一部分，不隨部署環境改變。
ORIGINAL_SET_BUCKET = "original-sets"
MANUAL_SET_BUCKET = "manual-sets"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CXR_", extra="ignore")

    # docker/docker-compose.yml 把 postgres 對外開在 5433
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5433/cxr"

    s3_endpoint_url: Optional[str] = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_access_key_id: Optional[str] = "minioadmin"
    s3_secret_access_key: Optional[str] = "minioadmin"
    presigned_url_expire_seconds: int = 3600

    # 誰建的資料集。設在 .env 就不用每次 commit 都打；沒設的話 CLI 會問。
    author_name: Optional[str] = None
    author_email: Optional[str] = None

    # 影像實際不存在物件儲存時（例如只匯入了 metadata），UI 顯示佔位圖而非壞掉的 <img>
    strict_object_storage: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
