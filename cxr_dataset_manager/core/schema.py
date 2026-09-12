"""Spec 的 Pydantic 定義（design_doc.md §3）。

Spec 是「探索完之後」的乾淨記錄：一串有依賴關係的 steps。大多數情況是
線性的（每步一個 input），只有 union / intersect / except 會有多個 input。

這一層只管**語法**（欄位齊不齊、型別對不對、引用的 step 存不存在、
有沒有環）。語意錯誤（找不到 batch、category 沒映射到）留給 engine 執行
時報，因為那需要查資料庫。
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

OnMissing = Literal["error", "warn", "ignore"]


class StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        ..., min_length=1, description="step name; also how `cxr why` reports the step"
    )

    def input_ids(self) -> list[str]:
        return []


# ---------------------------------------------------------------------------
# 葉節點
# ---------------------------------------------------------------------------


class SourceStep(StepBase):
    """引入整批 image_batch／annotation_batch。"""

    op: Literal["source"] = "source"
    original_set: str
    image_batch: str | None = None
    annotation_batch: str | None = None
    with_annotations: bool = Field(
        True,
        description=(
            "In image_batch mode, also bring in the annotations these images already have. "
            "Turn it off to take the images alone"
        ),
    )

    @model_validator(mode="after")
    def _one_of(self) -> SourceStep:
        if bool(self.image_batch) == bool(self.annotation_batch):
            raise ValueError(
                f"step '{self.id}': source must name exactly one of image_batch or annotation_batch"
            )
        return self


class FileNamesRef(BaseModel):
    """清單很長時，spec 只存內容雜湊，本體放 manual_set_import_lists。

    `import_list` 與 `filter` 的 explicit_list 都可以用。上萬個檔名內嵌在
    spec 裡，`cxr spec` 印不出來、git diff 也沒法看，所以超過
    INLINE_LIST_MAX 筆時 session 會自動改用這種寫法。
    """

    model_config = ConfigDict(extra="forbid")

    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    source: str | None = Field(
        None, description="a note for humans; never used to locate the list"
    )


class ImportListStep(StepBase):
    """用外部 file_name 清單直接匯入（葉節點）。"""

    op: Literal["import_list"] = "import_list"
    original_set: str
    image_batch: str
    file_names: list[str] | None = None
    file_names_ref: FileNamesRef | None = None
    on_missing: OnMissing = "error"
    with_annotations: bool = Field(
        True,
        description="also bring in the annotations these images already have (as source does)",
    )

    @model_validator(mode="after")
    def _one_of(self) -> ImportListStep:
        if bool(self.file_names) == bool(self.file_names_ref):
            raise ValueError(
                f"step '{self.id}': import_list must name exactly one of file_names or file_names_ref"
            )
        return self


# ---------------------------------------------------------------------------
# 集合運算
# ---------------------------------------------------------------------------


class UnionStep(StepBase):
    op: Literal["union"] = "union"
    inputs: list[str] = Field(..., min_length=1)

    def input_ids(self) -> list[str]:
        return list(self.inputs)


class IntersectStep(StepBase):
    op: Literal["intersect"] = "intersect"
    inputs: list[str] = Field(..., min_length=2)

    def input_ids(self) -> list[str]:
        return list(self.inputs)


class ExceptStep(StepBase):
    """第一個 input 扣掉其餘 input。"""

    op: Literal["except"] = "except"
    inputs: list[str] = Field(..., min_length=2)

    def input_ids(self) -> list[str]:
        return list(self.inputs)


# ---------------------------------------------------------------------------
# filter
# ---------------------------------------------------------------------------


class FilterStep(StepBase):
    """從既有結果中依條件縮限（design_doc §3）。"""

    op: Literal["filter"] = "filter"
    input: str
    criterion: Literal["sample", "predicate", "explicit_list", "annotated", "balance"]

    # criterion = sample
    method: Literal["hash_mod"] = "hash_mod"
    key_field: Literal["subject_id", "image_id", "file_name"] = "subject_id"
    mod: int | None = Field(None, ge=2)
    keep_remainder: list[int] | None = None
    seed: str | None = None

    # criterion = predicate
    expression: str | None = None

    # criterion = balance
    max_per_class: int | None = Field(None, ge=1)
    by: Literal["target", "local"] = Field(
        "target",
        description="classify by the mapped target category or the original local category",
    )

    # criterion = explicit_list
    file_names: list[str] | None = None
    file_names_ref: FileNamesRef | None = None
    on_missing: OnMissing = "error"

    def input_ids(self) -> list[str]:
        return [self.input]

    @model_validator(mode="after")
    def _per_criterion(self) -> FilterStep:
        if self.criterion == "sample":
            if self.mod is None or self.keep_remainder is None or self.seed is None:
                raise ValueError(
                    f"step '{self.id}': criterion=sample needs mod / keep_remainder / seed"
                )
            bad = [r for r in self.keep_remainder if not 0 <= r < self.mod]
            if bad:
                raise ValueError(
                    f"step '{self.id}': keep_remainder {bad} is outside 0..{self.mod - 1}"
                )
            if len(self.keep_remainder) >= self.mod:
                raise ValueError(
                    f"step '{self.id}': keep_remainder covers every remainder, so this filter would drop nothing"
                )
        elif self.criterion == "predicate":
            if not self.expression:
                raise ValueError(
                    f"step '{self.id}': criterion=predicate needs expression"
                )
        elif self.criterion == "annotated":
            pass  # 不需要參數：只留下目前集合裡帶有標註的影像
        elif self.criterion == "balance":
            if self.max_per_class is None:
                raise ValueError(
                    f"step '{self.id}': criterion=balance needs max_per_class"
                )
            if not self.seed:
                raise ValueError(
                    f"step '{self.id}': criterion=balance needs seed — which images get picked must be deterministic"
                )
        else:
            if bool(self.file_names) == bool(self.file_names_ref):
                raise ValueError(
                    f"step '{self.id}': criterion=explicit_list must name exactly one of "
                    "file_names or file_names_ref"
                )
        return self


# ---------------------------------------------------------------------------
# dedup / category_map / conflict_resolve / manual_override
# ---------------------------------------------------------------------------


class DedupStep(StepBase):
    op: Literal["dedup"] = "dedup"
    input: str
    key: Literal["blake3"] = "blake3"
    source_priority: list[str] = Field(
        default_factory=list,
        description="original_set names, highest priority first; unlisted sources rank after every listed one",
    )
    keep: list[int] = Field(
        default_factory=list,
        description=(
            "image_ids to keep, chosen by hand after `duplicates` has shown each group's images "
            "and annotations; groups not pinned here fall back to prefer_annotated and source_priority"
        ),
    )
    prefer_annotated: bool = Field(
        True,
        description=(
            "In groups not pinned by hand, keep the annotated copy. Identical blake3 is the same picture, "
            "so keeping the unannotated one discards labels for nothing; source_priority decides only when both are annotated"
        ),
    )

    def input_ids(self) -> list[str]:
        return [self.input]


class CategoryMapStep(StepBase):
    """local category → target category。

    mapping 的 key 是 `{original_set}@{annotation_batch_version}`，
    因為 category 命名空間 scope 在 annotation_batch 底下，
    同名不同義是常態，不能只用類別名當 key。
    """

    op: Literal["category_map"] = "category_map"
    input: str
    mapping: dict[str, dict[str, str]] = Field(default_factory=dict)
    merge_identical: bool = Field(
        False,
        description="first merge identically named local categories into a target of the same name (design_doc §3)",
    )
    require_total: bool = Field(
        True,
        description="require every local category in the candidate set to be mapped, or fail",
    )
    drop_unmapped: bool = Field(
        True,
        description=(
            "whether to drop the annotations of unmapped local categories. "
            "Exploration maps one annotation_batch at a time and must set False, "
            "or the first mapping would drop every batch not mapped yet"
        ),
    )

    def input_ids(self) -> list[str]:
        return [self.input]


class ConflictRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: Literal[
        "manual", "annotator_precedence", "batch_version_precedence", "highest_score"
    ]
    annotator_precedence: list[str] | None = None
    version_precedence: list[str] | None = None
    # rule = manual：(image ref, target_category) -> 指定留下的 annotation id
    decisions: list[ManualConflictDecision] | None = None

    @model_validator(mode="after")
    def _needs_params(self) -> ConflictRule:
        if self.rule == "annotator_precedence" and not self.annotator_precedence:
            raise ValueError(
                "rule=annotator_precedence needs an annotator_precedence list"
            )
        if self.rule == "batch_version_precedence" and not self.version_precedence:
            raise ValueError(
                "rule=batch_version_precedence needs a version_precedence list"
            )
        if self.rule == "manual" and not self.decisions:
            raise ValueError("rule=manual needs a decisions list")
        return self


class ManualConflictDecision(BaseModel):
    """人工指定衝突裡要留下哪一筆標註。

    只需要 annotation_id：一筆標註只屬於一張影像，影像資訊是冗餘的，
    而冗餘欄位就是有一天會跟本體對不起來的欄位。
    """

    model_config = ConfigDict(extra="forbid")

    keep_annotation_id: int
    reason: str = ""


class ConflictResolveStep(StepBase):
    """依規則裁決同一 (image, target_category) 上的矛盾標註。

    規則依序套用，前一條裁決不了的才交給下一條。`strict=True` 時，
    所有規則跑完仍未解決的衝突會讓整個 build 失敗——刻意不做靜默
    fallback，逼使用者正視（design_doc §6）。
    """

    op: Literal["conflict_resolve"] = "conflict_resolve"
    input: str
    rules: list[ConflictRule] = Field(default_factory=list)
    strict: bool = True

    def input_ids(self) -> list[str]:
        return [self.input]


class Override(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "exclude_image", "include_image", "exclude_annotation", "include_annotation"
    ]
    # 一律用 id 定位。檔名不是全域唯一（UNIQUE 只 scope 在 image_batch 底下），
    # 用 original_set/版本/檔名 這種複合字串就得解析、比對，每一步都是出錯的機會。
    image_id: int | None = None
    annotation_id: int | None = None
    annotation_kind: Literal["cls", "det"] = "cls"
    reason: str = ""

    @model_validator(mode="after")
    def _target(self) -> Override:
        if self.action.endswith("_image") and self.image_id is None:
            raise ValueError(f"action={self.action} needs image_id")
        if self.action.endswith("_annotation") and self.annotation_id is None:
            raise ValueError(f"action={self.action} needs annotation_id")
        return self


class ManualOverrideStep(StepBase):
    """人工指定納入／排除，最高優先權。"""

    op: Literal["manual_override"] = "manual_override"
    input: str
    overrides: list[Override] = Field(default_factory=list)

    def input_ids(self) -> list[str]:
        return [self.input]


Step = Annotated[
    SourceStep
    | ImportListStep
    | UnionStep
    | IntersectStep
    | ExceptStep
    | FilterStep
    | DedupStep
    | CategoryMapStep
    | ConflictResolveStep
    | ManualOverrideStep,
    Field(discriminator="op"),
]


class BuildSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    description: str | None = None
    steps: list[Step] = Field(..., min_length=1)
    final: str

    @model_validator(mode="after")
    def _validate_graph(self) -> BuildSpec:
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"duplicate step id: '{step.id}'")
            for dep in step.input_ids():
                if dep not in seen:
                    raise ValueError(
                        f"step '{step.id}' uses input '{dep}' before it is defined"
                        " (steps must be in topological order; no forward references or cycles)"
                    )
            seen.add(step.id)
        if self.final not in seen:
            raise ValueError(f"final '{self.final}' is not the id of any step")
        return self

    # -- 序列化 -----------------------------------------------------------

    def to_jsonable(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def canonical_json(self) -> str:
        return json.dumps(self.to_jsonable(), sort_keys=True, separators=(",", ":"))

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.to_jsonable(),
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )

    def step(self, step_id: str) -> Step:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    @classmethod
    def from_yaml(cls, text: str) -> BuildSpec:
        return cls.model_validate(yaml.safe_load(text))


ConflictRule.model_rebuild()
