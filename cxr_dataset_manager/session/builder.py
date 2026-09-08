"""ManualSetSession —— 可變、可預覽、可反悔的探索介面（design_doc §2）。

Spec 是探索完之後的乾淨記錄，但沒有人能一次寫對。日常使用的介面是
這個 session：內部悄悄累積同一份 steps 清單，只是還沒 commit。

  * checkpoint / rollback 只動記憶體裡的 steps 清單長度，
    commit 之前完全不碰資料庫，commit 之後也不留探索紀錄；
  * compile() 產出乾淨的 spec（被 rollback 掉的分支早就被截掉了）；
  * commit() 才落庫，走的是跟 CLI `cxr build` 完全同一支 engine。

分支的處理：add_source / import_list 會開一條新分支並把它設為 head，
後續的 filter / dedup 都作用在 head 上。compile() 時若還有多條分支沒被
合併，會自動補一個 union step 把它們接起來——這個 step 會明明白白出現
在 spec 裡，不是隱形行為。
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from sqlalchemy.orm import Session

from cxr_dataset_manager.core import ops
from cxr_dataset_manager.core.engine import execute_spec
from cxr_dataset_manager.core.schema import (
    BuildSpec,
    FileNamesRef,
    CategoryMapStep,
    ConflictResolveStep,
    ConflictRule,
    DedupStep,
    ExceptStep,
    FilterStep,
    ImportListStep,
    IntersectStep,
    ManualConflictDecision,
    ManualOverrideStep,
    Override,
    SourceStep,
    Step,
    UnionStep,
)
from cxr_dataset_manager.core.types import CandidateSet, Catalog, SpecError, StepResult
from cxr_dataset_manager.db import crud
from cxr_dataset_manager.session import analyzer


# 超過這個筆數，清單就不再內嵌進 spec，改存資料庫、spec 只留 sha256。
# 200 筆的 spec 還讀得下去；上千筆之後 `cxr spec` 印出來的東西沒有人看得完，
# git diff 也失去意義。
INLINE_LIST_MAX = 200


@dataclass
class ImportReport:
    """`missing` 是樣本（上限 200 筆），`missing_count` 才是總數——
    上萬筆的清單不可能整份帶在報告裡。"""

    matched_count: int
    missing: list[str]
    missing_count: int
    requested: int
    on_missing: str

    def __repr__(self) -> str:  # pragma: no cover - REPL 友善
        return (
            f"<ImportReport matched={self.matched_count}/{self.requested} "
            f"missing={self.missing_count}>"
        )


class ManualSetSession:
    """一次探索。狀態全在記憶體，只有 commit() 會寫資料庫。"""

    def __init__(
        self,
        db: Session,
        name: str,
        catalog: Optional[Catalog] = None,
        on_warning: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.db = db
        self.name = name
        self.catalog = catalog or Catalog(db)
        # 警告要怎麼呈現由呼叫端決定：Notebook 直接 print，CLI 用 rich 上色，
        # 未來的圖形介面則收進自己的狀態裡呈現。邏輯只有一份。
        self.on_warning = on_warning or (lambda message: print(f"⚠️  {message}"))
        self.steps: list[Step] = []
        self.results: dict[str, CandidateSet] = {}
        self.reports: dict[str, StepResult] = {}
        self.checkpoints: dict[str, int] = {}
        self._checkpoint_heads: dict[str, Optional[str]] = {}
        self.action_log: list[dict[str, Any]] = []
        self.head: Optional[str] = None
        self._counter: dict[str, int] = {}
        self._last_import: Optional[ImportReport] = None

    # -- 內部 -------------------------------------------------------------

    def _next_id(self, op: str) -> str:
        self._counter[op] = self._counter.get(op, 0) + 1
        return f"{op}_{self._counter[op]}"

    def _open_branches(self) -> list[str]:
        """沒有被任何後續 step 當成 input 的 step——也就是還沒被合併的分支。"""
        consumed = {dep for step in self.steps for dep in step.input_ids()}
        return [s.id for s in self.steps if s.id not in consumed]

    def _apply(self, step: Step, file_names: Optional[list[str]] = None) -> "ManualSetSession":
        """執行一個 step 並接到 steps 清單尾巴。

        失敗時 steps 清單保持不變——探索中打錯一個參數不該讓整個
        session 進入壞掉的狀態，改一改重下就好。
        """
        inputs = [self.results[i] for i in step.input_ids()]
        op = ops.OPS[step.op]
        if file_names is not None:
            # 已經在記憶體裡，不必為了執行再去資料庫撈一次
            result = op(self.catalog, inputs, step, file_names)
        else:
            result = op(self.catalog, inputs, step)

        self.steps.append(step)
        self.results[step.id] = result.candidates
        self.reports[step.id] = result
        self.head = step.id
        self.action_log.append(
            {
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "action": step.op,
                "step_id": step.id,
                "counts": result.candidates.counts(),
                "warnings": result.warnings,
            }
        )
        for warning in result.warnings:
            self.on_warning(warning)
        return self

    def _require_head(self, action: str) -> str:
        if self.head is None:
            raise SpecError(f"{action} 需要先有資料——請先 add_source() 或 import_list()")
        return self.head

    @property
    def current(self) -> CandidateSet:
        if self.head is None:
            return CandidateSet()
        return self.results[self.head]

    # -- 來源 -------------------------------------------------------------

    def add_source(
        self, original_set: str, image_batch: Optional[str] = None,
        annotation_batch: Optional[str] = None,
    ) -> "ManualSetSession":
        return self._apply(
            SourceStep(
                id=self._next_id("source"),
                original_set=original_set,
                image_batch=image_batch,
                annotation_batch=annotation_batch,
            )
        )

    def import_list(
        self, original_set: str, image_batch: str, file_names: Iterable[str],
        on_missing: str = "error", source_note: Optional[str] = None,
    ) -> "ManualSetSession":
        """直接匯入外部 file_name 清單（design_doc §3）。

        探索階段建議先用 on_missing="warn" 看匹配結果，確認沒問題後，
        compile() 前再收緊成 "error"，正式 spec 對缺漏零容忍。
        """
        names = crud.normalize_file_names(file_names)
        step = ImportListStep(
            id=self._next_id("import_list"),
            original_set=original_set,
            image_batch=image_batch,
            on_missing=on_missing,  # type: ignore[arg-type]
            **self._list_payload(names, source_note),
        )
        self._apply(step, file_names=names)
        stats = self.reports[step.id].stats
        self._last_import = ImportReport(
            matched_count=stats["matched_count"],
            missing=stats["missing"],
            missing_count=stats["missing_count"],
            requested=stats["requested"],
            on_missing=on_missing,
        )
        return self

    def _list_payload(
        self, names: list[str], source_note: Optional[str] = None
    ) -> dict[str, Any]:
        """決定這份清單要內嵌進 spec，還是存進資料庫只留 sha256。

        註冊寫的是一列內容定址、不具名的資料——它不記錄誰試了什麼、
        什麼時候試的，所以不違反「探索不留痕」：留下的只是一份可被任何
        spec 引用的清單本體。
        """
        if len(names) <= INLINE_LIST_MAX:
            return {"file_names": names}
        digest, created = crud.register_import_list(self.db, names, source_note)
        if created:
            self.on_warning(
                f"清單有 {len(names)} 筆，已存進資料庫（sha256 {digest[:12]}…），"
                "spec 只會保留這個雜湊"
            )
        return {"file_names_ref": FileNamesRef(sha256=digest, source=source_note)}

    def last_import_report(self) -> Optional[ImportReport]:
        return self._last_import

    # -- 集合運算 ---------------------------------------------------------

    def union(self, inputs: Optional[list[str]] = None) -> "ManualSetSession":
        branches = inputs or self._open_branches()
        if len(branches) < 2:
            raise SpecError(f"union 需要至少兩條分支，目前只有 {branches}")
        return self._apply(UnionStep(id=self._next_id("union"), inputs=branches))

    def intersect(self, inputs: Optional[list[str]] = None) -> "ManualSetSession":
        branches = inputs or self._open_branches()
        return self._apply(IntersectStep(id=self._next_id("intersect"), inputs=branches))

    def exclude(self, inputs: Optional[list[str]] = None) -> "ManualSetSession":
        branches = inputs or self._open_branches()
        return self._apply(ExceptStep(id=self._next_id("except"), inputs=branches))

    # -- filter -----------------------------------------------------------

    def filter(self, criterion: str = "sample", input: Optional[str] = None, **kwargs) -> "ManualSetSession":
        names: Optional[list[str]] = None
        if criterion == "explicit_list" and "file_names" in kwargs:
            names = crud.normalize_file_names(kwargs.pop("file_names"))
            kwargs.update(self._list_payload(names, kwargs.pop("source_note", None)))
        return self._apply(
            FilterStep(
                id=self._next_id("filter"),
                input=input or self._require_head("filter"),
                criterion=criterion,  # type: ignore[arg-type]
                **kwargs,
            ),
            file_names=names,
        )

    def split(self, mod: int, keep_remainder: list[int], seed: str,
              key_field: str = "subject_id") -> "ManualSetSession":
        """filter(criterion='sample') 的白話版：決定性切割。"""
        return self.filter(
            criterion="sample", method="hash_mod", key_field=key_field,
            mod=mod, keep_remainder=keep_remainder, seed=seed,
        )

    # -- dedup ------------------------------------------------------------

    def find_duplicates(self) -> list[dict[str, Any]]:
        """攤開目前集合裡 blake3 相同的影像，連同各自帶的標註。"""
        return ops.find_duplicates(self.catalog, self.current)

    def dedup(
        self, source_priority: Optional[list[str]] = None, key: str = "blake3",
        keep: Optional[list[int]] = None,
    ) -> "ManualSetSession":
        return self._apply(
            DedupStep(
                id=self._next_id("dedup"),
                input=self._require_head("dedup"),
                key=key,  # type: ignore[arg-type]
                source_priority=source_priority or [],
                keep=keep or [],
            )
        )

    def keep_annotated_only(self) -> "ManualSetSession":
        """剔除沒有標註的影像——manual-set 不接受它們。"""
        return self.filter(criterion="annotated")

    # -- category mapping -------------------------------------------------

    def map_category(
        self, scope: Optional[str] = None, mapping: Optional[dict[str, str]] = None,
        *, merge_identical: bool = False, require_total: bool = False,
        full_mapping: Optional[dict[str, dict[str, str]]] = None,
    ) -> "ManualSetSession":
        """s.map_category("aws_images@V1", {"Pneumonia": "pneumonia"})

        scope 一定要寫清楚是哪個 annotation_batch 的 category——
        category 命名空間 scope 在 annotation_batch 底下，同名不同義是常態。
        """
        table = full_mapping or ({scope: mapping} if scope and mapping else {})
        # 探索時一次只映射一個 annotation_batch，所以這裡絕不能剔除未映射的標註——
        # 否則第一次 map_category 就會把還沒輪到的 batch 的標註全部丟光。
        # compile() 會把最後一個 category_map 收緊成 require_total=True。
        return self._apply(
            CategoryMapStep(
                id=self._next_id("category_map"),
                input=self._require_head("map_category"),
                mapping=table,
                merge_identical=merge_identical,
                require_total=require_total,
                drop_unmapped=False,
            )
        )

    def merge_identical_category(self) -> "ManualSetSession":
        """design_doc §3：先把完全同名的 local category 合併成同名的 target。"""
        return self.map_category(merge_identical=True)

    def preview_categories(self) -> dict[str, Any]:
        return analyzer.category_report(self.catalog, self.current)

    # -- 衝突 -------------------------------------------------------------

    def find_conflicts(self) -> list[ops.ConflictGroup]:
        return ops.find_conflicts(self.catalog, self.current)

    def conflict_summary(self) -> dict[str, Any]:
        return analyzer.conflict_summary(self.catalog, self.current)

    def _resolve(self, rule: ConflictRule) -> "ManualSetSession":
        # strict=False：探索階段先讓規則覆蓋不到的部分現形，
        # 而不是當場中斷；compile() 會把最後一步收緊成 strict
        return self._apply(
            ConflictResolveStep(
                id=self._next_id("conflict_resolve"),
                input=self._require_head("resolve_conflicts"),
                rules=[rule],
                strict=False,
            )
        )

    def resolve_conflicts_by_annotator_precedence(
        self, annotator_precedence: list[str]
    ) -> "ManualSetSession":
        return self._resolve(
            ConflictRule(rule="annotator_precedence", annotator_precedence=annotator_precedence)
        )

    def resolve_conflicts_by_annotation_version(
        self, version_precedence: list[str]
    ) -> "ManualSetSession":
        return self._resolve(
            ConflictRule(rule="batch_version_precedence", version_precedence=version_precedence)
        )

    def resolve_conflicts_by_score(self) -> "ManualSetSession":
        return self._resolve(ConflictRule(rule="highest_score"))

    def unresolved_conflicts(self) -> list[dict[str, Any]]:
        """規則覆蓋不到的衝突會在這裡現形，逼使用者正視（design_doc §6）。"""
        return [g.to_dict() for g in self.find_conflicts()]

    # -- 人工介入 ---------------------------------------------------------

    def override(self, overrides: list[dict[str, Any]]) -> "ManualSetSession":
        return self._apply(
            ManualOverrideStep(
                id=self._next_id("manual_override"),
                input=self._require_head("override"),
                overrides=[Override(**o) for o in overrides],
            )
        )

    def exclude_image(self, image_id: int, reason: str = "") -> "ManualSetSession":
        return self.override([
            {"action": "exclude_image", "image_id": image_id, "reason": reason}
        ])

    def override_one(
        self, include: bool, kind: str, target_id: int, reason: str = ""
    ) -> "ManualSetSession":
        """指名一個 id，納入或排除。kind 是 image / cls / det。"""
        verb = "include" if include else "exclude"
        if kind == "image":
            action = {"action": f"{verb}_image", "image_id": target_id}
        elif kind in ("cls", "det"):
            action = {
                "action": f"{verb}_annotation",
                "annotation_id": target_id,
                "annotation_kind": kind,
            }
        else:
            raise SpecError(f"不認得的對象 {kind!r}（可用：image / cls / det）")
        return self.override([{**action, "reason": reason}])

    def resolve_conflicts_by_manual_setting(
        self, designated_annotation_id: int, reason: str = "",
    ) -> "ManualSetSession":
        """人工指定衝突裡要留下哪一筆標註。"""
        return self._resolve(
            ConflictRule(
                rule="manual",
                decisions=[
                    ManualConflictDecision(
                        keep_annotation_id=designated_annotation_id, reason=reason
                    )
                ],
            )
        )

    # -- 預覽 -------------------------------------------------------------

    def preview(self) -> dict[str, Any]:
        """統計摘要，不是把上萬張圖列出來——探索才會快。"""
        previous = None
        if len(self.steps) >= 2:
            head_step = self.steps[-1]
            deps = head_step.input_ids()
            if deps:
                previous = self.results[deps[0]]
        stats = self.reports[self.head].stats if self.head else None
        return analyzer.summarize(self.catalog, self.current, previous, stats)

    # -- checkpoint / rollback --------------------------------------------

    def checkpoint(self, label: str) -> "ManualSetSession":
        """記下目前 steps 清單的長度與所在位置。

        位置也要記：有了 checkout 之後，head 不一定在清單尾端，
        只記長度的話 rollback 會把你丟到別條分支上。
        """
        self.checkpoints[label] = len(self.steps)
        self._checkpoint_heads[label] = self.head
        self.action_log.append(
            {
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "action": "checkpoint",
                "label": label,
                "steps": len(self.steps),
            }
        )
        return self

    def rollback(self, label: str) -> "ManualSetSession":
        """把 steps 清單截回 checkpoint 當時的長度。

        不需要撤銷任何資料庫寫入——這些操作在 commit 之前都只存在
        session 的記憶體狀態裡。
        """
        if label not in self.checkpoints:
            raise SpecError(f"沒有名為 '{label}' 的 checkpoint（現有: {list(self.checkpoints)}）")
        keep = self.checkpoints[label]
        dropped = [s.id for s in self.steps[keep:]]
        self.steps = self.steps[:keep]
        for step_id in dropped:
            self.results.pop(step_id, None)
            self.reports.pop(step_id, None)
        # 回到 checkpoint 當時所在的位置，而不是清單尾端
        remembered = self._checkpoint_heads.get(label)
        self.head = (
            remembered if remembered in self.results
            else (self.steps[-1].id if self.steps else None)
        )
        self.checkpoints = {k: v for k, v in self.checkpoints.items() if v <= keep}
        self._checkpoint_heads = {
            k: v for k, v in self._checkpoint_heads.items() if k in self.checkpoints
        }
        self.action_log.append(
            {
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "action": "rollback",
                "label": label,
                "dropped_steps": dropped,
            }
        )
        return self

    def checkout(self, step_id: str) -> "ManualSetSession":
        """把目前位置移到某個既有步驟。

        每個 source / import 都會開一條新分支，沒有這個就只能加工「剛好是
        目前位置」的那一條。切換不刪除任何東西——所有步驟都還在，只是
        接下來的 filter / dedup 會接在這一步後面。
        """
        if step_id not in self.results:
            raise SpecError(
                f"沒有名為 '{step_id}' 的步驟（現有：{', '.join(s.id for s in self.steps) or '無'}）"
            )
        self.head = step_id
        self.action_log.append(
            {
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "action": "checkout",
                "step_id": step_id,
            }
        )
        return self

    def undo(self) -> "ManualSetSession":
        """復原**目前所在**的那一步，位置退回它的 input。

        刻意不是「刪掉清單最後一筆」：有了 checkout 之後那會刪到別條分支上
        的東西，而你根本沒在看那裡。被其他步驟依賴的步驟不能拔掉，會擋下來
        並告訴你是誰在用。
        """
        if not self.steps or self.head is None:
            return self

        target = self.head
        dependents = [s.id for s in self.steps if target in s.input_ids()]
        if dependents:
            raise SpecError(
                f"'{target}' 還被 {', '.join(dependents)} 當成 input，不能復原。"
                f"要先復原 {dependents[-1]}，或用 checkout 換個位置。"
            )

        step = next(s for s in self.steps if s.id == target)
        inputs = step.input_ids()
        self.steps = [s for s in self.steps if s.id != target]
        self.results.pop(target, None)
        self.reports.pop(target, None)
        self.head = (
            inputs[0] if inputs else (self.steps[-1].id if self.steps else None)
        )
        self.action_log.append(
            {
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "action": "undo",
                "dropped_steps": [target],
            }
        )
        return self

    # -- compile / commit -------------------------------------------------

    def compile(self, *, strict_conflicts: bool = True, description: Optional[str] = None) -> BuildSpec:
        """把目前的 steps 清單整理成一份乾淨的 spec。

        三件事會在這裡補上，都會實際寫進 spec（不是隱形行為）：
          1. 還沒合併的分支自動接一個 union；
          2. 最後一個 conflict_resolve 收緊成 strict——正式 spec 對
             「沒有規則能裁決的衝突」零容忍；
          3. 最後一個 category_map 收緊成 require_total——漏掉的 local
             category 必須當場報錯，不是悄悄丟掉它的標註。
        """
        if not self.steps:
            raise SpecError("session 還沒有任何 step，沒東西可以 compile")

        steps: list[Step] = [s.model_copy(deep=True) for s in self.steps]
        consumed = {dep for s in steps for dep in s.input_ids()}
        open_branches = [s.id for s in steps if s.id not in consumed]
        final = open_branches[0]
        if len(open_branches) > 1:
            merged = UnionStep(id="auto_union_final", inputs=open_branches)
            steps.append(merged)
            final = merged.id

        if strict_conflicts:
            for step in reversed(steps):
                if isinstance(step, ConflictResolveStep):
                    step.strict = True
                    break
            # 探索時逐批累加映射（drop_unmapped=False），正式 spec 的最後一個
            # category_map 要收緊：漏掉的 local category 必須報錯，不是悄悄丟掉
            for step in reversed(steps):
                if isinstance(step, CategoryMapStep):
                    step.require_total = True
                    step.drop_unmapped = True
                    break

        return BuildSpec(
            description=description or f"session '{self.name}' compiled",
            steps=steps,
            final=final,
        )

    def commit(
        self, spec: Optional[BuildSpec] = None, version: str = "V1",
        manual_set_name: Optional[str] = None, dry_run: bool = False,
        author=None,
    ):
        """編譯出 spec、執行它，產出正式的 manual-set 版本。

        走的是跟 CLI `cxr build` 完全同一支 engine——探索用的路徑跟正式
        產出用的路徑不會分岔（design_doc §1 principle 7）。

        這一刻之前，這個 session 沒有在資料庫留下任何東西：試了幾次、
        rollback 過幾次、中途看過什麼，全都只存在記憶體裡，隨 session
        一起消失。會被記下來的只有「這份 spec 產出了這個版本」。
        """
        from cxr_dataset_manager.core.engine import build

        spec = spec or self.compile()
        return build(
            self.db, spec, manual_set_name or self.name, version,
            dry_run=dry_run, author=author,
        )

    # -- 其他 -------------------------------------------------------------

    def replay(self, spec: BuildSpec) -> "ManualSetSession":
        """載入一份既有 spec，接著往下探索（`cxr build` 的反向操作）。"""
        execution = execute_spec(self.db, spec, self.catalog)
        self.steps = list(spec.steps)
        self.results = execution.results
        self.reports = {}
        self.head = spec.final
        for report in execution.reports:
            self._counter[report.op] = self._counter.get(report.op, 0) + 1
        return self

    def describe(self) -> list[dict[str, Any]]:
        """每個步驟的現況。

        `open` 代表這一步還沒被任何後續步驟消費，也就是一條分支的末端——
        `union` 抓的就是這些，`compile()` 也會把剩下的自動接起來。
        """
        open_branches = set(self._open_branches())
        return [
            {
                "step_id": s.id,
                "op": s.op,
                "inputs": s.input_ids(),
                "counts": self.results[s.id].counts(),
                "head": s.id == self.head,
                "open": s.id in open_branches,
            }
            for s in self.steps
        ]
