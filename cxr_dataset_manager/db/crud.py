"""基本的 DB 讀寫封裝（Layer 1）。

CLI 的各個指令共用同一套查詢——介面只是皮，邏輯只寫一份（design_doc §1 principle 7）。
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Optional

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from cxr_dataset_manager.core.schema import BuildSpec
from cxr_dataset_manager.core.types import SpecError
from cxr_dataset_manager.db import models as m
# spec 本體不在資料庫，讀某個版本的 spec 一定要碰物件儲存。storage 跟
# settings 一樣是最外層的葉子模組（不 import 任何 cxr 模組），所以這條
# 相依方向跟 db → settings 一致，沒有繞回來。
from cxr_dataset_manager.storage import get_store, spec_key


# ---------------------------------------------------------------------------
# 目錄瀏覽
# ---------------------------------------------------------------------------


def list_original_sets(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT os.id, os.name, os.created_at,
                   (SELECT count(*) FROM image_batches ib WHERE ib.original_set_id = os.id) AS image_batches,
                   (SELECT count(*) FROM annotation_batches ab WHERE ab.original_set_id = os.id) AS annotation_batches,
                   (SELECT count(*) FROM images i
                      JOIN image_batches ib ON ib.id = i.image_batch_id
                     WHERE ib.original_set_id = os.id) AS images
            FROM original_sets os ORDER BY os.name
            """
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def list_batches(db: Session, original_set: Optional[str] = None) -> list[dict[str, Any]]:
    sql = """
        SELECT b.original_set_name, b.batch_kind, b.version, b.batch_id,
               CASE WHEN b.batch_kind = 'image'
                    THEN (SELECT count(*) FROM images i WHERE i.image_batch_id = b.batch_id)
                    ELSE (SELECT count(*) FROM cls_annotations a WHERE a.annotation_batch_id = b.batch_id)
                         + (SELECT count(*) FROM det_annotations a WHERE a.annotation_batch_id = b.batch_id)
               END AS item_count,
               CASE WHEN b.batch_kind = 'annotation'
                    THEN (SELECT count(*) FROM categories c WHERE c.annotation_batch_id = b.batch_id)
                    ELSE 0 END AS categories
        FROM v_batches b
        {where}
        ORDER BY b.original_set_name, b.batch_kind, b.version
    """
    where = "WHERE b.original_set_name = :name" if original_set else ""
    rows = db.execute(
        text(sql.format(where=where)), {"name": original_set} if original_set else {}
    ).mappings().all()
    return [dict(r) for r in rows]


def list_categories(db: Session, annotation_batch_id: Optional[int] = None) -> list[dict[str, Any]]:
    stmt = (
        select(
            m.Category.id,
            m.Category.name,
            m.Category.supercategory,
            m.OriginalSet.name.label("original_set"),
            m.AnnotationBatch.version,
            m.AnnotationBatch.id.label("annotation_batch_id"),
        )
        .join(m.AnnotationBatch, m.AnnotationBatch.id == m.Category.annotation_batch_id)
        .join(m.OriginalSet, m.OriginalSet.id == m.AnnotationBatch.original_set_id)
        .order_by(m.OriginalSet.name, m.AnnotationBatch.version, m.Category.name)
    )
    if annotation_batch_id:
        stmt = stmt.where(m.Category.annotation_batch_id == annotation_batch_id)
    return [dict(r._mapping) for r in db.execute(stmt).all()]


def list_annotators(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT a.id, a.name,
                   (SELECT count(*) FROM cls_annotations c WHERE c.annotator_id = a.id) AS cls,
                   (SELECT count(*) FROM det_annotations d WHERE d.annotator_id = a.id) AS det
            FROM annotators a ORDER BY a.name
            """
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def list_manual_sets(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT ms.id, ms.name, ms.created_at,
                   mv.id AS version_id, mv.version, mv.created_at AS version_created_at,
                   (SELECT count(*) FROM manual_set_images x WHERE x.manual_set_version_id = mv.id) AS images,
                   (SELECT count(*) FROM manual_set_cls_annotations x WHERE x.manual_set_version_id = mv.id) AS cls,
                   (SELECT count(*) FROM manual_set_det_annotations x WHERE x.manual_set_version_id = mv.id) AS det,
                   (SELECT count(*) FROM manual_set_target_categories x WHERE x.manual_set_version_id = mv.id) AS targets
            FROM manual_sets ms
            LEFT JOIN manual_set_versions mv ON mv.manual_set_id = ms.id
            ORDER BY ms.name, mv.version
            """
        )
    ).mappings().all()

    grouped: dict[str, dict[str, Any]] = {}
    for r in rows:
        entry = grouped.setdefault(
            r["name"], {"id": r["id"], "name": r["name"], "created_at": r["created_at"], "versions": []}
        )
        if r["version_id"] is not None:
            entry["versions"].append(
                {
                    "version_id": r["version_id"],
                    "version": r["version"],
                    "created_at": r["version_created_at"],
                    "images": r["images"],
                    "cls": r["cls"],
                    "det": r["det"],
                    "targets": r["targets"],
                }
            )
    return list(grouped.values())


