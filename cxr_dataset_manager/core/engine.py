"""Spec 執行引擎（design_doc.md §5）。

`execute_spec` 依 steps 的宣告順序跑完，每步用指定 input 的輸出當輸入；
`build` 再把結果落庫，而且只落兩樣東西：spec 原文 → manual_set_build_specs，
最終選取 → manual_set_images / manual_set_cls_annotations /
manual_set_det_annotations（加上 target category 與映射）。

逐步的過程完全不存。探索不落庫，dry-run 不落庫，每一步的統計也不落庫——
「這份 spec 產出了這個版本」就是全部的記錄，其餘由重跑 spec 得出。

刻意沒有 content_hash 或快取層——每次 build 就是照順序整份重跑一遍
（design_doc §1 principle 2）。真的遇到 build 太慢再加節點快取，op 的介面不用改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

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


@dataclass(frozen=True)
class Author:
    """誰建了這份資料集。內部工具，沒有帳號系統，就存名字與 email。"""

    name: str
    email: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise SpecError("建立者姓名不能是空的")
        if "@" not in self.email:
            raise SpecError(f"email 格式看起來不對：{self.email!r}")

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


def resolve_file_names(db: Session, step: Step) -> Optional[list[str]]:
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
            f"step '{step.id}': manual_set_import_lists 裡找不到 "
            f"sha256={ref.sha256[:12]}… 的清單"
            f"（來源說明: {ref.source or '無'}）。"
            "用 `cxr lists add <檔案>` 把它註冊進這個資料庫。"
        )
    return list(row.file_names)


def execute_spec(
    db: Session, spec: BuildSpec, catalog: Optional[Catalog] = None
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
    """dry-run 時 build_spec_id 與 manual_set_version_id 都是 None——
    試跑不會在資料庫留下任何東西。"""

    manual_set_version_id: Optional[int]
    build_spec_id: Optional[int]
    dry_run: bool
    execution: ExecutionResult
    target_categories: dict[str, int] = field(default_factory=dict)

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
        [{"manual_set_version_id": version_id, "image_id": i} for i in sorted(final.images)],
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
    used_targets = sorted({final.category_targets[c] for c in present if c in final.category_targets})
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
    author: Optional["Author"] = None,
    catalog: Optional[Catalog] = None,
) -> BuildResult:
    """執行一份 spec，並（非 dry-run 時）產出正式的 manual-set version。

    整支包在單一 transaction 裡：任何一步失敗就整個 rollback，資料庫回到
    什麼都沒發生的狀態——不會留下半成品版本，也不會留下失敗的殘跡。

    dry-run 完全不寫資料庫：試跑就只是試跑。探索過程不留痕是刻意的，
    值得被記下來的只有「這份 spec 產出了這個版本」這件事。
    """
    if not dry_run:
        if author is None:
            raise SpecError(
                "commit 需要知道是誰建的。用 --author-name / --author-email 指定，"
                "或在 .env 裡設 CXR_AUTHOR_NAME 與 CXR_AUTHOR_EMAIL。"
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

        manual_set = db.execute(
            select(m.ManualSet).where(m.ManualSet.name == manual_set_name)
        ).scalar_one_or_none()
        if manual_set is None:
            manual_set = m.ManualSet(name=manual_set_name)
            db.add(manual_set)
            db.flush()

        version_row = m.ManualSetVersion(manual_set_id=manual_set.id, version=version)
        db.add(version_row)
        db.flush()

        assert author is not None
        build_spec = m.ManualSetBuildSpec(
            manual_set_version_id=version_row.id,
            spec=spec.to_jsonable(),
            spec_sha256=spec.sha256(),
            created_by_name=author.name,
            created_by_email=author.email,
        )
        db.add(build_spec)
        db.flush()

        target_ids = _write_selection(db, version_row.id, execution)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # 樂觀鎖：不預先上鎖，撞到唯一約束才處理。兩個人同時 commit 同一個
        # 版本號時，資料庫擋下其中一個，這裡把它翻成看得懂的訊息。
        if "manual_set_versions" in str(exc.orig):
            owner = _version_owner(db, manual_set_name, version)
            raise SpecError(
                f"'{manual_set_name}@{version}' 剛剛被"
                + (f" {owner} " if owner else "別人 ")
                + "建立了。版本是不可變的記錄，請換一個版本號重試"
                "（你的探索狀態還在，改個版本號再 commit 一次即可）。"
            ) from exc
        raise
    except Exception:
        db.rollback()
        raise

    return BuildResult(version_row.id, build_spec.id, False, execution, target_ids)


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
            f"最終結果裡有 {len(unmapped)} 個 local category 沒有對應的 target category: "
            + ", ".join(unmapped[:10])
            + ("…" if len(unmapped) > 10 else "")
            + "。請補一個 category_map step 把它們映射掉（或用 filter 排除它們的標註）。"
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
        f"最終結果裡有 {len(bare)} 張影像沒有任何標註（{sample}"
        + ("…" if len(bare) > 5 else "")
        + "）。manual-set 是 training-ready 的資料集，不接受沒標註的影像。"
        "加一步 filter criterion=annotated 明確把它們剔除，"
        "或補上涵蓋這些影像的 annotation batch。"
    )


def _version_owner(db: Session, manual_set_name: str, version: str) -> Optional[str]:
    """撞版本號時，告訴使用者是誰搶先建的。"""
    row = db.execute(
        select(m.ManualSetBuildSpec.created_by_name, m.ManualSetBuildSpec.created_by_email)
        .join(
            m.ManualSetVersion,
            m.ManualSetVersion.id == m.ManualSetBuildSpec.manual_set_version_id,
        )
        .join(m.ManualSet, m.ManualSet.id == m.ManualSetVersion.manual_set_id)
        .where(m.ManualSet.name == manual_set_name, m.ManualSetVersion.version == version)
    ).one_or_none()
    return f"{row[0]} <{row[1]}>" if row else None


def _assert_version_available(db: Session, manual_set_name: str, version: str) -> None:
    exists = db.execute(
        select(m.ManualSetVersion.id)
        .join(m.ManualSet, m.ManualSet.id == m.ManualSetVersion.manual_set_id)
        .where(m.ManualSet.name == manual_set_name, m.ManualSetVersion.version == version)
    ).scalar_one_or_none()
    if exists is not None:
        raise SpecError(
            f"manual-set '{manual_set_name}' 已經有版本 '{version}'。"
            "版本是不可變的記錄，請換一個版本號，不要覆蓋既有版本。"
        )
