"""核心記憶體結構（design_doc.md §5）。

`CandidateSet` 是每個 op 的輸入與輸出：一組 image / annotation 的 id，
加上目前累積的 category 映射。它刻意只存 id，metadata 一律去 `Catalog`
查——這樣 union / intersect 這類集合運算就是純粹的 set 運算，不用搬資料。

`Catalog` 是唯讀的 metadata 快取：一次把用到的 batch 讀進記憶體，
之後所有 op 都在記憶體裡跑。build 的成本因此是「讀一次 DB + 純 Python
集合運算」，符合 design_doc §1 principle 1（業務規則留在 Python，不進 DB）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cxr_dataset_manager.db import models as m

# ---------------------------------------------------------------------------
# 唯讀 metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImageMeta:
    id: int
    image_batch_id: int
    original_set_id: int
    original_set_name: str
    batch_version: str
    file_name: str
    width: int
    height: int
    blake3_hash: str | None
    date_captured: dt.date | None
    subject_id: str | None

    @property
    def ref(self) -> str:
        """人類可讀的定位字串，spec 的 manual_override 就用這個格式。"""
        return f"{self.original_set_name}/{self.batch_version}/{self.file_name}"

    @property
    def source(self) -> str:
        return f"{self.original_set_name}@{self.batch_version}"


@dataclass(frozen=True, slots=True)
class CategoryMeta:
    id: int
    annotation_batch_id: int
    name: str
    supercategory: str | None
    original_set_name: str
    batch_version: str

    @property
    def scope(self) -> str:
        """category 命名空間的識別字串，spec 的 category_map 就用這個 key。"""
        return f"{self.original_set_name}@{self.batch_version}"

    @property
    def ref(self) -> str:
        return f"{self.scope}:{self.name}"


@dataclass(frozen=True, slots=True)
class AnnotationMeta:
    id: int
    kind: str  # 'cls' | 'det'
    annotation_batch_id: int
    image_id: int
    category_id: int
    annotator_id: int
    annotator_name: str
    score: float | None
    original_set_name: str
    batch_version: str
    bbox: tuple[float, float, float, float] | None = None
    iscrowd: int = 0

    @property
    def source(self) -> str:
        return f"{self.original_set_name}@{self.batch_version}"


# ---------------------------------------------------------------------------
# CandidateSet
# ---------------------------------------------------------------------------


@dataclass
class CandidateSet:
    """一個 step 的輸出狀態。

    不變條件：`cls` / `det` 裡的每一筆，其 image_id 一定在 `images` 裡。
    這條規則對應 schema 裡 manual_set_cls_annotations 的複合外鍵
    （「標註被納入時，對應的影像必須先被納入」），由 ops 在每次
    運算後用 `prune()` 維持，所以 commit 時 DB 一定收得下。
    """

    images: set[int] = field(default_factory=set)
    cls: set[int] = field(default_factory=set)
    det: set[int] = field(default_factory=set)
    # local category_id -> target category name（design_doc §3 顯式映射）
    category_targets: dict[int, str] = field(default_factory=dict)

    def copy(self) -> CandidateSet:
        return CandidateSet(
            images=set(self.images),
            cls=set(self.cls),
            det=set(self.det),
            category_targets=dict(self.category_targets),
        )

    def counts(self) -> dict[str, int]:
        return {"images": len(self.images), "cls": len(self.cls), "det": len(self.det)}

    def prune(self, catalog: Catalog) -> CandidateSet:
        """丟掉 image 已不在集合內的 annotation，維持上面的不變條件。"""
        self.cls = {a for a in self.cls if catalog.cls(a).image_id in self.images}
        self.det = {a for a in self.det if catalog.det(a).image_id in self.images}
        return self

    def orphan_annotations(self, catalog: Catalog) -> tuple[set[int], set[int]]:
        return (
            {a for a in self.cls if catalog.cls(a).image_id not in self.images},
            {a for a in self.det if catalog.det(a).image_id not in self.images},
        )


@dataclass(frozen=True, slots=True)
class Decision:
    """一步之中某個 entity 為何改變狀態。

    不落庫：`cxr why` 重跑 spec 時才產生。只解釋「為什麼」——「有哪些」
    由各步 CandidateSet 的成員關係決定，不需要逐筆記錄。
    """

    entity_kind: str  # image | cls_annotation | det_annotation | category
    entity_id: int
    decision: str  # added | dropped | remapped | overridden | missing
    reason: str
    detail: dict[str, Any] | None = None


@dataclass
class StepResult:
    """一個 op 的完整產出：新狀態 + 統計 + 變化說明。

    decisions 只在記憶體裡活著（`cxr why` 重跑 spec 時用），不落庫。
    """

    candidates: CandidateSet
    stats: dict[str, Any] = field(default_factory=dict)
    decisions: list[Decision] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class SpecError(Exception):
    """spec 語意錯誤（找不到 batch、未映射的 category、清單對不上…）。"""


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class Catalog:
    """唯讀 metadata 快取。所有 op 只透過它看資料，不自己下 SQL。"""

    def __init__(self, db: Session) -> None:
        self.db = db
        self._images: dict[int, ImageMeta] = {}
        self._cls: dict[int, AnnotationMeta] = {}
        self._det: dict[int, AnnotationMeta] = {}
        self._categories: dict[int, CategoryMeta] = {}
        self._loaded_image_batches: set[int] = set()
        self._loaded_annotation_batches: set[int] = set()
        self._images_by_batch: dict[int, list[int]] = {}
        self._cls_by_batch: dict[int, list[int]] = {}
        self._det_by_batch: dict[int, list[int]] = {}
        self._categories_by_batch: dict[int, list[int]] = {}

    # -- batch 定位（design_doc §3 的 v_batches 的程式碼版本）--------------------------

    def resolve_image_batch(self, original_set: str, version: str) -> int:
        row = self.db.execute(
            select(m.ImageBatch.id)
            .join(m.OriginalSet, m.OriginalSet.id == m.ImageBatch.original_set_id)
            .where(m.OriginalSet.name == original_set, m.ImageBatch.version == version)
        ).scalar_one_or_none()
        if row is None:
            raise SpecError(f"image_batch not found: {original_set}@{version}")
        return row

    def resolve_annotation_batch(self, original_set: str, version: str) -> int:
        row = self.db.execute(
            select(m.AnnotationBatch.id)
            .join(m.OriginalSet, m.OriginalSet.id == m.AnnotationBatch.original_set_id)
            .where(
                m.OriginalSet.name == original_set, m.AnnotationBatch.version == version
            )
        ).scalar_one_or_none()
        if row is None:
            raise SpecError(f"annotation_batch not found: {original_set}@{version}")
        return row

    # -- 載入 --------------------------------------------------------------

    def load_image_batch(self, batch_id: int) -> list[int]:
        if batch_id in self._loaded_image_batches:
            return self._images_by_batch.get(batch_id, [])
        rows = self.db.execute(
            select(
                m.Image.id,
                m.Image.image_batch_id,
                m.OriginalSet.id,
                m.OriginalSet.name,
                m.ImageBatch.version,
                m.Image.file_name,
                m.Image.width,
                m.Image.height,
                m.Image.blake3_hash,
                m.Image.date_captured,
                m.ImageSubject.subject_id,
            )
            .join(m.ImageBatch, m.ImageBatch.id == m.Image.image_batch_id)
            .join(m.OriginalSet, m.OriginalSet.id == m.ImageBatch.original_set_id)
            .outerjoin(m.ImageSubject, m.ImageSubject.image_id == m.Image.id)
            .where(m.Image.image_batch_id == batch_id)
        ).all()
        ids: list[int] = []
        for r in rows:
            meta = ImageMeta(
                id=r[0],
                image_batch_id=r[1],
                original_set_id=r[2],
                original_set_name=r[3],
                batch_version=r[4],
                file_name=r[5],
                width=r[6],
                height=r[7],
                blake3_hash=r[8],
                date_captured=r[9],
                subject_id=r[10],
            )
            self._images[meta.id] = meta
            ids.append(meta.id)
        self._images_by_batch[batch_id] = ids
        self._loaded_image_batches.add(batch_id)
        return ids

    def load_annotation_batch(self, batch_id: int) -> tuple[list[int], list[int]]:
        if batch_id in self._loaded_annotation_batches:
            return self._cls_by_batch.get(batch_id, []), self._det_by_batch.get(
                batch_id, []
            )

        src = self.db.execute(
            select(m.OriginalSet.name, m.AnnotationBatch.version)
            .join(
                m.AnnotationBatch, m.AnnotationBatch.original_set_id == m.OriginalSet.id
            )
            .where(m.AnnotationBatch.id == batch_id)
        ).one()
        set_name, version = src

        cat_ids: list[int] = []
        for c in self.db.execute(
            select(m.Category).where(m.Category.annotation_batch_id == batch_id)
        ).scalars():
            self._categories[c.id] = CategoryMeta(
                id=c.id,
                annotation_batch_id=c.annotation_batch_id,
                name=c.name,
                supercategory=c.supercategory,
                original_set_name=set_name,
                batch_version=version,
            )
            cat_ids.append(c.id)
        self._categories_by_batch[batch_id] = cat_ids

        cls_ids: list[int] = []
        for a, annotator in self.db.execute(
            select(m.ClsAnnotation, m.Annotator.name)
            .join(m.Annotator, m.Annotator.id == m.ClsAnnotation.annotator_id)
            .where(m.ClsAnnotation.annotation_batch_id == batch_id)
        ).all():
            self._cls[a.id] = AnnotationMeta(
                id=a.id,
                kind="cls",
                annotation_batch_id=a.annotation_batch_id,
                image_id=a.image_id,
                category_id=a.category_id,
                annotator_id=a.annotator_id,
                annotator_name=annotator,
                score=float(a.score) if a.score is not None else None,
                original_set_name=set_name,
                batch_version=version,
            )
            cls_ids.append(a.id)
        self._cls_by_batch[batch_id] = cls_ids

        det_ids: list[int] = []
        for a, annotator in self.db.execute(
            select(m.DetAnnotation, m.Annotator.name)
            .join(m.Annotator, m.Annotator.id == m.DetAnnotation.annotator_id)
            .where(m.DetAnnotation.annotation_batch_id == batch_id)
        ).all():
            self._det[a.id] = AnnotationMeta(
                id=a.id,
                kind="det",
                annotation_batch_id=a.annotation_batch_id,
                image_id=a.image_id,
                category_id=a.category_id,
                annotator_id=a.annotator_id,
                annotator_name=annotator,
                score=float(a.score) if a.score is not None else None,
                original_set_name=set_name,
                batch_version=version,
                bbox=tuple(float(x) for x in a.bbox),  # type: ignore[arg-type]
                iscrowd=a.iscrowd,
            )
            det_ids.append(a.id)
        self._det_by_batch[batch_id] = det_ids

        # 標註指向的影像可能屬於別的 image_batch（annotation_batch 與
        # image_batch 是兩條獨立的版本軸），把它們一併載進來。
        self.ensure_images(
            {self._cls[i].image_id for i in cls_ids}
            | {self._det[i].image_id for i in det_ids}
        )

        self._loaded_annotation_batches.add(batch_id)
        return cls_ids, det_ids

    def load_annotations_for_images(
        self, image_ids: Iterable[int]
    ) -> tuple[list[int], list[int]]:
        """把這些影像身上既有的標註全部載進來，回傳 (cls_ids, det_ids)。

        一張影像可能被多個 annotation_batch 標註過——那正是衝突的來源，
        所以這裡不挑，全部帶進來，讓 conflict_resolve 去攤開處理。
        """
        wanted = set(image_ids)
        if not wanted:
            return [], []

        batch_ids = set(
            self.db.execute(
                select(m.ClsAnnotation.annotation_batch_id)
                .where(m.ClsAnnotation.image_id.in_(wanted))
                .distinct()
            ).scalars()
        ) | set(
            self.db.execute(
                select(m.DetAnnotation.annotation_batch_id)
                .where(m.DetAnnotation.image_id.in_(wanted))
                .distinct()
            ).scalars()
        )

        cls_ids: list[int] = []
        det_ids: list[int] = []
        for batch_id in sorted(batch_ids):
            batch_cls, batch_det = self.load_annotation_batch(batch_id)
            cls_ids += [a for a in batch_cls if self.cls(a).image_id in wanted]
            det_ids += [a for a in batch_det if self.det(a).image_id in wanted]
        return cls_ids, det_ids

    def annotation_batches_of(
        self, annotation_ids: Iterable[int], kind: str
    ) -> set[str]:
        pool = self._cls if kind == "cls" else self._det
        return {pool[a].source for a in annotation_ids if a in pool}

    def ensure_images(self, image_ids: Iterable[int]) -> None:
        missing = [i for i in set(image_ids) if i not in self._images]
        if not missing:
            return
        rows = self.db.execute(
            select(
                m.Image.id,
                m.Image.image_batch_id,
                m.OriginalSet.id,
                m.OriginalSet.name,
                m.ImageBatch.version,
                m.Image.file_name,
                m.Image.width,
                m.Image.height,
                m.Image.blake3_hash,
                m.Image.date_captured,
                m.ImageSubject.subject_id,
            )
            .join(m.ImageBatch, m.ImageBatch.id == m.Image.image_batch_id)
            .join(m.OriginalSet, m.OriginalSet.id == m.ImageBatch.original_set_id)
            .outerjoin(m.ImageSubject, m.ImageSubject.image_id == m.Image.id)
            .where(m.Image.id.in_(missing))
        ).all()
        for r in rows:
            self._images[r[0]] = ImageMeta(
                id=r[0],
                image_batch_id=r[1],
                original_set_id=r[2],
                original_set_name=r[3],
                batch_version=r[4],
                file_name=r[5],
                width=r[6],
                height=r[7],
                blake3_hash=r[8],
                date_captured=r[9],
                subject_id=r[10],
            )

    def ensure_annotations(self, kind: str, annotation_ids: Iterable[int]) -> None:
        """依 id 補載標註，連同它所屬的整個 annotation_batch。

        對應 ensure_images：使用者用 manual_override 指名一筆標註時，那筆
        不見得屬於這份 spec source 過的批次，補載一下就好——沒有理由要求
        使用者為了指名一筆標註而先 source 整個批次。
        """
        pool = self._cls if kind == "cls" else self._det
        missing = [a for a in set(annotation_ids) if a not in pool]
        if not missing:
            return
        model = m.ClsAnnotation if kind == "cls" else m.DetAnnotation
        batch_ids = set(
            self.db.execute(
                select(model.annotation_batch_id)
                .where(model.id.in_(missing))
                .distinct()
            ).scalars()
        )
        for batch_id in sorted(batch_ids):
            self.load_annotation_batch(batch_id)

    def ensure_categories(self, category_ids: Iterable[int]) -> None:
        missing = [c for c in set(category_ids) if c not in self._categories]
        if not missing:
            return
        rows = self.db.execute(
            select(m.Category, m.OriginalSet.name, m.AnnotationBatch.version)
            .join(
                m.AnnotationBatch,
                m.AnnotationBatch.id == m.Category.annotation_batch_id,
            )
            .join(m.OriginalSet, m.OriginalSet.id == m.AnnotationBatch.original_set_id)
            .where(m.Category.id.in_(missing))
        ).all()
        for c, set_name, version in rows:
            self._categories[c.id] = CategoryMeta(
                id=c.id,
                annotation_batch_id=c.annotation_batch_id,
                name=c.name,
                supercategory=c.supercategory,
                original_set_name=set_name,
                batch_version=version,
            )

    # -- 查詢 --------------------------------------------------------------

    def image(self, image_id: int) -> ImageMeta:
        if image_id not in self._images:
            self.ensure_images([image_id])
        return self._images[image_id]

    def cls(self, annotation_id: int) -> AnnotationMeta:
        return self._cls[annotation_id]

    def det(self, annotation_id: int) -> AnnotationMeta:
        return self._det[annotation_id]

    def annotation(self, kind: str, annotation_id: int) -> AnnotationMeta:
        return self._cls[annotation_id] if kind == "cls" else self._det[annotation_id]

    def category(self, category_id: int) -> CategoryMeta:
        if category_id not in self._categories:
            self.ensure_categories([category_id])
        return self._categories[category_id]

    def annotations_of_image(
        self, image_id: int, kind: str = "cls"
    ) -> list[AnnotationMeta]:
        pool = self._cls if kind == "cls" else self._det
        return [a for a in pool.values() if a.image_id == image_id]

    def images_of_batch(self, batch_id: int) -> list[int]:
        return self._images_by_batch.get(batch_id, [])

    def categories_of_batch(self, batch_id: int) -> list[int]:
        return self._categories_by_batch.get(batch_id, [])

    def find_image_by_file_name(
        self, original_set: str, batch_version: str, file_name: str
    ) -> ImageMeta | None:
        for meta in self._images.values():
            if (
                meta.original_set_name == original_set
                and meta.batch_version == batch_version
                and meta.file_name == file_name
            ):
                return meta
        return None

    def subject_key(self, image_id: int) -> tuple[str, bool]:
        """回傳 (切割用的 key, 是否為 fallback)。

        design_doc §1 principle 5：沒有 subject 資訊的影像 fallback 用 image_id 本身當 key，
        並在溯源記錄裡註記，讓使用者知道這批資料沒有病患層級保證。
        """
        meta = self.image(image_id)
        if meta.subject_id:
            return meta.subject_id, False
        return f"__image__{image_id}", True