def resolve_version(db: Session, manual_set: str, version: str) -> Optional[int]:
    return db.execute(
        select(m.ManualSetVersion.id)
        .join(m.ManualSet, m.ManualSet.id == m.ManualSetVersion.manual_set_id)
        .where(m.ManualSet.name == manual_set, m.ManualSetVersion.version == version)
    ).scalar_one_or_none()


def parse_ref(ref: str) -> tuple[str, str]:
    """`name@version` → (name, version)。

    丟 SpecError 而不是 ValueError：這是「使用者打錯字」，該被介面層
    收成一行訊息，不該變成 traceback。
    """
    name, _, version = ref.rpartition("@")
    if not name or not version:
        raise SpecError(f"格式應為 name@version（例如 pneumonia@V1），收到 {ref!r}")
    return name, version


# ---------------------------------------------------------------------------
# 外部檔名清單
#
# 上萬個檔名內嵌在 spec 裡會讓 spec 完全無法 review，所以清單本體存在
# manual_set_import_lists，spec 只留 sha256。這張表是內容定址的：同一份
# 清單重複註冊只會有一列，不同 manual-set 用到同一份清單也只存一次。
# ---------------------------------------------------------------------------


def normalize_file_names(names: Iterable[str]) -> list[str]:
    """去掉空白與空行，保留順序與重複。

    刻意不去重：`import_list` 會回報「清單裡有幾筆重複」，先去重就看不到了。
    """
    return [n.strip() for n in names if n.strip()]


def import_list_sha256(names: Iterable[str]) -> str:
    """內容雜湊。同樣的清單在任何機器上都得到同一個 sha256，
    所以 spec 帶著 sha256 換一台資料庫，只要對方也註冊過同一份清單就能重跑。"""
    normalized = normalize_file_names(names)
    return hashlib.sha256("\n".join(normalized).encode()).hexdigest()


def register_import_list(
    db: Session, names: Iterable[str], source_note: Optional[str] = None
) -> tuple[str, bool]:
    """把清單存進資料庫，回傳 (sha256, 是否為新建)。

    內容定址所以天然冪等：同一份清單註冊幾次都只有一列。
    """
    normalized = normalize_file_names(names)
    if not normalized:
        raise SpecError("清單是空的，沒有東西可以註冊")
    digest = import_list_sha256(normalized)

    if db.get(m.ManualSetImportList, digest) is not None:
        return digest, False
    db.add(
        m.ManualSetImportList(
            sha256=digest, file_names=normalized, source_note=source_note
        )
    )
    db.commit()
    return digest, True


def get_import_list(db: Session, sha256: str) -> Optional[list[str]]:
    row = db.get(m.ManualSetImportList, sha256)
    return list(row.file_names) if row else None


def list_import_lists(db: Session, limit: int = 50) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT sha256, array_length(file_names, 1) AS n, source_note, created_at
            FROM manual_set_import_lists ORDER BY created_at DESC LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# manual-set 版本內容
# ---------------------------------------------------------------------------


def load_spec(db: Session, version_id: int) -> dict[str, Any]:
    """把某個版本的 spec 從物件儲存讀回來，順便驗指紋。

    spec 本體是 manual-sets/{name}/annotations/{version}/spec.yaml，位置由
    (名稱, 版本) 算出來，不存在資料庫裡。資料庫只留 sha256，於是這裡能分辨
    四種狀態——這正是留著那個欄位的理由：

      none      這版本是匯入的，本來就沒有 spec
      ok        檔案在，而且跟當初建構時逐位元組相同
      missing   資料庫說有，物件儲存找不到（被刪了，或 bucket 不對）
      modified  檔案在，但內容跟指紋對不起來（被人改過）
    """
    row = db.execute(
        text(
            """
            SELECT ms.name AS manual_set, mv.version, mv.spec_sha256
            FROM manual_set_versions mv JOIN manual_sets ms ON ms.id = mv.manual_set_id
            WHERE mv.id = :vid
            """
        ),
        {"vid": version_id},
    ).mappings().one()

    key = spec_key(row["manual_set"], row["version"])
    if row["spec_sha256"] is None:
        return {"status": "none", "yaml": None, "key": key, "sha256": None, "expected": None}

    yaml_text = get_store().get_spec(row["manual_set"], row["version"])
    if yaml_text is None:
        return {
            "status": "missing", "yaml": None, "key": key,
            "sha256": None, "expected": row["spec_sha256"],
        }

    actual = BuildSpec.from_yaml(yaml_text).sha256()
    return {
        "status": "ok" if actual == row["spec_sha256"] else "modified",
        "yaml": yaml_text,
        "key": key,
        "sha256": actual,
        "expected": row["spec_sha256"],
    }


