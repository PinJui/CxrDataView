#!/usr/bin/env python
"""補算 images.blake3_hash 為 NULL 的影像。

    python scripts/tools/backfill_blake3.py                    # 從物件儲存讀
    python scripts/tools/backfill_blake3.py --root /data/cxr   # 從本機目錄讀
    python scripts/tools/backfill_blake3.py --dry-run --limit 100

blake3_hash 是去重的唯一依據——沒有它，`dedup` 就完全看不到那張影像的重複。
從 parquet 匯入的資料常常沒有這個欄位（或格式不合被存成 NULL），這支就是
用來把它補起來。

跟舊版腳本的差別：
  * 預設從物件儲存讀（影像本體現在存在 MinIO/S3，不是本機檔案系統）；
    要讀本機目錄仍然可以用 --root，路徑規則跟規格一致
  * 走 SQLAlchemy 與專案的 models，連線設定從 .env 讀
  * 用 id 游標分批推進，而不是 server-side cursor——失敗的影像 hash 仍是
    NULL，用「WHERE hash IS NULL」重查會一直撈到同一批，游標不會

保留的設計（這些理由現在仍然成立）：
  * 冪等：只處理 blake3_hash IS NULL 的列，中途被 kill 可以直接重跑
  * 每批 commit 一次，不是全部做完才 commit
  * 單張失敗只記錄不中斷，失敗清單寫成 JSON Lines 方便事後分析
  * 核心是一個純函式，可以被 Airflow 的 PythonOperator 直接呼叫
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import blake3
from sqlalchemy import select, text

from _common import console, die, summary_table

from cxr_dataset_manager.db import models as m
from cxr_dataset_manager.db.engine import new_session
from cxr_dataset_manager.storage import get_store, object_key_for_image

CHUNK_SIZE = 1024 * 1024  # 讀檔用，避免大檔一次整包進記憶體


def _hash_bytes(payload: bytes) -> str:
    return blake3.blake3(payload).hexdigest()


def _hash_file(path: Path) -> str:
    hasher = blake3.blake3()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def _make_reader(root: Optional[Path]) -> Callable[[str, str, str], str]:
    """回傳 (original_set, version, file_name) -> hex digest。"""
    if root is not None:
        def read_local(original_set: str, version: str, file_name: str) -> str:
            path = root / "original-sets" / original_set / "images" / version / file_name
            if not path.exists():
                raise FileNotFoundError(str(path))
            return _hash_file(path)

        return read_local

    store = get_store()
    if not store.alive():
        die("連不上物件儲存。用 --root 指定本機影像目錄，或檢查 CXR_S3_ENDPOINT_URL。")

    def read_object(original_set: str, version: str, file_name: str) -> str:
        key = object_key_for_image(original_set, version, file_name)
        payload = store.get(key)
        if payload is None:
            raise FileNotFoundError(key)
        return _hash_bytes(payload)

    return read_object


def backfill_blake3(
    root: Optional[Path] = None,
    limit: Optional[int] = None,
    batch_size: int = 200,
    dry_run: bool = False,
    failure_log: Optional[Path] = None,
) -> dict[str, Any]:
    """回傳 summary dict，可以直接當 Airflow 的 XCom 值。

    單張失敗不會讓整支中斷——回傳值裡的 failed 數字交給呼叫端決定
    要不要讓整個 DAG run 標記失敗。
    """
    read = None if dry_run else _make_reader(root)
    db = new_session()
    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source": "local" if root else "object_store",
        "total_pending": 0,
        "updated": 0,
        "failed": 0,
        "dry_run": dry_run,
    }
    failures: list[dict[str, Any]] = []

    try:
        summary["total_pending"] = db.execute(
            text("SELECT count(*) FROM images WHERE blake3_hash IS NULL")
        ).scalar_one()
        if summary["total_pending"] == 0:
            console.print("[green]✓[/] 沒有 blake3_hash 為 NULL 的影像，不用補算")
            return summary
        if dry_run:
            console.print(
                f"[yellow]--dry-run[/]：有 {summary['total_pending']:,} 張影像待補算，未讀取任何檔案"
            )
            return summary

        cursor = 0
        processed = 0
        while True:
            rows = db.execute(
                select(
                    m.Image.id, m.Image.file_name, m.OriginalSet.name, m.ImageBatch.version
                )
                .join(m.ImageBatch, m.ImageBatch.id == m.Image.image_batch_id)
                .join(m.OriginalSet, m.OriginalSet.id == m.ImageBatch.original_set_id)
                # 用 id 游標推進：失敗的影像 hash 仍是 NULL，
                # 光靠 "WHERE hash IS NULL" 會一直撈到同一批
                .where(m.Image.blake3_hash.is_(None), m.Image.id > cursor)
                .order_by(m.Image.id)
                .limit(batch_size)
            ).all()
            if not rows:
                break

            updates = []
            for image_id, file_name, original_set, version in rows:
                cursor = image_id
                processed += 1
                try:
                    updates.append({"id": image_id, "blake3_hash": read(original_set, version, file_name)})
                except Exception as exc:  # 單張失敗不中斷
                    summary["failed"] += 1
                    failures.append(
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "image_id": image_id,
                            "ref": f"{original_set}/{version}/{file_name}",
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                        }
                    )
                if limit and processed >= limit:
                    break

            if updates:
                db.bulk_update_mappings(m.Image, updates)
                db.commit()  # 每批就 commit，中途被 kill 也不會白做
                summary["updated"] += len(updates)
            console.print(
                f"  已處理 {processed:,}/{summary['total_pending']:,}"
                f"（成功 {summary['updated']:,}、失敗 {summary['failed']:,}）",
                end="\r",
            )
            if limit and processed >= limit:
                break
    finally:
        db.close()
        console.print()

    if failures and failure_log:
        failure_log.parent.mkdir(parents=True, exist_ok=True)
        with open(failure_log, "a", encoding="utf-8") as fh:
            for record in failures:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=None,
                        help="從本機目錄讀影像；不給就從物件儲存讀")
    parser.add_argument("--limit", type=int, default=None, help="這次最多處理幾張")
    parser.add_argument("--batch-size", type=int, default=200, help="每批處理並 commit 幾張")
    parser.add_argument("--dry-run", action="store_true", help="只回報待處理數量")
    parser.add_argument("--failure-log", type=Path,
                        default=Path("logs/blake3_failures.jsonl"),
                        help="失敗紀錄（JSON Lines）")
    args = parser.parse_args()

    summary = backfill_blake3(
        root=args.root, limit=args.limit, batch_size=args.batch_size,
        dry_run=args.dry_run, failure_log=args.failure_log,
    )
    summary_table(
        "補算結果",
        [
            ("來源", summary["source"]),
            ("待處理", summary["total_pending"]),
            ("已補算", summary["updated"]),
            ("失敗", summary["failed"]),
        ],
    )
    if summary["failed"]:
        console.print(f"  [yellow]失敗清單寫在 {args.failure_log}[/]")


if __name__ == "__main__":
    main()
