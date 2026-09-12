"""產生 mock 資料：Postgres metadata + MinIO 影像本體。

刻意做得「髒」，因為乾淨資料試不出這套系統的價值：

  * 跨來源重複影像（同樣的 bytes、同樣的 blake3、不同檔名不同 batch）
  * 同一張圖被兩個 annotation_batch 標成不同類別（annotator 衝突）
  * 同一個 original_set 的 V1／V2 標註互相矛盾（版本衝突）
  * 各家 category 命名不一致（Pneumonia / PNEU / pneumonia）
  * 一部分影像沒有 subject_id（試 design_doc §1 principle 5 的 fallback 與警告）
  * 一個病患多張片子（試 subject-level 切割真的沒把同一人拆開）
"""

from __future__ import annotations

import io
import random
from dataclasses import dataclass
from datetime import date, timedelta

import blake3
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFilter
from sqlalchemy import text
from sqlalchemy.orm import Session

from cxr_dataset_manager.db import models as m
from cxr_dataset_manager.storage import ObjectStore, object_key_for_image

IMAGE_SIZE = 256


# ---------------------------------------------------------------------------
# 合成影像
# ---------------------------------------------------------------------------


def synth_cxr(seed: int, size: int = IMAGE_SIZE) -> bytes:
    """畫一張很粗略、但看得出是「胸腔」的灰階圖，讓 UI 有東西可看。"""
    rng = random.Random(seed)
    img = PILImage.new("L", (size, size), color=12)
    draw = ImageDraw.Draw(img)

    cx, cy = size / 2, size / 2 + size * 0.03
    # 兩片肺野
    for sign in (-1, 1):
        lung_cx = cx + sign * size * 0.18
        draw.ellipse(
            [
                lung_cx - size * 0.15,
                cy - size * 0.28,
                lung_cx + size * 0.15,
                cy + size * 0.26,
            ],
            fill=60 + rng.randint(-8, 8),
        )
    # 縱膈／心影
    draw.ellipse(
        [cx - size * 0.11, cy - size * 0.05, cx + size * 0.13, cy + size * 0.30],
        fill=28,
    )
    # 肋骨
    for i in range(7):
        y = cy - size * 0.26 + i * size * 0.08
        draw.arc(
            [cx - size * 0.40, y - size * 0.12, cx + size * 0.40, y + size * 0.12],
            start=200,
            end=340,
            fill=95,
            width=2,
        )
    # 每張圖獨有的雜訊紋理，確保不同 seed 的 blake3 一定不同
    px = img.load()
    for _ in range(size * 12):
        x, y = rng.randrange(size), rng.randrange(size)
        px[x, y] = max(0, min(255, px[x, y] + rng.randint(-40, 40)))
    img = img.filter(ImageFilter.GaussianBlur(0.6))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 資料規格
# ---------------------------------------------------------------------------


@dataclass
class ImageSpec:
    file_name: str
    payload: bytes
    blake3_hash: str
    subject_id: str | None
    date_captured: date | None
    license_name: str | None


