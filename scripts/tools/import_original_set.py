#!/usr/bin/env python
"""把 cxr-dataset-format 的 parquet 匯入成一個 original-set 的 annotation batch。

    python scripts/tools/import_original_set.py \
        --root /path/to/ChestDatasetsRoot \
        --dataset CheXpert \
        --ann-version V1 [--dry-run]

讀取 {root}/original-sets/{dataset}/annotations/{ann-version}/ 底下的
licenses / annotators / categories / images / cls_annotations /
det_annotations（後兩者至少要有一個）。

影像屬於哪個 image_batch 由 images.parquet 的 source_version 決定——
annotation batch 與 image batch 是兩條獨立的版本軸，一批標註可以橫跨
多個影像批次。

跟舊版腳本的差別（都是為了符合現行 schema）：
  * 走 SQLAlchemy 與專案的 models，連線設定從 .env 讀，不再寫死 DSN
  * 支援 image_subjects（病患識別，切割避免 leakage 用）
  * 匯入前先檢查「category 不可 dangling」——schema 有 deferred trigger
    會在 COMMIT 時擋，但那時只講得出第一個出問題的，這裡先一次列完
  * blake3_hash 不合格式就存 NULL，而不是讓整批 INSERT 失敗
  * 預設拒絕重複匯入同一個 annotation batch，除非 --replace
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select

from _common import cell, clean_date, clean_hash, console, die, read_parquet, summary_table

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


def import_original_set(
    root: Path, dataset: str, ann_version: str, dry_run: bool = False, replace: bool = False
) -> None:
    ann_dir = root / "original-sets" / dataset / "annotations" / ann_version
    if not ann_dir.is_dir():
        die(f"找不到目錄 {ann_dir}")

    images_df = read_parquet(ann_dir, "images")
    categories_df = read_parquet(ann_dir, "categories")
    annotators_df = read_parquet(ann_dir, "annotators")
    licenses_df = read_parquet(ann_dir, "licenses", required=False)
    cls_df = read_parquet(ann_dir, "cls_annotations", required=False)
    det_df = read_parquet(ann_dir, "det_annotations", required=False)

    if cls_df is None and det_df is None:
        die("cls_annotations.parquet 與 det_annotations.parquet 至少要有一個")

    # ---- 匯入前的檢查：category 不可 dangling ----------------------------
    used_categories: set = set()
    for df in (cls_df, det_df):
        if df is not None:
            used_categories |= set(df["category_id"].unique())
    declared = set(categories_df["id"])
    dangling = declared - used_categories
    if dangling:
        names = [
            str(cell(r, "name"))
            for _, r in categories_df.iterrows()
            if r["id"] in dangling
        ]
        die(
            f"categories.parquet 有 {len(dangling)} 個類別沒有任何標註引用："
            f"{', '.join(names[:10])}"
            + ("…" if len(names) > 10 else "")
            + "。schema 不允許 dangling category（COMMIT 時會被 trigger 擋），"
            "請先從 parquet 移除它們，或補上引用它們的標註。"
        )

    has_subject = "subject_id" in images_df.columns
    batches_used = sorted(set(images_df["source_version"].astype(str)))

    summary_table(
        f"{dataset}@{ann_version} 準備匯入",
        [
            ("影像", len(images_df)),
            ("  分屬 image batch", ", ".join(batches_used)),
            ("  帶 subject_id", int(images_df["subject_id"].notna().sum()) if has_subject else "（無此欄位）"),
            ("類別", len(categories_df)),
            ("標註者", len(annotators_df)),
            ("授權", len(licenses_df) if licenses_df is not None else 0),
            ("cls 標註", len(cls_df) if cls_df is not None else 0),
            ("det 標註", len(det_df) if det_df is not None else 0),
        ],
    )
    if not has_subject:
        console.print(
            "  [yellow]⚠[/] images.parquet 沒有 subject_id 欄位——"
            "這批影像做切割時無法保證病患層級不洩漏"
        )
    if dry_run:
        console.print("[yellow]--dry-run：以上只是檢查結果，沒有寫入任何東西[/]")
        return

    db = new_session()
    try:
        original_set = _get_or_create(db, m.OriginalSet, name=dataset)
        existing = db.execute(
            select(m.AnnotationBatch).filter_by(
                original_set_id=original_set.id, version=ann_version
            )
        ).scalar_one_or_none()
        if existing is not None:
            count = db.execute(
                select(m.ClsAnnotation).filter_by(annotation_batch_id=existing.id).limit(1)
            ).first()
            if count and not replace:
                die(
                    f"{dataset}@{ann_version} 這個 annotation batch 已經有資料了。"
                    "重跑會產生重複的標註（標註沒有自然唯一鍵）。"
                    "確定要重來請加 --replace，它會先刪掉這個 batch 的既有標註。"
                )
            if replace:
                db.execute(
                    m.ClsAnnotation.__table__.delete().where(
                        m.ClsAnnotation.annotation_batch_id == existing.id
                    )
                )
                db.execute(
                    m.DetAnnotation.__table__.delete().where(
                        m.DetAnnotation.annotation_batch_id == existing.id
                    )
                )
                db.execute(
                    m.Category.__table__.delete().where(
                        m.Category.annotation_batch_id == existing.id
                    )
                )
                db.flush()
        annotation_batch = existing or _get_or_create(
            db, m.AnnotationBatch, original_set_id=original_set.id, version=ann_version
        )

        # ---- licenses（全域詞彙表）----
        license_map: dict = {}
        if licenses_df is not None:
            for _, r in licenses_df.iterrows():
                row = _get_or_create(
                    db, m.License, defaults={"url": cell(r, "url")}, name=str(r["name"])
                )
                license_map[r["id"]] = row.id

        # ---- annotators（全域名冊）----
        annotator_map: dict = {}
        for _, r in annotators_df.iterrows():
            annotator_map[r["id"]] = _get_or_create(db, m.Annotator, name=str(r["name"])).id

        # ---- categories（scope 在這個 annotation batch 底下）----
        category_map: dict = {}
        for _, r in categories_df.iterrows():
            row = m.Category(
                annotation_batch_id=annotation_batch.id,
                name=str(r["name"]),
                supercategory=cell(r, "supercategory"),
            )
            db.add(row)
            db.flush()
            category_map[r["id"]] = row.id

        # ---- image batches + images ----
        image_batches: dict[str, int] = {}
        for version in batches_used:
            image_batches[version] = _get_or_create(
                db, m.ImageBatch, original_set_id=original_set.id, version=version
            ).id

        image_map: dict = {}
        new_images = new_subjects = 0
        for _, r in images_df.iterrows():
            batch_id = image_batches[str(r["source_version"])]
            existing_image = db.execute(
                select(m.Image).filter_by(
                    image_batch_id=batch_id, file_name=str(r["file_name"])
                )
            ).scalar_one_or_none()
            if existing_image is None:
                license_key = cell(r, "license")
                existing_image = m.Image(
                    image_batch_id=batch_id,
                    file_name=str(r["file_name"]),
                    height=int(r["height"]),
                    width=int(r["width"]),
                    blake3_hash=clean_hash(cell(r, "blake3_hash")),
                    license_id=license_map.get(license_key),
                    date_captured=clean_date(cell(r, "date_captured")),
                )
                db.add(existing_image)
                db.flush()
                new_images += 1
            image_map[r["id"]] = existing_image.id

            subject = cell(r, "subject_id")
            if subject and db.get(m.ImageSubject, existing_image.id) is None:
                db.add(m.ImageSubject(image_id=existing_image.id, subject_id=str(subject)))
                new_subjects += 1
        db.flush()

        # ---- annotations ----
        cls_rows = []
        if cls_df is not None:
            for _, r in cls_df.iterrows():
                cls_rows.append(
                    {
                        "annotation_batch_id": annotation_batch.id,
                        "image_id": image_map[r["image_id"]],
                        "category_id": category_map[r["category_id"]],
                        "score": float(cell(r, "score", 0) or 0),
                        "annotator_id": annotator_map[r["annotator_id"]],
                    }
                )
            db.bulk_insert_mappings(m.ClsAnnotation, cls_rows)

        det_rows = []
        if det_df is not None:
            for _, r in det_df.iterrows():
                bbox = [float(x) for x in r["bbox"]]
                if len(bbox) != 4:
                    die(f"det_annotations 的 bbox 必須是 4 個數字，收到 {bbox}")
                segmentation = cell(r, "segmentation")
                if segmentation is not None and hasattr(segmentation, "tolist"):
                    segmentation = segmentation.tolist()
                score = cell(r, "score")
                det_rows.append(
                    {
                        "annotation_batch_id": annotation_batch.id,
                        "image_id": image_map[r["image_id"]],
                        "category_id": category_map[r["category_id"]],
                        "bbox": bbox,
                        "segmentation": segmentation,
                        "iscrowd": int(cell(r, "iscrowd", 0) or 0),
                        "score": float(score) if score is not None else None,
                        "annotator_id": annotator_map[r["annotator_id"]],
                    }
                )
            db.bulk_insert_mappings(m.DetAnnotation, det_rows)

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    summary_table(
        f"[green]✓[/] {dataset}@{ann_version} 匯入完成",
        [
            ("新增影像", new_images),
            ("既有影像（沿用）", len(image_map) - new_images),
            ("新增 image_subjects", new_subjects),
            ("類別", len(category_map)),
            ("cls 標註", len(cls_rows)),
            ("det 標註", len(det_rows)),
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, type=Path, help="ChestDatasetsRoot 路徑")
    parser.add_argument("--dataset", required=True, help="original-set 名稱")
    parser.add_argument("--ann-version", required=True, help="annotations/{版本}")
    parser.add_argument("--dry-run", action="store_true", help="只檢查，不寫入")
    parser.add_argument("--replace", action="store_true",
                        help="這個 annotation batch 已有資料時，先刪掉再匯入")
    args = parser.parse_args()
    import_original_set(args.root, args.dataset, args.ann_version, args.dry_run, args.replace)


if __name__ == "__main__":
    main()