def version_summary(db: Session, version_id: int) -> dict[str, Any]:
    head = db.execute(
        text(
            """
            SELECT ms.name AS manual_set, mv.version, mv.created_at, mv.id AS version_id,
                   mv.created_by_name, mv.created_by_email, mv.spec_sha256
            FROM manual_set_versions mv JOIN manual_sets ms ON ms.id = mv.manual_set_id
            WHERE mv.id = :vid
            """
        ),
        {"vid": version_id},
    ).mappings().one()

    counts = db.execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM manual_set_images WHERE manual_set_version_id = :vid) AS images,
              (SELECT count(*) FROM manual_set_cls_annotations WHERE manual_set_version_id = :vid) AS cls,
              (SELECT count(*) FROM manual_set_det_annotations WHERE manual_set_version_id = :vid) AS det
            """
        ),
        {"vid": version_id},
    ).mappings().one()

    by_source = db.execute(
        text(
            """
            SELECT os.name || '@' || ib.version AS source, count(*) AS n
            FROM manual_set_images msi
            JOIN images i ON i.id = msi.image_id
            JOIN image_batches ib ON ib.id = i.image_batch_id
            JOIN original_sets os ON os.id = ib.original_set_id
            WHERE msi.manual_set_version_id = :vid
            GROUP BY 1 ORDER BY n DESC
            """
        ),
        {"vid": version_id},
    ).mappings().all()

    by_target = db.execute(
        text(
            """
            SELECT tc.name AS target, count(*) AS n
            FROM manual_set_cls_annotations msa
            JOIN cls_annotations a ON a.id = msa.cls_annotation_id
            JOIN manual_set_category_mappings cm
              ON cm.manual_set_version_id = msa.manual_set_version_id
             AND cm.category_id = a.category_id
            JOIN manual_set_target_categories tc ON tc.id = cm.target_category_id
            WHERE msa.manual_set_version_id = :vid
            GROUP BY 1 ORDER BY n DESC
            """
        ),
        {"vid": version_id},
    ).mappings().all()

    subjects = db.execute(
        text(
            """
            -- 欄位名要跟 session/analyzer.py 的 summarize() 一致：
            -- 同一個概念在兩個地方叫不同名字，讀的人遲早會拿錯鍵
            SELECT count(DISTINCT s.subject_id) AS distinct,
                   count(*) FILTER (WHERE s.subject_id IS NULL) AS images_without_subject
            FROM manual_set_images msi
            LEFT JOIN image_subjects s ON s.image_id = msi.image_id
            WHERE msi.manual_set_version_id = :vid
            """
        ),
        {"vid": version_id},
    ).mappings().one()

    mappings = db.execute(
        text(
            """
            SELECT tc.name AS target, os.name || '@' || ab.version || ':' || c.name AS local
            FROM manual_set_category_mappings cm
            JOIN manual_set_target_categories tc ON tc.id = cm.target_category_id
            JOIN categories c ON c.id = cm.category_id
            JOIN annotation_batches ab ON ab.id = c.annotation_batch_id
            JOIN original_sets os ON os.id = ab.original_set_id
            WHERE cm.manual_set_version_id = :vid
            ORDER BY tc.name, local
            """
        ),
        {"vid": version_id},
    ).mappings().all()

    grouped_mappings: dict[str, list[str]] = {}
    for row in mappings:
        grouped_mappings.setdefault(row["target"], []).append(row["local"])

    spec = load_spec(db, version_id)

    return {
        **dict(head),
        **dict(counts),
        "by_source": {r["source"]: r["n"] for r in by_source},
        "by_target_category": {r["target"]: r["n"] for r in by_target},
        "subjects": dict(subjects),
        "category_mappings": grouped_mappings,
        "created_by": f"{head['created_by_name']} <{head['created_by_email']}>",
        "spec_yaml": spec["yaml"],
        "spec_key": spec["key"],
        "spec_status": spec["status"],
    }


def list_version_images(
    db: Session, version_id: int, *, limit: int = 60, offset: int = 0,
    search: Optional[str] = None, target_category: Optional[str] = None,
    source: Optional[str] = None,
) -> dict[str, Any]:
    where = ["msi.manual_set_version_id = :vid"]
    params: dict[str, Any] = {"vid": version_id, "limit": limit, "offset": offset}
    if search:
        where.append("i.file_name ILIKE :search")
        params["search"] = f"%{search}%"
    if source:
        where.append("os.name || '@' || ib.version = :source")
        params["source"] = source
    if target_category:
        where.append(
            """EXISTS (
                 SELECT 1 FROM manual_set_cls_annotations msa
                 JOIN cls_annotations a ON a.id = msa.cls_annotation_id
                 JOIN manual_set_category_mappings cm
                   ON cm.manual_set_version_id = msa.manual_set_version_id
                  AND cm.category_id = a.category_id
                 JOIN manual_set_target_categories tc ON tc.id = cm.target_category_id
                 WHERE msa.manual_set_version_id = msi.manual_set_version_id
                   AND msa.image_id = i.id AND tc.name = :target)"""
        )
        params["target"] = target_category

    clause = " AND ".join(where)
    total = db.execute(
        text(
            f"""
            SELECT count(*) FROM manual_set_images msi
            JOIN images i ON i.id = msi.image_id
            JOIN image_batches ib ON ib.id = i.image_batch_id
            JOIN original_sets os ON os.id = ib.original_set_id
            WHERE {clause}
            """
        ),
        params,
    ).scalar_one()

    rows = db.execute(
        text(
            f"""
            SELECT i.id, i.file_name, i.width, i.height, i.blake3_hash, i.date_captured,
                   os.name AS original_set, ib.version AS batch_version,
                   s.subject_id, l.name AS license,
                   (SELECT count(*) FROM manual_set_cls_annotations x
                     WHERE x.manual_set_version_id = :vid AND x.image_id = i.id) AS cls,
                   (SELECT count(*) FROM manual_set_det_annotations x
                     WHERE x.manual_set_version_id = :vid AND x.image_id = i.id) AS det,
                   (SELECT string_agg(DISTINCT tc.name, ',')
                      FROM manual_set_cls_annotations msa
                      JOIN cls_annotations a ON a.id = msa.cls_annotation_id
                      JOIN manual_set_category_mappings cm
                        ON cm.manual_set_version_id = msa.manual_set_version_id
                       AND cm.category_id = a.category_id
                      JOIN manual_set_target_categories tc ON tc.id = cm.target_category_id
                     WHERE msa.manual_set_version_id = :vid AND msa.image_id = i.id) AS targets
            FROM manual_set_images msi
            JOIN images i ON i.id = msi.image_id
            JOIN image_batches ib ON ib.id = i.image_batch_id
            JOIN original_sets os ON os.id = ib.original_set_id
            LEFT JOIN image_subjects s ON s.image_id = i.id
            LEFT JOIN licenses l ON l.id = i.license_id
            WHERE {clause}
            ORDER BY os.name, i.file_name
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings().all()

    return {"total": total, "offset": offset, "limit": limit, "items": [dict(r) for r in rows]}