class Seeder:
    def __init__(
        self, db: Session, store: ObjectStore | None = None, seed: int = 20260907
    ):
        self.db = db
        self.store = store
        self.rng = random.Random(seed)
        self.licenses: dict[str, m.License] = {}
        self.annotators: dict[str, m.Annotator] = {}
        self.uploaded = 0
        # 內容池：跨 original_set 的重複影像就是從這裡拿同一份 bytes
        self._payloads: dict[int, bytes] = {}

    # -- 基礎資料 ---------------------------------------------------------

    def _payload(self, content_id: int) -> bytes:
        if content_id not in self._payloads:
            self._payloads[content_id] = synth_cxr(content_id)
        return self._payloads[content_id]

    def license(self, name: str, url: str) -> m.License:
        if name not in self.licenses:
            row = m.License(name=name, url=url)
            self.db.add(row)
            self.db.flush()
            self.licenses[name] = row
        return self.licenses[name]

    def annotator(self, name: str) -> m.Annotator:
        if name not in self.annotators:
            row = m.Annotator(name=name)
            self.db.add(row)
            self.db.flush()
            self.annotators[name] = row
        return self.annotators[name]

    # -- 影像 -------------------------------------------------------------

    def make_image_specs(
        self,
        prefix: str,
        content_ids: list[int],
        *,
        subject_prefix: str,
        images_per_subject: int = 2,
        missing_subject_rate: float = 0.08,
        start_date: date = date(2021, 1, 1),
        license_name: str | None = "CC BY-SA 4.0",
    ) -> list[ImageSpec]:
        specs: list[ImageSpec] = []
        subject_counter = 0
        in_subject = 0
        for index, content_id in enumerate(content_ids):
            payload = self._payload(content_id)
            digest = blake3.blake3(payload).hexdigest()

            if in_subject == 0:
                subject_counter += 1
                in_subject = self.rng.randint(1, images_per_subject + 1)
            in_subject -= 1
            subject = f"{subject_prefix}-{subject_counter:04d}"
            if self.rng.random() < missing_subject_rate:
                subject = None  # type: ignore[assignment]

            specs.append(
                ImageSpec(
                    file_name=f"{prefix}_{index:05d}.png",
                    payload=payload,
                    blake3_hash=digest,
                    subject_id=subject,
                    date_captured=(
                        None
                        if self.rng.random() < 0.05
                        else start_date + timedelta(days=self.rng.randrange(0, 1500))
                    ),
                    license_name=license_name,
                )
            )
        return specs

    def add_image_batch(
        self, original_set: m.OriginalSet, version: str, specs: list[ImageSpec]
    ) -> tuple[m.ImageBatch, list[m.Image]]:
        batch = m.ImageBatch(original_set_id=original_set.id, version=version)
        self.db.add(batch)
        self.db.flush()

        images: list[m.Image] = []
        for spec in specs:
            image = m.Image(
                image_batch_id=batch.id,
                file_name=spec.file_name,
                width=IMAGE_SIZE,
                height=IMAGE_SIZE,
                blake3_hash=spec.blake3_hash,
                license_id=(
                    self.license(
                        spec.license_name,
                        "https://creativecommons.org/licenses/by-sa/4.0/",
                    ).id
                    if spec.license_name
                    else None
                ),
                date_captured=spec.date_captured,
            )
            self.db.add(image)
            images.append(image)
        self.db.flush()

        for image, spec in zip(images, specs):
            if spec.subject_id:
                self.db.add(
                    m.ImageSubject(image_id=image.id, subject_id=spec.subject_id)
                )
            if self.store is not None:
                self.store.put(
                    object_key_for_image(original_set.name, version, spec.file_name),
                    spec.payload,
                    content_type="image/png",
                )
                self.uploaded += 1
        self.db.flush()
        return batch, images

    # -- 標註 -------------------------------------------------------------

    def add_annotation_batch(
        self,
        original_set: m.OriginalSet,
        version: str,
        category_names: list[str],
        images: list[m.Image],
        annotator_name: str,
        *,
        coverage: float = 1.0,
        label_bias: dict[str, float] | None = None,
        with_det: bool = False,
        det_categories: list[str] | None = None,
        label_seed: int = 0,
    ) -> m.AnnotationBatch:
        batch = m.AnnotationBatch(original_set_id=original_set.id, version=version)
        self.db.add(batch)
        self.db.flush()

        categories: dict[str, m.Category] = {}
        for name in category_names:
            row = m.Category(
                annotation_batch_id=batch.id,
                name=name,
                supercategory="finding"
                if name.lower() not in ("normal", "norm")
                else "normal",
            )
            self.db.add(row)
            categories[name] = row
        self.db.flush()

        annotator = self.annotator(annotator_name)
        rng = random.Random(label_seed)
        weights = [(label_bias or {}).get(n, 1.0) for n in category_names]

        # 保證每個 category 至少被引用一次——schema 有 deferred constraint
        # trigger 擋 dangling category，少一個引用整筆 transaction 就會被拒
        forced = list(category_names)
        used: set[str] = set()

        chosen = [i for i in images if rng.random() < coverage]
        if len(chosen) < len(forced):
            chosen = images[: max(len(forced), len(chosen))]

        for offset, image in enumerate(chosen):
            if offset < len(forced):
                name = forced[offset]
            else:
                name = rng.choices(category_names, weights=weights, k=1)[0]
            used.add(name)
            self.db.add(
                m.ClsAnnotation(
                    annotation_batch_id=batch.id,
                    image_id=image.id,
                    category_id=categories[name].id,
                    score=round(rng.uniform(0.55, 1.0), 3),
                    annotator_id=annotator.id,
                )
            )

            if (
                with_det
                and name in (det_categories or category_names)
                and rng.random() < 0.6
            ):
                w = rng.uniform(0.12, 0.30) * IMAGE_SIZE
                h = rng.uniform(0.12, 0.30) * IMAGE_SIZE
                x = rng.uniform(0.05, 0.95 - w / IMAGE_SIZE) * IMAGE_SIZE
                y = rng.uniform(0.05, 0.95 - h / IMAGE_SIZE) * IMAGE_SIZE
                self.db.add(
                    m.DetAnnotation(
                        annotation_batch_id=batch.id,
                        image_id=image.id,
                        category_id=categories[name].id,
                        bbox=[round(x, 2), round(y, 2), round(w, 2), round(h, 2)],
                        iscrowd=0,
                        score=round(rng.uniform(0.5, 0.99), 3),
                        annotator_id=annotator.id,
                    )
                )

        missing = set(category_names) - used
        assert not missing, (
            f"category {missing} is referenced by nothing and would be rejected by the dangling trigger"
        )
        self.db.flush()
        return batch


