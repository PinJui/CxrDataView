import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from cxr_dataset_manager.db import models as m
from cxr_dataset_manager.db.engine import new_session


def _get_or_create(db, model, defaults=None, **keys):
    row = db.execute(select(model).filter_by(**keys)).scalar_one_or_none()
    if row is not None:
        return row
    row = model(**keys, **(defaults or {}))
    db.add(row)
    db.flush()
    return row


def to_py_type(val):
    """將 pandas/numpy 型態轉回 python 原生型態以利 SQLAlchemy 寫入 DB"""
    if isinstance(val, np.ndarray):
        return val.tolist()
    if pd.isna(val):
        return None
    return val


def main():
    parser = argparse.ArgumentParser(
        description="Import annotation batch from parquet files into the database."
    )
    parser.add_argument(
        "-d",
        "--dir",
        required=True,
        help="Directory containing the annotation Parquet files",
    )
    parser.add_argument("-s", "--set-name", required=True, help="Original-set name")
    parser.add_argument(
        "-v",
        "--batch-version",
        required=True,
        help="Annotation batch version (e.g., V1)",
    )
    parser.add_argument(
        "--on-missing",
        choices=["rollback", "skip"],
        default="rollback",
        help="Action when an image referenced in images.parquet does not exist in DB (default: rollback)",
    )
    parser.add_argument(
        "--on-duplicate",
        choices=["rollback", "skip"],
        default="rollback",
        help="Action when duplicate annotations are detected (default: rollback)",
    )

    args = parser.parse_args()
    folder_path = Path(args.dir)

    if not folder_path.is_dir():
        print(f"[ERROR] Directory {folder_path} does not exist.")
        return

    # 安全地讀取 Parquet 檔案
    def load_parquet(filename):
        fpath = folder_path / filename
        return pd.read_parquet(fpath) if fpath.exists() else None

    df_img = load_parquet("images.parquet")
    df_cls = load_parquet("cls_annotations.parquet")
    df_det = load_parquet("det_annotations.parquet")
    df_cat = load_parquet("categories.parquet")
    df_lic = load_parquet("licenses.parquet")
    df_ann = load_parquet("annotators.parquet")

    if df_img is None:
        print("[ERROR] images.parquet is required but not found in the directory.")
        return

    db = new_session()

    try:
        # 1. 取得或建立 OriginalSet 與 AnnotationBatch
        original_set = _get_or_create(db, m.OriginalSet, name=args.set_name)
        ann_batch = _get_or_create(
            db,
            m.AnnotationBatch,
            original_set_id=original_set.id,
            version=args.batch_version,
        )

        license_map = {}  # parquet_id -> db_license_id
        annotator_map = {}  # parquet_id -> db_annotator_id
        category_map = {}  # parquet_id -> db_category_id
        image_map = {}  # parquet_id -> db_image_id

        # 2. 處理全域 Licenses
        if df_lic is not None:
            for _, row in df_lic.iterrows():
                db_lic = _get_or_create(
                    db, m.License, name=row["name"], defaults={"url": row.get("url")}
                )
                license_map[int(row["id"])] = db_lic.id

        # 3. 處理全域 Annotators
        if df_ann is not None:
            for _, row in df_ann.iterrows():
                db_ann = _get_or_create(db, m.Annotator, name=row["name"])
                annotator_map[int(row["id"])] = db_ann.id

        # 4. 處理影像 (檢查是否存在 & 同步中繼資料)
        print("[INFO] Checking images and updating metadata...")
        missing_count = 0
        skipped_image_pids = set()  # 紀錄因缺失被略過的 parquet image id
        image_update_dicts = []

        # 按照 source_version (對應 image_batch.version) 批次查詢以增進效能
        for src_version, group in df_img.groupby("source_version"):
            # 找到對應的 image_batch_id
            batch_id = db.execute(
                select(m.ImageBatch.id)
                .where(m.ImageBatch.original_set_id == original_set.id)
                .where(m.ImageBatch.version == src_version)
            ).scalar_one_or_none()

            if not batch_id:
                missing_count += len(group)
                skipped_image_pids.update(group["id"].tolist())
                continue

            # 抓出此 batch 下資料庫實際存在的所有指定檔名
            file_names = group["file_name"].tolist()
            existing_imgs = db.execute(
                select(m.Image.id, m.Image.file_name)
                .where(m.Image.image_batch_id == batch_id)
                .where(m.Image.file_name.in_(file_names))
            ).all()

            db_img_map = {r.file_name: r.id for r in existing_imgs}

            for _, row in group.iterrows():
                fname = row["file_name"]
                pid = int(row["id"])

                if fname not in db_img_map:
                    missing_count += 1
                    skipped_image_pids.add(pid)
                    continue

                db_id = db_img_map[fname]
                image_map[pid] = db_id

                # 準備更新中繼資料 (License, Date Captured)
                update_data = {"id": db_id}
                needs_update = False

                if "license" in row and not pd.isna(row["license"]):
                    db_lic_id = license_map.get(int(row["license"]))
                    if db_lic_id:
                        update_data["license_id"] = db_lic_id
                        needs_update = True

                if "date_captured" in row and not pd.isna(row["date_captured"]):
                    dc = str(row["date_captured"]).strip()
                    # 資料庫 Date 欄位不接受 0000-00-00，直接略過保留 NULL
                    if dc and dc != "0000-00-00":
                        update_data["date_captured"] = dc
                        needs_update = True

                if needs_update:
                    image_update_dicts.append(update_data)

        # 處理缺失影像的原則
        if missing_count > 0:
            if args.on_missing == "rollback":
                raise RuntimeError(
                    f"Found {missing_count} referenced images that do not exist in the DB. Rollback requested."
                )
            else:
                print(
                    f"[WARNING] {missing_count} referenced images are missing in the DB. They will be skipped."
                )

        # 批次執行中繼資料更新
        if image_update_dicts:
            db.bulk_update_mappings(m.Image, image_update_dicts)

        # 5. 處理 Categories (避免建立懸空 Dangling Categories)
        if df_cat is not None:
            # 只抽出「沒有被跳過影像」所實際關聯到的 category ids
            used_category_pids = set()
            if df_cls is not None:
                valid_cls = df_cls[~df_cls["image_id"].isin(skipped_image_pids)]
                used_category_pids.update(
                    valid_cls["category_id"].dropna().astype(int).tolist()
                )
            if df_det is not None:
                valid_det = df_det[~df_det["image_id"].isin(skipped_image_pids)]
                used_category_pids.update(
                    valid_det["category_id"].dropna().astype(int).tolist()
                )

            for _, row in df_cat.iterrows():
                pid = int(row["id"])
                if pid not in used_category_pids:
                    continue  # 若為懸空 category，不寫入 DB 避免觸發 Deferred Trigger 阻擋 Commit

                db_cat = _get_or_create(
                    db,
                    m.Category,
                    annotation_batch_id=ann_batch.id,
                    name=row["name"],
                    defaults={"supercategory": row.get("supercategory")},
                )
                category_map[pid] = db_cat.id

        # 6. 處理 Classification Annotations
        if df_cls is not None and not df_cls.empty:
            print("[INFO] Processing classification annotations...")
            cls_records = []
            for _, row in df_cls.iterrows():
                pid = int(row["image_id"])
                if pid in skipped_image_pids:
                    continue

                cls_records.append(
                    {
                        "annotation_batch_id": ann_batch.id,
                        "image_id": image_map[pid],
                        "category_id": category_map[int(row["category_id"])],
                        "score": float(row["score"])
                        if not pd.isna(row.get("score"))
                        else 0.0,
                        "annotator_id": annotator_map[int(row["annotator_id"])],
                    }
                )

            if cls_records:
                stmt = insert(m.ClsAnnotation).values(cls_records)
                if args.on_duplicate == "skip":
                    # ON CONFLICT DO NOTHING (PostgreSQL支援)
                    stmt = stmt.on_conflict_do_nothing()
                    res = db.execute(stmt)
                    inserted = res.rowcount
                    if inserted < len(cls_records):
                        print(
                            f"[INFO] Skipped {len(cls_records) - inserted} duplicate classification annotations."
                        )
                else:
                    try:
                        db.execute(stmt)
                    except IntegrityError:
                        raise RuntimeError(
                            "Duplicate classification annotations found and --on-duplicate is set to rollback."
                        )

        # 7. 處理 Detection Annotations
        if df_det is not None and not df_det.empty:
            print("[INFO] Processing detection annotations...")
            det_records = []
            for _, row in df_det.iterrows():
                pid = int(row["image_id"])
                if pid in skipped_image_pids:
                    continue

                bbox = to_py_type(row.get("bbox"))
                if bbox is None or len(bbox) != 4:
                    continue  # bbox 為 required

                score = to_py_type(row.get("score"))
                if score is not None and (pd.isna(score) or math.isnan(score)):
                    score = None

                det_records.append(
                    {
                        "annotation_batch_id": ann_batch.id,
                        "image_id": image_map[pid],
                        "category_id": category_map[int(row["category_id"])],
                        "bbox": bbox,
                        "segmentation": to_py_type(row.get("segmentation")),
                        "iscrowd": int(row.get("iscrowd", 0))
                        if not pd.isna(row.get("iscrowd"))
                        else 0,
                        "score": float(score) if score is not None else None,
                        "annotator_id": annotator_map[int(row["annotator_id"])],
                    }
                )

            if det_records:
                stmt = insert(m.DetAnnotation).values(det_records)
                if args.on_duplicate == "skip":
                    stmt = stmt.on_conflict_do_nothing()
                    res = db.execute(stmt)
                    inserted = res.rowcount
                    if inserted < len(det_records):
                        print(
                            f"[INFO] Skipped {len(det_records) - inserted} duplicate detection annotations."
                        )
                else:
                    try:
                        db.execute(stmt)
                    except IntegrityError:
                        raise RuntimeError(
                            "Duplicate detection annotations found and --on-duplicate is set to rollback."
                        )

        # ==========================================
        # Commit (延遲觸發器會在此刻檢查所有的 Dangling 規則)
        # ==========================================
        db.commit()
        print("\n[INFO] Annotation batch import operation completed successfully!")
        print(
            "[INFO] Next: describe this batch with "
            f"`cxr meta annotations {args.set_name}@{args.batch_version}`"
        )

    except BaseException as e:
        # 攔截包含 Exception 以及 KeyboardInterrupt (Ctrl+C) 在內的所有例外
        db.rollback()

        if isinstance(e, KeyboardInterrupt):
            print(
                "\n[INFO] User keyboard interrupt detected (Ctrl+C), DB writing cancelled (Rollback)."
            )
        else:
            print(f"\n[ERROR] {e} (DB Rollback Done)")

    finally:
        db.close()


if __name__ == "__main__":
    main()