def resolve_image(db: Session, token: str) -> Optional[int]:
    """`123`、`original_set/版本/檔名`、或裸檔名 → image_id。

    純數字當 id；其餘走檔名查詢。檔名只在 image_batch 內唯一，所以裸檔名
    對到多張時會回 None，要求使用者給完整路徑或 id。
    """
    if token.isdigit():
        found = db.execute(
            select(m.Image.id).where(m.Image.id == int(token))
        ).scalar_one_or_none()
        return found

    matches = list(
        db.execute(
            text(
                """
                SELECT i.id FROM images i
                JOIN image_batches ib ON ib.id = i.image_batch_id
                JOIN original_sets os ON os.id = ib.original_set_id
                WHERE i.file_name = :fn
                  AND (CAST(:osname AS text) IS NULL OR os.name = :osname)
                  AND (CAST(:version AS text) IS NULL OR ib.version = :version)
                ORDER BY i.id
                """
            ),
            _split_image_ref(token),
        ).scalars()
    )
    if len(matches) != 1:
        return None
    return matches[0]


def image_detail(db: Session, image_id: int, version_id: Optional[int] = None) -> dict[str, Any]:
    head = db.execute(
        text(
            """
            SELECT i.id, i.file_name, i.width, i.height, i.blake3_hash, i.date_captured,
                   os.name AS original_set, ib.version AS batch_version,
                   s.subject_id, l.name AS license
            FROM images i
            JOIN image_batches ib ON ib.id = i.image_batch_id
            JOIN original_sets os ON os.id = ib.original_set_id
            LEFT JOIN image_subjects s ON s.image_id = i.id
            LEFT JOIN licenses l ON l.id = i.license_id
            WHERE i.id = :iid
            """
        ),
        {"iid": image_id},
    ).mappings().one()

    ann_filter = ""
    params: dict[str, Any] = {"iid": image_id}
    if version_id:
        ann_filter = """AND a.id IN (SELECT cls_annotation_id FROM manual_set_cls_annotations
                                      WHERE manual_set_version_id = :vid)"""
        params["vid"] = version_id

    cls = db.execute(
        text(
            f"""
            SELECT a.id, c.name AS category, os.name || '@' || ab.version AS source,
                   an.name AS annotator, a.score
            FROM cls_annotations a
            JOIN categories c ON c.id = a.category_id
            JOIN annotation_batches ab ON ab.id = a.annotation_batch_id
            JOIN original_sets os ON os.id = ab.original_set_id
            JOIN annotators an ON an.id = a.annotator_id
            WHERE a.image_id = :iid {ann_filter}
            ORDER BY a.id
            """
        ),
        params,
    ).mappings().all()

    det_filter = ann_filter.replace("cls_annotation_id", "det_annotation_id").replace(
        "manual_set_cls_annotations", "manual_set_det_annotations"
    )
    det = db.execute(
        text(
            f"""
            SELECT a.id, c.name AS category, os.name || '@' || ab.version AS source,
                   an.name AS annotator, a.score, a.bbox, a.iscrowd
            FROM det_annotations a
            JOIN categories c ON c.id = a.category_id
            JOIN annotation_batches ab ON ab.id = a.annotation_batch_id
            JOIN original_sets os ON os.id = ab.original_set_id
            JOIN annotators an ON an.id = a.annotator_id
            WHERE a.image_id = :iid {det_filter}
            ORDER BY a.id
            """
        ),
        params,
    ).mappings().all()

    duplicates = db.execute(
        text(
            """
            SELECT i.id, i.file_name, os.name AS original_set, ib.version AS batch_version
            FROM images i
            JOIN image_batches ib ON ib.id = i.image_batch_id
            JOIN original_sets os ON os.id = ib.original_set_id
            WHERE i.blake3_hash = (SELECT blake3_hash FROM images WHERE id = :iid)
              AND i.id <> :iid AND i.blake3_hash IS NOT NULL
            ORDER BY os.name, i.file_name
            """
        ),
        {"iid": image_id},
    ).mappings().all()

    lineage = db.execute(
        text(
            """
            SELECT 'parent' AS direction, p.id, p.file_name, os.name AS original_set, ib.version
            FROM image_lineage l
            JOIN images p ON p.id = l.parent_image_id
            JOIN image_batches ib ON ib.id = p.image_batch_id
            JOIN original_sets os ON os.id = ib.original_set_id
            WHERE l.child_image_id = :iid
            UNION ALL
            SELECT 'child', c.id, c.file_name, os.name, ib.version
            FROM image_lineage l
            JOIN images c ON c.id = l.child_image_id
            JOIN image_batches ib ON ib.id = c.image_batch_id
            JOIN original_sets os ON os.id = ib.original_set_id
            WHERE l.parent_image_id = :iid
            """
        ),
        {"iid": image_id},
    ).mappings().all()

    # 這張圖被哪些 manual-set 用了——刪除、查洩漏、追責任時都會想知道
    used_by = db.execute(
        text(
            """
            SELECT ms.name, mv.version,
                   (SELECT count(*) FROM manual_set_cls_annotations a
                     WHERE a.manual_set_version_id = mv.id AND a.image_id = :iid)
                 + (SELECT count(*) FROM manual_set_det_annotations d
                     WHERE d.manual_set_version_id = mv.id AND d.image_id = :iid) AS annotations
            FROM manual_set_images msi
            JOIN manual_set_versions mv ON mv.id = msi.manual_set_version_id
            JOIN manual_sets ms ON ms.id = mv.manual_set_id
            WHERE msi.image_id = :iid
            ORDER BY ms.name, mv.version
            """
        ),
        {"iid": image_id},
    ).mappings().all()

    return {
        **dict(head),
        "used_by": [dict(r) for r in used_by],
        "cls_annotations": [dict(r) for r in cls],
        "det_annotations": [
            {**dict(r), "bbox": [float(x) for x in r["bbox"]]} for r in det
        ],
        "duplicates": [dict(r) for r in duplicates],
        "lineage": [dict(r) for r in lineage],
    }


