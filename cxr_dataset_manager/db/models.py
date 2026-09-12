"""SQLAlchemy ORM 結構，對應 db/schema.sql。

這裡只描述結構，不放任何業務邏輯——dedup / filter / 衝突解決規則都是
core/ops.py 裡的純函式（見 design_doc.md §1 principle 1）。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# =========================================================
# original-set 側：真正擁有影像／標註本體的表
# =========================================================


class OriginalSet(Base):
    __tablename__ = "original_sets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    image_batches: Mapped[list[ImageBatch]] = relationship(
        back_populates="original_set", cascade="all, delete-orphan"
    )
    annotation_batches: Mapped[list[AnnotationBatch]] = relationship(
        back_populates="original_set", cascade="all, delete-orphan"
    )


class ImageBatch(Base):
    __tablename__ = "image_batches"
    __table_args__ = (UniqueConstraint("original_set_id", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    original_set_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("original_sets.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    original_set: Mapped[OriginalSet] = relationship(back_populates="image_batches")
    images: Mapped[list[Image]] = relationship(
        back_populates="image_batch", cascade="all, delete-orphan"
    )


class AnnotationBatch(Base):
    __tablename__ = "annotation_batches"
    __table_args__ = (UniqueConstraint("original_set_id", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    original_set_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("original_sets.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    original_set: Mapped[OriginalSet] = relationship(
        back_populates="annotation_batches"
    )
    categories: Mapped[list[Category]] = relationship(
        back_populates="annotation_batch", cascade="all, delete-orphan"
    )


class License(Base):
    """全域共用的授權詞彙表（不 scope 在 original_set 之下）。"""

    __tablename__ = "licenses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    url: Mapped[str | None] = mapped_column(Text)


class Image(Base):
    __tablename__ = "images"
    __table_args__ = (
        CheckConstraint("height > 0"),
        CheckConstraint("width > 0"),
        UniqueConstraint("image_batch_id", "file_name"),
        UniqueConstraint("id", "image_batch_id"),  # 供其他表複合外鍵引用
        Index("idx_images_image_batch_id", "image_batch_id"),
        Index("idx_images_blake3_hash", "blake3_hash"),
        Index("idx_images_license_id", "license_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    image_batch_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("image_batches.id", ondelete="CASCADE"), nullable=False
    )
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    blake3_hash: Mapped[str | None] = mapped_column(CHAR(64))
    license_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("licenses.id", ondelete="RESTRICT")
    )
    date_captured: Mapped[dt.date | None] = mapped_column(Date)

    image_batch: Mapped[ImageBatch] = relationship(back_populates="images")
    subject: Mapped[ImageSubject | None] = relationship(
        back_populates="image", uselist=False, cascade="all, delete-orphan"
    )


class ImageLineage(Base):
    """影像血緣：某張影像經前處理衍生出另一張影像。"""

    __tablename__ = "image_lineage"
    __table_args__ = (Index("idx_image_lineage_child", "child_image_id"),)

    parent_image_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("images.id", ondelete="CASCADE"), primary_key=True
    )
    child_image_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("images.id", ondelete="CASCADE"), primary_key=True
    )


class Annotator(Base):
    """標註者名冊——全域表，才能跨版本／跨資料集統計同一位標註者。"""

    __tablename__ = "annotators"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class Category(Base):
    """每個 annotation_batch 各自的類別命名空間（同名不同義是常態）。"""

    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("annotation_batch_id", "id"),
        UniqueConstraint("annotation_batch_id", "name"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    annotation_batch_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("annotation_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    supercategory: Mapped[str | None] = mapped_column(Text)

    annotation_batch: Mapped[AnnotationBatch] = relationship(
        back_populates="categories"
    )


class ClsAnnotation(Base):
    __tablename__ = "cls_annotations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["annotation_batch_id", "category_id"],
            ["categories.annotation_batch_id", "categories.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "image_id"),
        # 一個 batch 裡不該有同一筆主張出現兩次；這些欄位都 NOT NULL，
        # 所以一般的 UNIQUE 語意就夠了
        UniqueConstraint(
            "annotation_batch_id", "image_id", "category_id", "annotator_id", "score"
        ),
        Index("idx_cls_annotations_batch", "annotation_batch_id"),
        Index("idx_cls_annotations_image", "image_id"),
        Index("idx_cls_annotations_category", "category_id"),
        Index("idx_cls_annotations_annotator", "annotator_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    annotation_batch_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    image_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("images.id", ondelete="RESTRICT"), nullable=False
    )
    category_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric, nullable=False, server_default="0")
    annotator_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("annotators.id", ondelete="RESTRICT"), nullable=False
    )


class DetAnnotation(Base):
    __tablename__ = "det_annotations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["annotation_batch_id", "category_id"],
            ["categories.annotation_batch_id", "categories.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("array_length(bbox, 1) = 4"),
        CheckConstraint("iscrowd IN (0, 1)"),
        UniqueConstraint("id", "image_id"),
        # 同樣的規則。score 與 segmentation 可為 NULL，而 Postgres 預設把 NULL
        # 視為互不相同——正好會放過最容易重複的那種列（沒有分數也沒有遮罩的
        # 框），所以要 postgresql_nulls_not_distinct。
        UniqueConstraint(
            "annotation_batch_id",
            "image_id",
            "category_id",
            "annotator_id",
            "score",
            "bbox",
            "segmentation",
            "iscrowd",
            postgresql_nulls_not_distinct=True,
        ),
        Index("idx_det_annotations_batch", "annotation_batch_id"),
        Index("idx_det_annotations_image", "image_id"),
        Index("idx_det_annotations_category", "category_id"),
        Index("idx_det_annotations_annotator", "annotator_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    annotation_batch_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    image_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("images.id", ondelete="RESTRICT"), nullable=False
    )
    category_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bbox: Mapped[list[Decimal]] = mapped_column(ARRAY(Numeric), nullable=False)
    segmentation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    iscrowd: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )
    score: Mapped[Decimal | None] = mapped_column(Numeric)
    annotator_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("annotators.id", ondelete="RESTRICT"), nullable=False
    )


# =========================================================
# manual-set 側：只做「選取」，不複製任何資料本體
# =========================================================


class ManualSet(Base):
    __tablename__ = "manual_sets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    versions: Mapped[list[ManualSetVersion]] = relationship(
        back_populates="manual_set", cascade="all, delete-orphan"
    )


class ManualSetVersion(Base):
    __tablename__ = "manual_set_versions"
    __table_args__ = (UniqueConstraint("manual_set_id", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    manual_set_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("manual_sets.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[str] = mapped_column(Text, nullable=False)
    # 刻意不指向 annotators：那張表記的是「誰標註的」，跟「誰建了這份資料集」
    # 是兩回事，混在一起會污染標註者統計
    created_by_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_email: Mapped[str] = mapped_column(Text, nullable=False)
    # spec 本體在物件儲存（manual-sets/{name}/annotations/{version}/spec.yaml），
    # 這裡只留指紋——用來判斷兩個版本是不是同一份配方，以及偵測 spec.yaml
    # 事後被改過或不見了。NULL 表示這版本是匯入的，本來就沒有 spec。
    spec_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    manual_set: Mapped[ManualSet] = relationship(back_populates="versions")


class ManualSetImage(Base):
    """允許影像沒有任何標註（尚未標記）也能被選入。"""

    __tablename__ = "manual_set_images"
    __table_args__ = (
        UniqueConstraint("manual_set_version_id", "image_id"),
        Index("idx_manual_set_images_image", "image_id"),
    )

    manual_set_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("manual_set_versions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    image_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("images.id", ondelete="RESTRICT"), primary_key=True
    )


class ManualSetClsAnnotation(Base):
    """複合外鍵同時保證 ① image_id 是該 annotation 真正的影像
    ② 該影像已先被選入同一個 manual-set 版本。"""

    __tablename__ = "manual_set_cls_annotations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["cls_annotation_id", "image_id"],
            ["cls_annotations.id", "cls_annotations.image_id"],
        ),
        ForeignKeyConstraint(
            ["manual_set_version_id", "image_id"],
            ["manual_set_images.manual_set_version_id", "manual_set_images.image_id"],
        ),
        Index("idx_manual_set_cls_ann_image", "manual_set_version_id", "image_id"),
    )

    manual_set_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("manual_set_versions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    cls_annotation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    image_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ManualSetDetAnnotation(Base):
    __tablename__ = "manual_set_det_annotations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["det_annotation_id", "image_id"],
            ["det_annotations.id", "det_annotations.image_id"],
        ),
        ForeignKeyConstraint(
            ["manual_set_version_id", "image_id"],
            ["manual_set_images.manual_set_version_id", "manual_set_images.image_id"],
        ),
        Index("idx_manual_set_det_ann_image", "manual_set_version_id", "image_id"),
    )

    manual_set_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("manual_set_versions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    det_annotation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    image_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ManualSetTargetCategory(Base):
    """這個 version 自己定義的 training-ready target category 清單。"""

    __tablename__ = "manual_set_target_categories"
    __table_args__ = (
        UniqueConstraint("manual_set_version_id", "name"),
        UniqueConstraint("id", "manual_set_version_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    manual_set_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("manual_set_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)


class ManualSetCategoryMapping(Base):
    """local category → target category，scope 完全綁定在 version 上。"""

    __tablename__ = "manual_set_category_mappings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["target_category_id", "manual_set_version_id"],
            [
                "manual_set_target_categories.id",
                "manual_set_target_categories.manual_set_version_id",
            ],
            ondelete="CASCADE",
        ),
    )

    manual_set_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("manual_set_versions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    category_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True
    )
    target_category_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


# =========================================================
# 建構系統擴充（design_doc.md §3）
# =========================================================


class ImageSubject(Base):
    """design_doc §1 principle 5 病患／受試者識別，全域表，用於避免 patient-level leakage。"""

    __tablename__ = "image_subjects"
    __table_args__ = (Index("idx_image_subjects_subject", "subject_id"),)

    image_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("images.id", ondelete="CASCADE"), primary_key=True
    )
    subject_id: Mapped[str] = mapped_column(Text, nullable=False)

    image: Mapped[Image] = relationship(back_populates="subject")


class ManualSetImportList(Base):
    """design_doc §3 外部 file_name 清單本體，spec 只存 sha256。"""

    __tablename__ = "manual_set_import_lists"

    sha256: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    file_names: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    source_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
