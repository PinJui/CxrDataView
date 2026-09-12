"""Spec 執行引擎（design_doc.md §5）。

`execute_spec` 依 steps 的宣告順序跑完，每步用指定 input 的輸出當輸入；
`build` 再把結果落地，分成兩個地方：spec 本體寫成 YAML 放物件儲存的
manual-sets/{name}/annotations/{version}/spec.yaml，最終選取寫進資料庫的
manual_set_images / manual_set_cls_annotations / manual_set_det_annotations
（加上 target category 與映射），版本列上只留 spec 的 sha256。

spec 先寫、資料庫後 commit。兩者不在同一個交易裡，所以順序就是保證：
資料庫失敗只會留下一個沒人指向的 spec.yaml（下次同版本號建成時被覆蓋），
反過來則會產生一個「存在但重現不出來」的版本，那是不能接受的。

逐步的過程完全不存。探索不落庫，dry-run 不落庫也不寫物件，每一步的統計
也不存——「這份 spec 產出了這個版本」就是全部的記錄，其餘由重跑 spec 得出。

刻意沒有 content_hash 或快取層——每次 build 就是照順序整份重跑一遍
（design_doc §1 principle 2）。真的遇到 build 太慢再加節點快取，op 的介面不用改。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cxr_dataset_manager.core.ops import OPS, local_categories_in
from cxr_dataset_manager.core.schema import BuildSpec, Step
from cxr_dataset_manager.core.types import (
    CandidateSet,
    Catalog,
    Decision,
    SpecError,
    StepResult,
)
from cxr_dataset_manager.db import models as m
from cxr_dataset_manager.storage import get_store


@dataclass(frozen=True)
class Author:
    """誰建了這份資料集。內部工具，沒有帳號系統，就存名字與 email。"""

    name: str
    email: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise SpecError("the builder's name cannot be empty")
        if "@" not in self.email:
            raise SpecError(f"that email does not look right: {self.email!r}")

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name} <{self.email}>"


@dataclass
class StepReport:
    step_id: str
    step_index: int
    op: str
    params: dict[str, Any]
    input_step_ids: list[str]
    counts: dict[str, int]
    stats: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)


@dataclass
class ExecutionResult:
    final: CandidateSet
    results: dict[str, CandidateSet]
    reports: list[StepReport]
    catalog: Catalog

    @property
    def warnings(self) -> list[str]:
        return [w for r in self.reports for w in r.warnings]


def _step_params(step: Step) -> dict[str, Any]:
    return step.model_dump(mode="json", exclude={"id", "op"}, exclude_none=True)


def resolve_file_names(db: Session, step: Step) -> list[str] | None:
    """取出一個 step 要用的檔名清單（design_doc §3）。

    `import_list` 與 `filter/explicit_list` 都可能用 file_names_ref——
    spec 只存 sha256，本體在 manual_set_import_lists。查資料庫這件事留在
    engine，op 才能維持純函式。
    """
    names = getattr(step, "file_names", None)
    if names is not None:
        return list(names)
    ref = getattr(step, "file_names_ref", None)
    if ref is None:
        return None

    row = db.get(m.ManualSetImportList, ref.sha256)
    if row is None:
        raise SpecError(
            f"step '{step.id}': manual_set_import_lists has no list "
            f"with sha256={ref.sha256[:12]}…"
            f" (source: {ref.source or 'none'}). "
            "Register it in this database with `cxr lists add <file>`."
        )
    return list(row.file_names)


def execute_spec(
    db: Session, spec: BuildSpec, catalog: Catalog | None = None
) -> ExecutionResult:
    catalog = catalog or Catalog(db)
    results: dict[str, CandidateSet] = {}
    reports: list[StepReport] = []

    for index, step in enumerate(spec.steps):
        inputs = [results[i] for i in step.input_ids()]
        op = OPS[step.op]
        if step.op == "import_list" or (
            step.op == "filter" and step.criterion == "explicit_list"
        ):
            result: StepResult = op(catalog, inputs, step, resolve_file_names(db, step))  # type: ignore[arg-type]
        else:
            result = op(catalog, inputs, step)

        results[step.id] = result.candidates
        reports.append(
            StepReport(
                step_id=step.id,
                step_index=index,
                op=step.op,
                params=_step_params(step),
                input_step_ids=step.input_ids(),
                counts=result.candidates.counts(),
                stats=result.stats,
                warnings=result.warnings,
                decisions=result.decisions,
            )
        )

    return ExecutionResult(results[spec.final], results, reports, catalog)


# ---------------------------------------------------------------------------
# 落庫
# ---------------------------------------------------------------------------


@dataclass
class BuildResult:
    """dry-run 時 manual_set_version_id 與 spec_key 都是 None——
    試跑不會在資料庫或物件儲存留下任何東西。"""

    manual_set_version_id: int | None
    spec_key: str | None
    dry_run: bool
    execution: ExecutionResult
    target_categories: dict[str, int] = field(default_factory=dict)
    # `bucket/key` of the version's __meta__.md; None when build() got no meta
    meta_location: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        return self.execution.final.counts()


def _write_selection(
    db: Session, version_id: int, execution: ExecutionResult
) -> dict[str, int]:
    """把最終結果寫進既有 schema 的成員關係表。

    順序有意義：images 必須先寫，manual_set_cls/det_annotations 的複合外鍵
    才有得參照（「標註被納入時，對應的影像必須先被納入」）。
    """
    final = execution.final
    catalog = execution.catalog

    db.bulk_insert_mappings(
        m.ManualSetImage,
        [
            {"manual_set_version_id": version_id, "image_id": i}
            for i in sorted(final.images)
        ],
    )
    db.flush()

    if final.cls:
        db.bulk_insert_mappings(
            m.ManualSetClsAnnotation,
            [
                {
                    "manual_set_version_id": version_id,
                    "cls_annotation_id": a,
                    "image_id": catalog.cls(a).image_id,
                }
                for a in sorted(final.cls)
            ],
        )
    if final.det:
        db.bulk_insert_mappings(
            m.ManualSetDetAnnotation,
            [
                {
                    "manual_set_version_id": version_id,
                    "det_annotation_id": a,
                    "image_id": catalog.det(a).image_id,
                }
                for a in sorted(final.det)
            ],
        )
    db.flush()

    # target category 與映射：只寫「真的被用到」的 local category，
    # 避免留下一堆指向不存在標註的死映射。
    present = local_categories_in(catalog, final)
    used_targets = sorted(
        {final.category_targets[c] for c in present if c in final.category_targets}
    )
    target_ids: dict[str, int] = {}
    for name in used_targets:
        row = m.ManualSetTargetCategory(manual_set_version_id=version_id, name=name)
        db.add(row)
        db.flush()
        target_ids[name] = row.id

    mappings = [
        {
            "manual_set_version_id": version_id,
            "category_id": category_id,
            "target_category_id": target_ids[final.category_targets[category_id]],
        }
        for category_id in sorted(present)
        if category_id in final.category_targets
    ]
    if mappings:
        db.bulk_insert_mappings(m.ManualSetCategoryMapping, mappings)
    db.flush()
    return target_ids


def build(
    db: Session,
    spec: BuildSpec,
    manual_set_name: str,
    version: str,
    *,
    dry_run: bool = False,
    author: Author | None = None,
    catalog: Catalog | None = None,
    meta: Callable[[int], str] | None = None,
) -> BuildResult:
    """執行一份 spec，並（非 dry-run 時）產出正式的 manual-set version。

    整支包在單一 transaction 裡：任何一步失敗就整個 rollback，資料庫回到
    什麼都沒發生的狀態——不會留下半成品版本，也不會留下失敗的殘跡。

    dry-run 完全不寫資料庫：試跑就只是試跑。探索過程不留痕是刻意的，
    值得被記下來的只有「這份 spec 產出了這個版本」這件事。

    `meta` produces the version's __meta__.md. It is called with the new
    version id after the selection is flushed, so the statistics it reads are
    final, and before the commit, so a build that fails writes none. Like the
    spec, the file reaches object storage ahead of the commit.
    """
    if not dry_run:
        if author is None:
            raise SpecError(
                "commit needs to know who is building it. Pass --author-name / --author-email, "
                "or set CXR_AUTHOR_NAME and CXR_AUTHOR_EMAIL in .env."
            )
        # 先查一次只是為了在常見情況下快速給出好訊息；真正的保證是資料庫的
        # UNIQUE 約束，競態由下面的 IntegrityError 處理（樂觀鎖）。
        _assert_version_available(db, manual_set_name, version)

    try:
        execution = execute_spec(db, spec, catalog)
        _assert_annotations_are_mapped(execution)
        _assert_every_image_is_annotated(execution)

        if dry_run:
            db.rollback()
            return BuildResult(None, None, True, execution)

        assert author is not None
        # 先把 spec 放上物件儲存。這一步失敗就整個放棄，什麼都還沒寫進資料庫。
        spec_object_key = get_store().put_spec(manual_set_name, version, spec.to_yaml())

        manual_set = db.execute(
            select(m.ManualSet).where(m.ManualSet.name == manual_set_name)
        ).scalar_one_or_none()
        if manual_set is None:
            manual_set = m.ManualSet(name=manual_set_name)
            db.add(manual_set)
            db.flush()

        version_row = m.ManualSetVersion(
            manual_set_id=manual_set.id,
            version=version,
            created_by_name=author.name,
            created_by_email=author.email,
            spec_sha256=spec.sha256(),
        )
        db.add(version_row)
        db.flush()

        target_ids = _write_selection(db, version_row.id, execution)
        meta_location = None
        if meta is not None:
            meta_location = get_store().put_meta(
                "manual-set", manual_set_name, version, meta(version_row.id)
            )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # 樂觀鎖：不預先上鎖，撞到唯一約束才處理。兩個人同時 commit 同一個
        # 版本號時，資料庫擋下其中一個，這裡把它翻成看得懂的訊息。
        if "manual_set_versions" in str(exc.orig):
            owner = _version_owner(db, manual_set_name, version)
            raise SpecError(
                f"'{manual_set_name}@{version}' was just created by"
                + (f" {owner}. " if owner else " someone else. ")
                + "Versions are immutable records; pick another version number and retry "
                "(your exploration state is intact, just commit again with a new version)."
            ) from exc
        raise
    except BaseException:
        # BaseException, not Exception: `meta` may be asking a person, and a
        # Ctrl-C there must not leave the half-written version in the session.
        db.rollback()
        raise

    return BuildResult(
        version_row.id, spec_object_key, False, execution, target_ids, meta_location
    )


def _assert_annotations_are_mapped(execution: ExecutionResult) -> None:
    """帶著沒有 target category 的標註落庫，等於產出一份標籤不可用的資料集。

    spec 少寫一個 category_map、或漏了某個 annotation_batch 時就會這樣，
    而且不擋的話要到訓練腳本讀不到 class name 才會發現。寧可在這裡失敗。
    """
    final, catalog = execution.final, execution.catalog
    unmapped = sorted(
        {
            catalog.category(cid).ref
            for cid in local_categories_in(catalog, final)
            if cid not in final.category_targets
        }
    )
    if unmapped:
        raise SpecError(
            f"the final result has {len(unmapped)} local categories with no target category: "
            + ", ".join(unmapped[:10])
            + ("…" if len(unmapped) > 10 else "")
            + ". Add a category_map step to map them (or filter out their annotations)."
        )


def _assert_every_image_is_annotated(execution: ExecutionResult) -> None:
    """manual-set 是 training-ready 的資料集，不能有沒標註的影像。

    schema 有 deferred trigger 會擋，但那個錯誤訊息只講得出第一張出問題的圖。
    這裡先擋一次，說得出總共幾張、以及怎麼處理。
    """
    final, catalog = execution.final, execution.catalog
    annotated = {catalog.cls(a).image_id for a in final.cls} | {
        catalog.det(a).image_id for a in final.det
    }
    bare = sorted(final.images - annotated)
    if not bare:
        return
    sample = ", ".join(catalog.image(i).ref for i in bare[:5])
    raise SpecError(
        f"the final result has {len(bare)} images with no annotation ({sample}"
        + ("…" if len(bare) > 5 else "")
        + "). A manual-set is a training-ready dataset and does not accept unannotated images. "
        "Add a filter criterion=annotated step to drop them explicitly, "
        "or add an annotation batch that covers them."
    )


def _version_owner(db: Session, manual_set_name: str, version: str) -> str | None:
    """撞版本號時，告訴使用者是誰搶先建的。"""
    row = db.execute(
        select(m.ManualSetVersion.created_by_name, m.ManualSetVersion.created_by_email)
        .join(m.ManualSet, m.ManualSet.id == m.ManualSetVersion.manual_set_id)
        .where(
            m.ManualSet.name == manual_set_name, m.ManualSetVersion.version == version
        )
    ).one_or_none()
    return f"{row[0]} <{row[1]}>" if row else None


def _assert_version_available(db: Session, manual_set_name: str, version: str) -> None:
    exists = db.execute(
        select(m.ManualSetVersion.id)
        .join(m.ManualSet, m.ManualSet.id == m.ManualSetVersion.manual_set_id)
        .where(
            m.ManualSet.name == manual_set_name, m.ManualSetVersion.version == version
        )
    ).scalar_one_or_none()
    if exists is not None:
        raise SpecError(
            f"manual-set '{manual_set_name}' already has version '{version}'. "
            "Versions are immutable records; pick another version number instead of overwriting one."
        )