# ---------------------------------------------------------------------------
# 刪除
#
# 版本本來是不可變的記錄，刪除是那條規則的例外——build 打錯想重用版本號、
# 清掉實驗留下的東西之類。所以刪除前一定要把「會失去什麼」攤開來講，
# 尤其是 spec：那是唯一能重現這份資料集的東西，而它只是一個文字檔。
# ---------------------------------------------------------------------------


def describe_deletion(
    db: Session, manual_set: str, version: Optional[str] = None
) -> dict[str, Any]:
    """列出將被刪除的版本與各自的內容。version=None 表示整個 manual-set。"""
    ms = db.execute(select(m.ManualSet).filter_by(name=manual_set)).scalar_one_or_none()
    if ms is None:
        return {"found": False}

    stmt = select(m.ManualSetVersion).filter_by(manual_set_id=ms.id)
    if version is not None:
        stmt = stmt.where(m.ManualSetVersion.version == version)
    versions = list(db.execute(stmt.order_by(m.ManualSetVersion.version)).scalars())
    if not versions:
        return {"found": False}

    detail = []
    for v in versions:
        counts = db.execute(
            text(
                """
                SELECT
                  (SELECT count(*) FROM manual_set_images WHERE manual_set_version_id = :v) AS images,
                  (SELECT count(*) FROM manual_set_cls_annotations WHERE manual_set_version_id = :v) AS cls,
                  (SELECT count(*) FROM manual_set_det_annotations WHERE manual_set_version_id = :v) AS det,
                  (SELECT count(*) FROM manual_set_target_categories WHERE manual_set_version_id = :v) AS targets
                """
            ),
            {"v": v.id},
        ).mappings().one()
        spec = load_spec(db, v.id)
        detail.append(
            {
                "version_id": v.id,
                "version": v.version,
                "created_at": v.created_at,
                **dict(counts),
                "steps": len(BuildSpec.from_yaml(spec["yaml"]).steps) if spec["yaml"] else None,
                "spec_sha256": v.spec_sha256,
                "spec_key": spec["key"] if spec["status"] != "none" else None,
                "spec_status": spec["status"],
                "created_by": f"{v.created_by_name} <{v.created_by_email}>",
            }
        )

    remaining = db.execute(
        select(m.ManualSetVersion).filter_by(manual_set_id=ms.id)
    ).scalars().all()
    return {
        "found": True,
        "manual_set": manual_set,
        "manual_set_id": ms.id,
        "versions": detail,
        # 刪完之後這個 manual-set 就空了，連名字一起收掉比較乾淨
        "removes_manual_set": len(detail) == len(remaining),
    }