# ---------------------------------------------------------------------------
# 整份 demo 資料集
# ---------------------------------------------------------------------------


def seed_demo(
    db: Session, store: ObjectStore | None = None, verbose: bool = True
) -> dict:
    """建出四個 original-set，彼此有內容重複、標註矛盾、命名不一致。"""
    s = Seeder(db, store)
    log = print if verbose else (lambda *a, **k: None)

    def add_set(name: str) -> m.OriginalSet:
        row = m.OriginalSet(name=name)
        db.add(row)
        db.flush()
        return row

    aws = add_set("aws_images")
    indo = add_set("indo_vnn")
    tb = add_set("TB-portal")
    drlee = add_set("DrLee")

    # 內容池的切法決定了「哪些圖其實是同一張」：
    #   0-299      aws_images 專有
    #   300-479    indo_vnn 專有
    #   480-629    TB-portal 專有
    #   630-809    DrLee 專有
    #   900-939    aws_images 與 DrLee 共有的 40 張（跨來源重複）
    #   940-959    indo_vnn 與 TB-portal 共有的 20 張
    aws_content = list(range(300)) + list(range(900, 940))
    # aws 自己批次內也有 8 張重複（同一份 bytes 收了兩次，不同檔名）
    aws_content += aws_content[:8]
    indo_content = list(range(300, 480)) + list(range(940, 960))
    tb_content = list(range(480, 630)) + list(range(940, 960))
    drlee_content = list(range(630, 810)) + list(range(900, 940))

    log("generating synthetic images and metadata…")
    aws_specs = s.make_image_specs("AWS", aws_content, subject_prefix="AWS-SUBJ")
    indo_specs = s.make_image_specs(
        "IDN", indo_content, subject_prefix="IDN-SUBJ", missing_subject_rate=0.15
    )
    tb_specs = s.make_image_specs(
        "TBP",
        tb_content,
        subject_prefix="TBP-SUBJ",
        license_name="TB-Portal Research License",
    )
    drlee_specs = s.make_image_specs(
        "DL",
        drlee_content,
        subject_prefix="DL-SUBJ",
        missing_subject_rate=0.02,
        start_date=date(2023, 1, 1),
    )

    _aws_v1, aws_images = s.add_image_batch(aws, "V1", aws_specs)
    _indo_v1, indo_images = s.add_image_batch(indo, "V1", indo_specs)
    _tb_v1, tb_images = s.add_image_batch(tb, "V1", tb_specs)
    _drlee_v1, drlee_images = s.add_image_batch(drlee, "V1", drlee_specs)

    # aws V2：V1 前 120 張的前處理產物（等比例縮放 + 直方圖均衡的假想結果），
    # 用 image_lineage 記錄父子關係
    log("generating aws_images V2 (a preprocessed batch derived from V1)…")
    derived_specs = []
    for parent_spec in aws_specs[:120]:
        payload = synth_cxr(hash(parent_spec.file_name) % 10**6 + 5_000_000)
        derived_specs.append(
            ImageSpec(
                file_name=parent_spec.file_name.replace("AWS_", "AWS_PP_"),
                payload=payload,
                blake3_hash=blake3.blake3(payload).hexdigest(),
                subject_id=parent_spec.subject_id,
                date_captured=parent_spec.date_captured,
                license_name=parent_spec.license_name,
            )
        )
    _aws_v2, aws_v2_images = s.add_image_batch(aws, "V2", derived_specs)
    for parent, child in zip(aws_images[:120], aws_v2_images):
        db.add(m.ImageLineage(parent_image_id=parent.id, child_image_id=child.id))
    db.flush()

    log("generating annotation batches…")
    # aws V1（junior）與 V2（senior）標同一批圖 → annotator 衝突
    s.add_annotation_batch(
        aws,
        "V1",
        ["Pneumonia", "Normal", "Effusion"],
        aws_images,
        "radiologist_junior",
        coverage=0.85,
        label_bias={"Normal": 2.0, "Pneumonia": 1.3, "Effusion": 0.6},
        label_seed=11,
    )
    s.add_annotation_batch(
        aws,
        "V2",
        ["Pneumonia", "Normal", "Effusion"],
        aws_images[:150],
        "radiologist_senior",
        coverage=0.9,
        label_bias={"Normal": 1.6, "Pneumonia": 1.6, "Effusion": 0.9},
        label_seed=22,
    )
    # indo V1／V2 標同一批圖 → 版本衝突；V2 多一個 EFFU 類別
    s.add_annotation_batch(
        indo,
        "V1",
        ["PNEU", "NORM"],
        indo_images,
        "annotator_indo_a",
        coverage=0.8,
        label_seed=33,
    )
    s.add_annotation_batch(
        indo,
        "V2",
        ["PNEU", "NORM", "EFFU"],
        indo_images,
        "annotator_indo_b",
        coverage=0.85,
        label_seed=44,
    )
    # TB-portal：唯一有 det 標註的來源
    s.add_annotation_batch(
        tb,
        "V1",
        ["TB", "Normal", "Pneumonia"],
        tb_images,
        "radiologist_senior",
        coverage=0.9,
        label_bias={"TB": 2.0},
        with_det=True,
        det_categories=["TB", "Pneumonia"],
        label_seed=55,
    )
    # DrLee：小寫命名，試 merge_identical 的極限
    s.add_annotation_batch(
        drlee,
        "V1",
        ["pneumonia", "normal"],
        drlee_images,
        "dr_lee",
        coverage=0.95,
        label_seed=66,
    )
    # aws V2（前處理批次）也有一份標註，讓 lineage 那條線有東西可看
    s.add_annotation_batch(
        aws,
        "V3",
        ["Pneumonia", "Normal"],
        aws_v2_images,
        "radiologist_junior",
        coverage=0.7,
        label_seed=77,
    )

    db.commit()

    counts = {
        row[0]: row[1]
        for row in db.execute(
            text(
                """
                SELECT 'original_sets', count(*) FROM original_sets
                UNION ALL SELECT 'image_batches', count(*) FROM image_batches
                UNION ALL SELECT 'annotation_batches', count(*) FROM annotation_batches
                UNION ALL SELECT 'images', count(*) FROM images
                UNION ALL SELECT 'image_subjects', count(*) FROM image_subjects
                UNION ALL SELECT 'categories', count(*) FROM categories
                UNION ALL SELECT 'cls_annotations', count(*) FROM cls_annotations
                UNION ALL SELECT 'det_annotations', count(*) FROM det_annotations
                UNION ALL SELECT 'image_lineage', count(*) FROM image_lineage
                UNION ALL SELECT 'annotators', count(*) FROM annotators
                """
            )
        ).all()
    }
    counts["objects_uploaded"] = s.uploaded
    return counts
