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
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from cxr_dataset_manager.db import crud


def _rows(db: Session, version_id: int) -> tuple[list[dict], list[dict], list[dict]]:
    images = db.execute(
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
    ).mappings().all()

    cls = db.execute(
        text(
            """
            SELECT a.id, a.image_id, a.score, tc.name AS target_category,
                   c.name AS local_category, an.name AS annotator,
                   os.name || '@' || ab.version AS source
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
    ).mappings().all()

    det = db.execute(
        text(
            """
            SELECT a.id, a.image_id, a.bbox, a.iscrowd, a.score, tc.name AS target_category,
                   c.name AS local_category, an.name AS annotator,
                   os.name || '@' || ab.version AS source
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
    ).mappings().all()

    return [dict(r) for r in images], [dict(r) for r in cls], [dict(r) for r in det]


def to_coco(db: Session, version_id: int) -> dict[str, Any]:
    summary = crud.version_summary(db, version_id)
    images, cls, det = _rows(db, version_id)

    names = sorted({r["target_category"] for r in cls + det if r["target_category"]})
    cat_ids = {name: i + 1 for i, name in enumerate(names)}

    coco: dict[str, Any] = {
        "info": {
            "description": f"{summary['manual_set']}@{summary['version']}",
            "spec_sha256": summary["spec_sha256"],
            "date_created": summary["created_at"].isoformat() if summary["created_at"] else None,
        },
        "images": [
            {
                "id": r["id"],
                "file_name": f"{r['original_set']}/images/{r['batch_version']}/{r['file_name']}",
                "width": r["width"],
                "height": r["height"],
                "date_captured": r["date_captured"].isoformat() if r["date_captured"] else None,
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
        ["image_id", "object_key", "original_set", "batch_version", "file_name",
         "width", "height", "subject_id", "date_captured", "blake3_hash", "labels", "n_boxes"]
    )
    box_counts: dict[int, int] = {}
    for r in det:
        box_counts[r["image_id"]] = box_counts.get(r["image_id"], 0) + 1
    for r in images:
        writer.writerow([
            r["id"],
            f"{r['original_set']}/images/{r['batch_version']}/{r['file_name']}",
            r["original_set"], r["batch_version"], r["file_name"],
            r["width"], r["height"], r["subject_id"] or "",
            r["date_captured"].isoformat() if r["date_captured"] else "",
            r["blake3_hash"] or "",
            "|".join(sorted(labels.get(r["id"], []))),
            box_counts.get(r["id"], 0),
        ])
    return buf.getvalue()


def to_zip(db: Session, version_id: int, include_spec: bool = True) -> bytes:
    """COCO json + manifest csv + 產生它的 spec，打包成一份可交付的東西。"""
    summary = crud.version_summary(db, version_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("annotations_coco.json", json.dumps(to_coco(db, version_id), indent=2, default=str))
        z.writestr("manifest.csv", to_manifest_csv(db, version_id))
        if include_spec and summary["spec"]:
            z.writestr("build_spec.json", json.dumps(summary["spec"], indent=2, ensure_ascii=False))
        z.writestr(
            "README.txt",
            f"""{summary['manual_set']}@{summary['version']}
建立時間: {summary['created_at']}
spec sha256: {summary['spec_sha256']}
影像: {summary['images']}  cls 標註: {summary['cls']}  det 標註: {summary['det']}

annotations_coco.json  COCO 格式，class name 用的是這個版本自己的 target category
manifest.csv           一張圖一列，含 subject_id，供 patient-level split 使用
build_spec.json        產生這份資料的 spec，重跑它可以完整重現本結果

影像本體不在這個包裡，請用 manifest.csv 的 object_key 去 original-sets bucket 取。
""",
        )
    return buf.getvalue()
