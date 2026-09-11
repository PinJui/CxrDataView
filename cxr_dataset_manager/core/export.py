"""把 manual-set 版本匯出成訓練用的格式。

資料來源是 manual_set_* 成員關係表——那已經是「乾淨、映射完成、
衝突解決過」的結果，匯出時不用重跑任何去重／映射邏輯，
直接用 target category 當 class name。
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from cxr_dataset_manager.core.meta import category_ids
from cxr_dataset_manager.core.types import SpecError
from cxr_dataset_manager.db import crud
from cxr_dataset_manager.storage import get_store


def _rows(db: Session, version_id: int) -> tuple[list[dict], list[dict], list[dict]]:
    images = (
        db.execute(
            text(
                """
            SELECT i.id, i.file_name, i.width, i.height, i.blake3_hash, i.date_captured,
                   os.name AS original_set, ib.version AS batch_version,
                   s.subject_id, l.name AS license
            FROM manual_set_images msi
            JOIN images i ON i.id = msi.image_id
            JOIN image_batches ib ON ib.id = i.image_batch_id
            JOIN original_sets os ON os.id = ib.original_set_id
            LEFT JOIN image_subjects s ON s.image_id = i.id
            LEFT JOIN licenses l ON l.id = i.license_id
            WHERE msi.manual_set_version_id = :vid
            ORDER BY i.id
            """
            ),
            {"vid": version_id},
        )
        .mappings()
        .all()
    )

    cls = (
        db.execute(
            text(
                """
            SELECT a.id, a.image_id, a.score, tc.name AS target_category,
                   c.name AS local_category, an.name AS annotator,
                   os.name || '@' || ab.version AS source, ab.version AS batch_version
            FROM manual_set_cls_annotations msa
            JOIN cls_annotations a ON a.id = msa.cls_annotation_id
            JOIN categories c ON c.id = a.category_id
            JOIN annotation_batches ab ON ab.id = a.annotation_batch_id
            JOIN original_sets os ON os.id = ab.original_set_id
            JOIN annotators an ON an.id = a.annotator_id
            LEFT JOIN manual_set_category_mappings cm
              ON cm.manual_set_version_id = msa.manual_set_version_id
             AND cm.category_id = a.category_id
            LEFT JOIN manual_set_target_categories tc ON tc.id = cm.target_category_id
            WHERE msa.manual_set_version_id = :vid
            ORDER BY a.id
            """
            ),
            {"vid": version_id},
        )
        .mappings()
        .all()
    )

    det = (
        db.execute(
            text(
                """
            SELECT a.id, a.image_id, a.bbox, a.segmentation, a.iscrowd, a.score,
                   tc.name AS target_category,
                   c.name AS local_category, an.name AS annotator,
                   os.name || '@' || ab.version AS source, ab.version AS batch_version
            FROM manual_set_det_annotations msa
            JOIN det_annotations a ON a.id = msa.det_annotation_id
            JOIN categories c ON c.id = a.category_id
            JOIN annotation_batches ab ON ab.id = a.annotation_batch_id
            JOIN original_sets os ON os.id = ab.original_set_id
            JOIN annotators an ON an.id = a.annotator_id
            LEFT JOIN manual_set_category_mappings cm
              ON cm.manual_set_version_id = msa.manual_set_version_id
             AND cm.category_id = a.category_id
            LEFT JOIN manual_set_target_categories tc ON tc.id = cm.target_category_id
            WHERE msa.manual_set_version_id = :vid
            ORDER BY a.id
            """
            ),
            {"vid": version_id},
        )
        .mappings()
        .all()
    )

    return [dict(r) for r in images], [dict(r) for r in cls], [dict(r) for r in det]


def to_coco(db: Session, version_id: int) -> dict[str, Any]:
    summary = crud.version_summary(db, version_id)
    images, cls, det = _rows(db, version_id)

    cat_ids = category_ids(
        r["target_category"] for r in cls + det if r["target_category"]
    )

    coco: dict[str, Any] = {
        "info": {
            "description": f"{summary['manual_set']}@{summary['version']}",
            "spec_sha256": summary["spec_sha256"],
            "date_created": summary["created_at"].isoformat()
            if summary["created_at"]
            else None,
        },
        "images": [
            {
                "id": r["id"],
                "file_name": f"{r['original_set']}/images/{r['batch_version']}/{r['file_name']}",
                "width": r["width"],
                "height": r["height"],
                "date_captured": r["date_captured"].isoformat()
                if r["date_captured"]
                else None,
                "license": r["license"],
                # 非 COCO 標準欄位，但下游做 patient-level split 一定會用到
                "subject_id": r["subject_id"],
                "blake3_hash": r["blake3_hash"],
            }
            for r in images
        ],
        "categories": [{"id": cid, "name": name} for name, cid in cat_ids.items()],
        "annotations": [
            {
                "id": r["id"],
                "image_id": r["image_id"],
                "category_id": cat_ids[r["target_category"]],
                "bbox": [float(x) for x in r["bbox"]],
                "area": float(r["bbox"][2]) * float(r["bbox"][3]),
                "iscrowd": r["iscrowd"],
                "score": float(r["score"]) if r["score"] is not None else None,
                "annotator": r["annotator"],
            }
            for r in det
            if r["target_category"]
        ],
        # cls 標註在 COCO 沒有標準位置，放進獨立欄位而不是硬塞成 bbox=全圖
        "cls_annotations": [
            {
                "id": r["id"],
                "image_id": r["image_id"],
                "category_id": cat_ids[r["target_category"]],
                "category_name": r["target_category"],
                "score": float(r["score"]) if r["score"] is not None else None,
                "annotator": r["annotator"],
                "source": r["source"],
            }
            for r in cls
            if r["target_category"]
        ],
    }
    return coco


def to_manifest_csv(db: Session, version_id: int) -> str:
    """一張圖一列的扁平清單——最常被下游 dataloader 直接吃掉的格式。"""
    images, cls, det = _rows(db, version_id)
    labels: dict[int, list[str]] = {}
    for r in cls:
        if r["target_category"]:
            labels.setdefault(r["image_id"], []).append(r["target_category"])

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "image_id",
            "object_key",
            "original_set",
            "batch_version",
            "file_name",
            "width",
            "height",
            "subject_id",
            "date_captured",
            "blake3_hash",
            "labels",
            "n_boxes",
        ]
    )
    box_counts: dict[int, int] = {}
    for r in det:
        box_counts[r["image_id"]] = box_counts.get(r["image_id"], 0) + 1
    for r in images:
        writer.writerow(
            [
                r["id"],
                f"{r['original_set']}/images/{r['batch_version']}/{r['file_name']}",
                r["original_set"],
                r["batch_version"],
                r["file_name"],
                r["width"],
                r["height"],
                r["subject_id"] or "",
                r["date_captured"].isoformat() if r["date_captured"] else "",
                r["blake3_hash"] or "",
                "|".join(sorted(labels.get(r["id"], []))),
                box_counts.get(r["id"], 0),
            ]
        )
    return buf.getvalue()


def to_zip(db: Session, version_id: int, include_spec: bool = True) -> bytes:
    """COCO json + manifest csv + 產生它的 spec，打包成一份可交付的東西。"""
    summary = crud.version_summary(db, version_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "annotations_coco.json",
            json.dumps(to_coco(db, version_id), indent=2, default=str),
        )
        z.writestr("manifest.csv", to_manifest_csv(db, version_id))
        if include_spec and summary["spec_yaml"]:
            # 逐位元組照抄物件儲存上的那份，不重新序列化——匯出包裡的 spec
            # 必須跟 spec_sha256 對得起來，否則這個檔案沒有意義。
            z.writestr("build_spec.yaml", summary["spec_yaml"])
        z.writestr(
            "README.txt",
            f"""{summary["manual_set"]}@{summary["version"]}
