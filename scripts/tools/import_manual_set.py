#!/usr/bin/env python
"""把 cxr-dataset-format 的 parquet 匯入成一個 manual-set 版本。

    python scripts/tools/import_manual_set.py \
        --root /path/to/ChestDatasetsRoot \
        --manual-set CheXpert_train_split \
        --version V1 [--dry-run]

manual-set 不擁有任何資料本體，只記錄「選了哪些」，所以這支腳本做的是
**反查**：拿 parquet 裡的 (source_dataset, source_version, file_name) 去
original-set 找對應的 image / annotation，再寫成員關係。對應的
original-set 必須先匯入。

跟舊版腳本的差別（都是為了符合現行 schema）：
  * 走 SQLAlchemy 與專案的 models，連線設定從 .env 讀
  * 反查標註時如果對到多列會直接失敗，不再默默取第一列——取錯一列
    等於把別人的判讀掛到這份資料集上
  * 匯入前檢查「每張影像都要有標註」——manual-set 是 training-ready 的，
    schema 有 deferred trigger 會擋，這裡先一次列完是哪幾張
  * 不建立 build spec：用 parquet 匯入的版本本來就沒有 spec，
    `cxr why` 會如實回答「這個版本沒有對應的 spec」
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import select

from _common import cell, console, die, read_parquet, summary_table

from cxr_dataset_manager.db import models as m
from cxr_dataset_manager.db.engine import new_session


def _resolve_image(db, dataset: str, version: str, file_name: str) -> int:
    image_id = db.execute(
        select(m.Image.id)
        .join(m.ImageBatch, m.ImageBatch.id == m.Image.image_batch_id)
        .join(m.OriginalSet, m.OriginalSet.id == m.ImageBatch.original_set_id)
        .where(
            m.OriginalSet.name == dataset,
            m.ImageBatch.version == version,
            m.Image.file_name == file_name,
        )
    ).scalar_one_or_none()
    if image_id is None:
        die(
            f"找不到來源影像 {dataset}/{version}/{file_name}。"
            "請先用 import_original_set.py 匯入對應的 original-set。"
        )
    return image_id


def _resolve_category(db, dataset: str, version: str, name: str) -> int:
    category_id = db.execute(
        select(m.Category.id)
        .join(m.AnnotationBatch, m.AnnotationBatch.id == m.Category.annotation_batch_id)
        .join(m.OriginalSet, m.OriginalSet.id == m.AnnotationBatch.original_set_id)
        .where(
            m.OriginalSet.name == dataset,
            m.AnnotationBatch.version == version,
            m.Category.name == name,
        )
    ).scalar_one_or_none()
    if category_id is None:
        die(f"找不到來源類別 {dataset}@{version}:{name}")
    return category_id


def _resolve_annotation(db, model, image_id: int, category_id: int, annotator_id: int,
                        extra_filter, describe: str) -> int:
    """反查一筆原始標註。對到多列就失敗——取錯一列等於偽造標註歸屬。"""
    stmt = select(model.id).where(
        model.image_id == image_id,
        model.category_id == category_id,
        model.annotator_id == annotator_id,
    )
    if extra_filter is not None:
        stmt = stmt.where(extra_filter)
    matches = list(db.execute(stmt).scalars())
    if not matches:
        die(f"反查不到對應的原始標註：{describe}")
    if len(matches) > 1:
        die(
            f"反查到 {len(matches)} 筆同樣的原始標註（id {matches}）：{describe}。"
            "無法判斷該選哪一筆，請先確認來源資料沒有重複列。"
        )
    return matches[0]


def import_manual_set(
    root: Path, manual_set: str, version: str, dry_run: bool = False
) -> None:
    ann_dir = root / "manual-sets" / manual_set / "annotations" / version
    if not ann_dir.is_dir():
        die(f"找不到目錄 {ann_dir}")

    images_df = read_parquet(ann_dir, "images")
    categories_df = read_parquet(ann_dir, "categories")
    annotators_df = read_parquet(ann_dir, "annotators")
    cls_df = read_parquet(ann_dir, "cls_annotations", required=False)
    det_df = read_parquet(ann_dir, "det_annotations", required=False)
    target_df = read_parquet(ann_dir, "target_categories", required=False)
    mapping_df = read_parquet(ann_dir, "category_mappings", required=False)

    # ---- 匯入前的檢查：每張影像都要有標註 ----
    annotated_locals: set = set()
    for df in (cls_df, det_df):
        if df is not None:
            annotated_locals |= set(df["image_id"].unique())
    bare = [
        str(r["file_name"]) for _, r in images_df.iterrows() if r["id"] not in annotated_locals
    ]
    if bare:
        die(
            f"images.parquet 有 {len(bare)} 張影像沒有任何標註"
            f"（{', '.join(bare[:5])}{'…' if len(bare) > 5 else ''}）。"
            "manual-set 是 training-ready 的資料集，schema 不接受沒標註的影像。"
            "請先從 parquet 移除它們，或補上標註。"
        )

    summary_table(
        f"{manual_set}@{version} 準備匯入",
        [
            ("影像", len(images_df)),
            ("類別", len(categories_df)),
            ("標註者", len(annotators_df)),
            ("cls 標註", len(cls_df) if cls_df is not None else 0),
            ("det 標註", len(det_df) if det_df is not None else 0),
            ("target 類別", len(target_df) if target_df is not None else 0),
            ("類別映射", len(mapping_df) if mapping_df is not None else 0),
        ],
    )
    if target_df is None or mapping_df is None:
        console.print(
            "  [yellow]⚠[/] 沒有 target_categories / category_mappings——"
            "這個版本的標註不會有統一的 class name，匯出時只拿得到 local category"
        )
    if dry_run:
        console.print("[yellow]--dry-run：以上只是檢查結果，沒有寫入任何東西[/]")
        return

    db = new_session()
    try:
        ms = db.execute(select(m.ManualSet).filter_by(name=manual_set)).scalar_one_or_none()
        if ms is None:
            ms = m.ManualSet(name=manual_set)
            db.add(ms)
            db.flush()
        if db.execute(
            select(m.ManualSetVersion).filter_by(manual_set_id=ms.id, version=version)
        ).scalar_one_or_none():
            die(
                f"{manual_set}@{version} 已經存在。版本是不可變的記錄，"
                "請換一個版本號，不要覆蓋既有版本。"
            )
        msv = m.ManualSetVersion(manual_set_id=ms.id, version=version)
        db.add(msv)
        db.flush()

        # ---- 影像：反查後寫成員關係 ----
        local_image: dict = {}
        local_source: dict = {}
        for _, r in images_df.iterrows():
            dataset, source_version = str(r["source_dataset"]), str(r["source_version"])
            image_id = _resolve_image(db, dataset, source_version, str(r["file_name"]))
            local_image[r["id"]] = image_id
            local_source[r["id"]] = dataset
            db.add(m.ManualSetImage(manual_set_version_id=msv.id, image_id=image_id))
        db.flush()

        local_category = dict(zip(categories_df["id"], categories_df["name"].astype(str)))
        local_annotator = dict(zip(annotators_df["id"], annotators_df["name"].astype(str)))

        def annotator_id(local_id) -> int:
            name = local_annotator[local_id]
            row = db.execute(select(m.Annotator).filter_by(name=name)).scalar_one_or_none()
            if row is None:
                die(f"找不到標註者 '{name}'，請先匯入對應的 original-set")
            return row.id

        # ---- cls ----
        n_cls = 0
        if cls_df is not None:
            for _, r in cls_df.iterrows():
                image_id = local_image[r["image_id"]]
                dataset = local_source[r["image_id"]]
                source_version = str(r["source_version"])
                category_id = _resolve_category(
                    db, dataset, source_version, local_category[r["category_id"]]
                )
                score = cell(r, "score")
                ann_id = _resolve_annotation(
                    db, m.ClsAnnotation, image_id, category_id, annotator_id(r["annotator_id"]),
                    m.ClsAnnotation.score == (float(score) if score is not None else 0),
                    f"{dataset}@{source_version} image={image_id} "
                    f"category={local_category[r['category_id']]}",
                )
                db.add(
                    m.ManualSetClsAnnotation(
                        manual_set_version_id=msv.id,
                        cls_annotation_id=ann_id,
                        image_id=image_id,
                    )
                )
                n_cls += 1

        # ---- det ----
        n_det = 0
        if det_df is not None:
            for _, r in det_df.iterrows():
                image_id = local_image[r["image_id"]]
                dataset = local_source[r["image_id"]]
                source_version = str(r["source_version"])
                category_id = _resolve_category(
                    db, dataset, source_version, local_category[r["category_id"]]
                )
                bbox = [float(x) for x in r["bbox"]]
                ann_id = _resolve_annotation(
                    db, m.DetAnnotation, image_id, category_id, annotator_id(r["annotator_id"]),
                    m.DetAnnotation.bbox == bbox,
                    f"{dataset}@{source_version} image={image_id} bbox={bbox}",
                )
                db.add(
                    m.ManualSetDetAnnotation(
                        manual_set_version_id=msv.id,
                        det_annotation_id=ann_id,
                        image_id=image_id,
                    )
                )
                n_det += 1
        db.flush()

        # ---- target categories 與映射（scope 綁在這個版本上）----
        n_targets = n_mappings = 0
        target_ids: dict = {}
        if target_df is not None:
            for _, r in target_df.iterrows():
                row = m.ManualSetTargetCategory(
                    manual_set_version_id=msv.id, name=str(r["name"])
                )
                db.add(row)
                db.flush()
                target_ids[r["id"]] = row.id
                n_targets += 1

        if mapping_df is not None and target_ids:
            for _, r in mapping_df.iterrows():
                category_id = _resolve_category(
                    db,
                    str(r["source_dataset"]),
                    str(r["source_version"]),
                    str(r["source_category_name"]),
                )
                db.add(
                    m.ManualSetCategoryMapping(
                        manual_set_version_id=msv.id,
                        category_id=category_id,
                        target_category_id=target_ids[r["target_category_id"]],
                    )
                )
                n_mappings += 1

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    summary_table(
        f"[green]✓[/] {manual_set}@{version} 匯入完成",
        [
            ("影像", len(local_image)),
            ("cls 標註", n_cls),
            ("det 標註", n_det),
            ("target 類別", n_targets),
            ("類別映射", n_mappings),
        ],
    )
    console.print(f"  [dim]cxr show {manual_set}@{version}[/]")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, type=Path, help="ChestDatasetsRoot 路徑")
    parser.add_argument("--manual-set", required=True, help="manual-set 名稱")
    parser.add_argument("--version", required=True, help="annotations/{版本}")
    parser.add_argument("--dry-run", action="store_true", help="只檢查，不寫入")
    args = parser.parse_args()
    import_manual_set(args.root, args.manual_set, args.version, args.dry_run)


if __name__ == "__main__":
    main()