def delete_manual_set(
    db: Session, manual_set: str, version: Optional[str] = None
) -> dict[str, Any]:
    """刪除一個版本或整個 manual-set。

    走資料庫的 CASCADE：成員關係、target category、映射、spec、溯源步驟
    都會跟著消失，而原始的 images / annotations 完全不受影響——manual-set
    從來就只是「選了哪些」的記錄。
    """
    plan = describe_deletion(db, manual_set, version)
    if not plan["found"]:
        raise SpecError(
            f"找不到 {manual_set}" + (f"@{version}" if version else "")
        )

    if plan["removes_manual_set"]:
        db.execute(
            m.ManualSet.__table__.delete().where(m.ManualSet.id == plan["manual_set_id"])
        )
    else:
        db.execute(
            m.ManualSetVersion.__table__.delete().where(
                m.ManualSetVersion.id.in_([v["version_id"] for v in plan["versions"]])
            )
        )
    db.commit()
    return plan


# ---------------------------------------------------------------------------
# 溯源：cxr why
# ---------------------------------------------------------------------------


def explain(
    db: Session, version_id: int, *, image_id: Optional[int] = None,
    file_name: Optional[str] = None,
) -> dict[str, Any]:
    """回答「這張圖是在哪一步、依據什麼規則被選中／排除的」（design_doc §5）。

    做法是把該版本的 spec 重跑一遍，觀察這張影像（與它的標註）在每一步的
    成員關係怎麼變。沒有預先存下來的逐筆紀錄——那是 (spec, 資料) 的純函式，
    存起來只會比資料集本身還大，而且規則一改就跟現況對不上。

    代價是這支查詢會實際執行一次 build（不寫任何東西），成本跟
    `cxr build --dry-run` 相同。
    """
    from cxr_dataset_manager.core.engine import execute_spec
    from cxr_dataset_manager.core.schema import BuildSpec

    if image_id is None and file_name:
        # 明確 cast 成 text：只給檔名時參數是 NULL，Postgres 推不出型別
        image_id = db.execute(
            text(
                """
                SELECT i.id FROM images i
                JOIN image_batches ib ON ib.id = i.image_batch_id
                JOIN original_sets os ON os.id = ib.original_set_id
                WHERE i.file_name = :fn
                  AND (CAST(:osname AS text) IS NULL OR os.name = :osname)
                  AND (CAST(:version AS text) IS NULL OR ib.version = :version)
                ORDER BY i.id LIMIT 1
                """
            ),
            _split_image_ref(file_name),
        ).scalar_one_or_none()
    if image_id is None:
        return {"found": False, "reason": "找不到這張影像"}

    spec = load_spec(db, version_id)
    if spec["yaml"] is None:
        return {"found": False, "reason": _spec_unavailable(spec)}

    execution = execute_spec(db, BuildSpec.from_yaml(spec["yaml"]))

    # cls 與 det 的 id 是各自獨立的序列，會撞號，所以一定要連 kind 一起配對
    tracked: list[tuple[str, int]] = [("image", image_id)]
    tracked += [
        ("cls_annotation", i)
        for i in db.execute(
            text("SELECT id FROM cls_annotations WHERE image_id = :iid"), {"iid": image_id}
        ).scalars()
    ]
    tracked += [
        ("det_annotation", i)
        for i in db.execute(
            text("SELECT id FROM det_annotations WHERE image_id = :iid"), {"iid": image_id}
        ).scalars()
    ]

    def contains(cand, kind: str, eid: int) -> bool:
        pool = {"image": cand.images, "cls_annotation": cand.cls, "det_annotation": cand.det}
        return eid in pool[kind]

    def category_of(kind: str, eid: int) -> Optional[int]:
        """這筆標註屬於哪個 local category。

        catalog 只載入這份 spec 用到的 annotation_batch，所以這張圖在別的
        batch 底下的標註查不到——那些本來就與這次 build 無關，跳過即可。
        """
        try:
            meta = (
                execution.catalog.cls(eid)
                if kind == "cls_annotation"
                else execution.catalog.det(eid)
            )
        except KeyError:
            return None
        return meta.category_id

    own_categories = {
        cid
        for kind, eid in tracked
        if kind != "image" and (cid := category_of(kind, eid)) is not None
    }

    trail: list[dict[str, Any]] = []
    for report in execution.reports:
        current = execution.results[report.step_id]
        inputs = [execution.results[dep] for dep in report.input_step_ids]
        # 這一步為什麼這樣做——由 op 產生的說明，只有變化的 entity 才有
        reasons = {
            (d.entity_kind, d.entity_id): d for d in report.decisions
        }

        for kind, eid in tracked:
            was_in = any(contains(c, kind, eid) for c in inputs)  # 葉節點沒有 input
            now_in = contains(current, kind, eid)
            if was_in == now_in:
                continue
            ruling = reasons.get((kind, eid))
            trail.append(
                {
                    "step_index": report.step_index,
                    "step_id": report.step_id,
                    "op": report.op,
                    "entity_kind": kind,
                    "entity_id": eid,
                    "decision": "added" if now_in else "dropped",
                    "reason": ruling.reason if ruling else report.op,
                    "detail": ruling.detail if ruling else None,
                }
            )

        # 類別重新映射不改變成員關係，但仍然是這張圖身上發生的事
        for (kind, eid), ruling in reasons.items():
            if kind == "category" and eid in own_categories:
                trail.append(
                    {
                        "step_index": report.step_index,
                        "step_id": report.step_id,
                        "op": report.op,
                        "entity_kind": kind,
                        "entity_id": eid,
                        "decision": ruling.decision,
                        "reason": ruling.reason,
                        "detail": ruling.detail,
                    }
                )

    in_final = db.execute(
        text(
            "SELECT 1 FROM manual_set_images WHERE manual_set_version_id = :vid AND image_id = :iid"
        ),
        {"vid": version_id, "iid": image_id},
    ).scalar_one_or_none()

    return {
        "found": True,
        "image_id": image_id,
        "spec_key": spec["key"],
        "spec_status": spec["status"],
        "in_final_set": bool(in_final),
        "trail": trail,
    }


