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
from typing import Annotated, Any, Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

OnMissing = Literal["error", "warn", "ignore"]


class StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="step 名稱，同時是溯源表的 key")

    def input_ids(self) -> list[str]:
        return []


# ---------------------------------------------------------------------------
# 葉節點
# ---------------------------------------------------------------------------


class SourceStep(StepBase):
    """引入整批 image_batch／annotation_batch。"""

    op: Literal["source"] = "source"
    original_set: str
    image_batch: Optional[str] = None
    annotation_batch: Optional[str] = None
    with_annotations: bool = Field(
        True,
        description=(
            "image_batch 模式下，是否連同這些影像既有的標註一起帶進來。"
            "關掉就只拿影像本身（manual-set 允許沒有標註的影像）"
        ),
    )

    @model_validator(mode="after")
    def _one_of(self) -> "SourceStep":
        if bool(self.image_batch) == bool(self.annotation_batch):
            raise ValueError(
                f"step '{self.id}': source 必須且只能指定 image_batch 或 annotation_batch 其中一個"
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
    source: Optional[str] = Field(None, description="僅供人閱讀的說明，不用來定位檔案")


class ImportListStep(StepBase):
    """用外部 file_name 清單直接匯入（葉節點）。"""

    op: Literal["import_list"] = "import_list"
    original_set: str
    image_batch: str
    file_names: Optional[list[str]] = None
    file_names_ref: Optional[FileNamesRef] = None
    on_missing: OnMissing = "error"
    with_annotations: bool = Field(
        True, description="連同這些影像既有的標註一起帶入（同 source）"
    )

    @model_validator(mode="after")
    def _one_of(self) -> "ImportListStep":
        if bool(self.file_names) == bool(self.file_names_ref):
            raise ValueError(
                f"step '{self.id}': import_list 必須且只能指定 file_names 或 file_names_ref"
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
    mod: Optional[int] = Field(None, ge=2)
    keep_remainder: Optional[list[int]] = None
    seed: Optional[str] = None

    # criterion = predicate
    expression: Optional[str] = None

    # criterion = balance
    max_per_class: Optional[int] = Field(None, ge=1)
    by: Literal["target", "local"] = Field(
        "target", description="用映射後的 target category 還是原始的 local category 分類"
    )

    # criterion = explicit_list
    file_names: Optional[list[str]] = None
    file_names_ref: Optional[FileNamesRef] = None
    on_missing: OnMissing = "error"

    def input_ids(self) -> list[str]:
        return [self.input]

    @model_validator(mode="after")
    def _per_criterion(self) -> "FilterStep":
        if self.criterion == "sample":
            if self.mod is None or self.keep_remainder is None or self.seed is None:
                raise ValueError(
                    f"step '{self.id}': criterion=sample 需要 mod / keep_remainder / seed"
                )
            bad = [r for r in self.keep_remainder if not 0 <= r < self.mod]
            if bad:
                raise ValueError(
                    f"step '{self.id}': keep_remainder {bad} 超出 0..{self.mod - 1} 範圍"
                )
            if len(self.keep_remainder) >= self.mod:
                raise ValueError(
                    f"step '{self.id}': keep_remainder 涵蓋了全部餘數，這個 filter 不會篩掉任何東西"
                )
        elif self.criterion == "predicate":
            if not self.expression:
                raise ValueError(f"step '{self.id}': criterion=predicate 需要 expression")
        elif self.criterion == "annotated":
            pass  # 不需要參數：只留下目前集合裡帶有標註的影像
        elif self.criterion == "balance":
            if self.max_per_class is None:
                raise ValueError(f"step '{self.id}': criterion=balance 需要 max_per_class")
            if not self.seed:
                raise ValueError(
                    f"step '{self.id}': criterion=balance 需要 seed——挑哪幾張必須是決定性的"
                )
        else:
            if bool(self.file_names) == bool(self.file_names_ref):
                raise ValueError(
                    f"step '{self.id}': criterion=explicit_list 必須且只能指定 "
                    "file_names 或 file_names_ref"
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
        description="original_set 名稱由高到低；沒列到的來源排在所有列到的之後",
    )
    keep: list[int] = Field(
        default_factory=list,
        description=(
            "人工指定要留下的 image_id。用 `duplicates` 看過每一組的影像與標註之後"
            "再挑；沒被指定的組才交給 prefer_annotated 與 source_priority"
        ),
    )
    prefer_annotated: bool = Field(
        True,
        description=(
            "沒有人工指定的組，優先留有標註的那張。blake3 相同代表就是同一張照片，"
            "留下沒標註的那份等於白白丟掉標籤；兩張都有標註時才輪到 source_priority"
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
        False, description="先把完全同名的 local category 併成同名 target（design_doc §3）"
    )
    require_total: bool = Field(
        True, description="要求所有出現在候選集合裡的 local category 都有映射，否則報錯"
    )
    drop_unmapped: bool = Field(
        True,
        description=(
            "未映射的 local category，其標註是否要剔除。"
            "探索時一次只映射一個 annotation_batch，這時必須設 False，"
            "否則第一步就會把還沒輪到的 batch 的標註全部丟光"
        ),
    )

    def input_ids(self) -> list[str]:
        return [self.input]


class ConflictRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: Literal["manual", "annotator_precedence", "batch_version_precedence", "highest_score"]
    annotator_precedence: Optional[list[str]] = None
    version_precedence: Optional[list[str]] = None
    # rule = manual：(image ref, target_category) -> 指定留下的 annotation id
    decisions: Optional[list["ManualConflictDecision"]] = None

    @model_validator(mode="after")
    def _needs_params(self) -> "ConflictRule":
        if self.rule == "annotator_precedence" and not self.annotator_precedence:
            raise ValueError("rule=annotator_precedence 需要 annotator_precedence 清單")
        if self.rule == "batch_version_precedence" and not self.version_precedence:
            raise ValueError("rule=batch_version_precedence 需要 version_precedence 清單")
        if self.rule == "manual" and not self.decisions:
            raise ValueError("rule=manual 需要 decisions 清單")
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
    image_id: Optional[int] = None
    annotation_id: Optional[int] = None
    annotation_kind: Literal["cls", "det"] = "cls"
    reason: str = ""

    @model_validator(mode="after")
    def _target(self) -> "Override":
        if self.action.endswith("_image") and self.image_id is None:
            raise ValueError(f"action={self.action} 需要 image_id")
        if self.action.endswith("_annotation") and self.annotation_id is None:
            raise ValueError(f"action={self.action} 需要 annotation_id")
        return self


class ManualOverrideStep(StepBase):
    """人工指定納入／排除，最高優先權。"""

    op: Literal["manual_override"] = "manual_override"
    input: str
    overrides: list[Override] = Field(default_factory=list)

    def input_ids(self) -> list[str]:
        return [self.input]


Step = Annotated[
    Union[
        SourceStep,
        ImportListStep,
        UnionStep,
        IntersectStep,
        ExceptStep,
        FilterStep,
        DedupStep,
        CategoryMapStep,
        ConflictResolveStep,
        ManualOverrideStep,
    ],
    Field(discriminator="op"),
]


class BuildSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    description: Optional[str] = None
    steps: list[Step] = Field(..., min_length=1)
    final: str

    @model_validator(mode="after")
    def _validate_graph(self) -> "BuildSpec":
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"step id 重複: '{step.id}'")
            for dep in step.input_ids():
                if dep not in seen:
                    raise ValueError(
                        f"step '{step.id}' 的 input '{dep}' 尚未定義"
                        "（steps 必須依拓撲順序排列，不允許前向引用或環）"
                    )
            seen.add(step.id)
        if self.final not in seen:
            raise ValueError(f"final '{self.final}' 不是任何 step 的 id")
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
            self.to_jsonable(), sort_keys=False, allow_unicode=True, default_flow_style=False
        )

    def step(self, step_id: str) -> Step:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    @classmethod
    def from_yaml(cls, text: str) -> "BuildSpec":
        return cls.model_validate(yaml.safe_load(text))


ConflictRule.model_rebuild()