created: {summary["created_at"]}
spec sha256: {summary["spec_sha256"] or "— (an imported version, no spec)"}
images: {summary["images"]}  cls annotations: {summary["cls"]}  det annotations: {summary["det"]}

annotations_coco.json  COCO format; class names are this version's own target categories
manifest.csv           one row per image, with subject_id for patient-level splits
build_spec.yaml        the spec that produced this data; re-running it reproduces the result

The images themselves are not in this package; fetch them from the original-sets
bucket using the object_key column of manifest.csv.
""",
        )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# manual-set parquet: the dataset format's own layout
# ---------------------------------------------------------------------------

PARQUET_FILES = (
    "images",
    "cls_annotations",
    "det_annotations",
    "categories",
    "annotators",
)


def _polygons(value: Any, annotation_id: int) -> list[list[float]] | None:
    """Segmentation as a list of polygons, the one shape the parquet schema holds."""
    if value is None:
        return None
    if isinstance(value, list) and all(isinstance(p, list) for p in value):
        return [[float(x) for x in polygon] for polygon in value]
    if isinstance(value, list) and all(isinstance(x, (int, float)) for x in value):
        return [[float(x) for x in value]]
    raise SpecError(
        f"det annotation #{annotation_id} has a segmentation that is not a list of "
        "polygons (RLE?); the manual-set parquet format cannot hold it"
    )


def parquet_dir(out_root: Path, manual_set: str, version: str) -> Path:
    return Path(out_root) / "manual-sets" / manual_set / "annotations" / version


def to_parquet(db: Session, version_id: int, out_root: Path) -> dict[str, Any]:
    """Write a version in the standard manual-set layout.

    Five parquet files plus the version's __meta__.md, in
    `<out_root>/manual-sets/<name>/annotations/<version>/`, so
    `-o ChestDatasetsRoot` puts them where the dataset format expects them.
    Ids are renumbered from 1: images and annotations in database id order,
    categories by name (the same numbering as COCO and the __meta__.md),
    annotators by name. An annotation keeps the version of the annotation
    batch it came from, an image that of its image batch.
    """
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - the parquet group installs by default
        raise SpecError(
            "parquet export needs pyarrow: poetry install --with parquet"
        ) from exc

    summary = crud.version_summary(db, version_id)
    images, cls, det = _rows(db, version_id)

    # build() refuses unmapped annotations, so this only fires on a version
    # imported by hand — but the format has no way to say "no class".
    unmapped = [r["id"] for r in cls + det if not r["target_category"]]
    if unmapped:
        raise SpecError(
            f"{len(unmapped)} annotations have no target category (e.g. #{unmapped[0]}); "
            "the parquet format needs a class for every annotation"
        )

    image_ids = {r["id"]: i for i, r in enumerate(images, start=1)}
    cat_ids = category_ids(r["target_category"] for r in cls + det)
    annotators = sorted({r["annotator"] for r in cls + det})
    annotator_ids = {name: i for i, name in enumerate(annotators, start=1)}

    int64, float64, string = pa.int64(), pa.float64(), pa.string()

    def col(values, type_):
        return pa.array(list(values), type=type_)

    tables = {
        "images": pa.table(
            {
                "id": col(image_ids.values(), int64),
                "file_name": col((r["file_name"] for r in images), string),
                "height": col((r["height"] for r in images), int64),
                "width": col((r["width"] for r in images), int64),
                "source_dataset": col((r["original_set"] for r in images), string),
                "source_version": col((r["batch_version"] for r in images), string),
            }
        ),
        "cls_annotations": pa.table(
            {
                "id": col(range(1, len(cls) + 1), int64),
                "image_id": col((image_ids[r["image_id"]] for r in cls), int64),
                "category_id": col(
                    (cat_ids[r["target_category"]] for r in cls), int64
                ),
                "score": col((float(r["score"]) for r in cls), float64),
                "annotator_id": col(
                    (annotator_ids[r["annotator"]] for r in cls), int64
                ),
                "source_version": col((r["batch_version"] for r in cls), string),
            }
        ),
        "det_annotations": pa.table(
            {
                "id": col(range(1, len(det) + 1), int64),
                "image_id": col((image_ids[r["image_id"]] for r in det), int64),
                "category_id": col(
                    (cat_ids[r["target_category"]] for r in det), int64
                ),
                "bbox": col(
                    ([float(x) for x in r["bbox"]] for r in det), pa.list_(float64)
                ),
                "segmentation": col(
                    (_polygons(r["segmentation"], r["id"]) for r in det),
                    pa.list_(pa.list_(float64)),
                ),
                "iscrowd": col((r["iscrowd"] for r in det), int64),
                "score": col(
                    (
                        float(r["score"]) if r["score"] is not None else None
                        for r in det
                    ),
                    float64,
                ),
                "annotator_id": col(
                    (annotator_ids[r["annotator"]] for r in det), int64
                ),
                "source_version": col((r["batch_version"] for r in det), string),
            }
        ),
        "categories": pa.table(
            {
                "id": col(cat_ids.values(), int64),
                "name": col(cat_ids.keys(), string),
                # target categories belong to this version; they have no supercategory
                "supercategory": col([None] * len(cat_ids), string),
            }
        ),
        "annotators": pa.table(
            {
                "id": col(annotator_ids.values(), int64),
                "name": col(annotator_ids.keys(), string),
            }
        ),
    }

    out_dir = parquet_dir(out_root, summary["manual_set"], summary["version"])
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in PARQUET_FILES:
        pq.write_table(tables[name], out_dir / f"{name}.parquet")

    meta_md = get_store().get_meta(
        "manual-set", summary["manual_set"], summary["version"]
    )
    if meta_md is not None:
        (out_dir / "__meta__.md").write_text(meta_md, encoding="utf-8")

    return {
        "dir": out_dir,
        "rows": {name: tables[name].num_rows for name in PARQUET_FILES},
        "meta": meta_md is not None,
    }