def _spec_unavailable(spec: dict[str, Any]) -> str:
    """把 load_spec 的狀態翻成一句能照著處理的話。"""
    if spec["status"] == "none":
        return "這個版本沒有 spec（是用 scripts/tools/ 匯入的，不是 build 出來的）"
    if spec["status"] == "missing":
        return f"物件儲存上找不到 {spec['key']}——spec 被刪掉了，這個版本已經重現不出來"
    return (
        f"{spec['key']} 的內容跟建構當時對不起來"
        f"（現在 {spec['sha256'][:16]}…，當初 {spec['expected'][:16]}…）——"
        "有人改過這份 spec，重跑它不保證得到同一個版本"
    )


def _split_image_ref(ref: str) -> dict[str, Any]:
    """`original_set/version/file_name` 或裸檔名都收。"""
    parts = ref.split("/")
    if len(parts) == 3:
        return {"osname": parts[0], "version": parts[1], "fn": parts[2]}
    return {"osname": None, "version": None, "fn": ref}


def build_history(db: Session, limit: int = 30) -> list[dict[str, Any]]:
    """每一個 manual-set 版本 + 產生它的 spec 的指紋。

    這就是全部的建構歷史了——探索過程不落庫，一個版本一份 spec，
    沒有「同一份 spec 跑過幾次」這種東西可查。

    這裡不顯示步驟數：spec 本體在物件儲存，數步驟等於一個版本抓一次檔案，
    列表沒必要付這個代價。要看步驟就 `cxr show` 或 `cxr spec` 單看一個版本。
    """
    rows = db.execute(
        text(
            """
            SELECT mv.id AS version_id, mv.version, mv.created_at, mv.spec_sha256,
                   mv.created_by_name, mv.created_by_email,
                   ms.name AS manual_set,
                   (SELECT count(*) FROM manual_set_images x
                     WHERE x.manual_set_version_id = mv.id) AS images
            FROM manual_set_versions mv
            JOIN manual_sets ms ON ms.id = mv.manual_set_id
            ORDER BY mv.id DESC LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# diff：資料集時光機
# ---------------------------------------------------------------------------


def diff_versions(db: Session, left_id: int, right_id: int) -> dict[str, Any]:
    """比較兩個 manual-set 版本：影像進出、標註變動、category 映射變動。"""

    def image_ids(vid: int) -> set[int]:
        return set(
            db.execute(
                select(m.ManualSetImage.image_id).where(
                    m.ManualSetImage.manual_set_version_id == vid
                )
            ).scalars()
        )

    def cls_ids(vid: int) -> set[int]:
        return set(
            db.execute(
                select(m.ManualSetClsAnnotation.cls_annotation_id).where(
                    m.ManualSetClsAnnotation.manual_set_version_id == vid
                )
            ).scalars()
        )

    def mappings(vid: int) -> dict[str, str]:
        rows = db.execute(
            text(
                """
                SELECT os.name || '@' || ab.version || ':' || c.name AS local, tc.name AS target
                FROM manual_set_category_mappings cm
                JOIN manual_set_target_categories tc ON tc.id = cm.target_category_id
                JOIN categories c ON c.id = cm.category_id
                JOIN annotation_batches ab ON ab.id = c.annotation_batch_id
                JOIN original_sets os ON os.id = ab.original_set_id
                WHERE cm.manual_set_version_id = :vid
                """
            ),
            {"vid": vid},
        ).all()
        return {r[0]: r[1] for r in rows}

    left_images, right_images = image_ids(left_id), image_ids(right_id)
    left_cls, right_cls = cls_ids(left_id), cls_ids(right_id)
    left_map, right_map = mappings(left_id), mappings(right_id)

    added, removed = right_images - left_images, left_images - right_images

    def describe(ids: set[int], limit: int = 20) -> list[str]:
        if not ids:
            return []
        rows = db.execute(
            text(
                """
                SELECT os.name || '/' || ib.version || '/' || i.file_name AS ref
                FROM images i
                JOIN image_batches ib ON ib.id = i.image_batch_id
                JOIN original_sets os ON os.id = ib.original_set_id
                WHERE i.id = ANY(:ids) ORDER BY ref LIMIT :limit
                """
            ),
            {"ids": list(ids), "limit": limit},
        ).scalars().all()
        return list(rows)

    # 同一張圖留著、但它的標註集合變了
    common = left_images & right_images
    changed_annotations = db.execute(
        text(
            """
            WITH l AS (SELECT image_id, array_agg(cls_annotation_id ORDER BY cls_annotation_id) a
                       FROM manual_set_cls_annotations WHERE manual_set_version_id = :l GROUP BY 1),
                 r AS (SELECT image_id, array_agg(cls_annotation_id ORDER BY cls_annotation_id) a
                       FROM manual_set_cls_annotations WHERE manual_set_version_id = :r GROUP BY 1)
            SELECT count(*) FROM l FULL OUTER JOIN r USING (image_id)
            WHERE image_id = ANY(:common) AND coalesce(l.a, '{}') <> coalesce(r.a, '{}')
            """
        ),
        {"l": left_id, "r": right_id, "common": list(common) or [0]},
    ).scalar_one()

    mapping_changes = {
        local: {"from": left_map.get(local), "to": right_map.get(local)}
        for local in set(left_map) | set(right_map)
        if left_map.get(local) != right_map.get(local)
    }

    return {
        "left": version_summary(db, left_id),
        "right": version_summary(db, right_id),
        "images": {
            "added": len(added),
            "removed": len(removed),
            "unchanged": len(common),
            "added_sample": describe(added),
            "removed_sample": describe(removed),
        },
        "cls_annotations": {
            "added": len(right_cls - left_cls),
            "removed": len(left_cls - right_cls),
            "images_with_changed_annotations": changed_annotations,
        },
        "category_mapping_changes": mapping_changes,
    }


# ---------------------------------------------------------------------------
# 查重 / leakage
# ---------------------------------------------------------------------------


def duplicate_report(db: Session, version_ids: list[int]) -> dict[str, Any]:
    """跨 manual-set 版本的 blake3 查重——train/val 切分的 leakage 防禦。"""
    rows = db.execute(
        text(
            """
            SELECT i.blake3_hash,
                   array_agg(DISTINCT msi.manual_set_version_id) AS versions,
                   array_agg(DISTINCT os.name || '/' || ib.version || '/' || i.file_name) AS refs,
                   array_agg(DISTINCT s.subject_id) FILTER (WHERE s.subject_id IS NOT NULL) AS subjects
            FROM manual_set_images msi
            JOIN images i ON i.id = msi.image_id
            JOIN image_batches ib ON ib.id = i.image_batch_id
            JOIN original_sets os ON os.id = ib.original_set_id
            LEFT JOIN image_subjects s ON s.image_id = i.id
            WHERE msi.manual_set_version_id = ANY(:vids) AND i.blake3_hash IS NOT NULL
            GROUP BY i.blake3_hash
            HAVING count(DISTINCT msi.manual_set_version_id) > 1
            """
        ),
        {"vids": version_ids},
    ).mappings().all()

    # 同一位病患橫跨多個版本——內容不同但仍然是 leakage
    subject_rows = db.execute(
        text(
            """
            SELECT s.subject_id, array_agg(DISTINCT msi.manual_set_version_id) AS versions,
                   count(*) AS images
            FROM manual_set_images msi
            JOIN image_subjects s ON s.image_id = msi.image_id
            WHERE msi.manual_set_version_id = ANY(:vids)
            GROUP BY s.subject_id
            HAVING count(DISTINCT msi.manual_set_version_id) > 1
            """
        ),
        {"vids": version_ids},
    ).mappings().all()

    return {
        "version_ids": version_ids,
        "identical_content": {
            "groups": len(rows),
            "sample": [dict(r) for r in rows[:50]],
        },
        "shared_subjects": {
            "count": len(subject_rows),
            "images_involved": sum(r["images"] for r in subject_rows),
            "sample": [dict(r) for r in subject_rows[:50]],
        },
    }
