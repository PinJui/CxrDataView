"""`filter` 的 predicate 求值器（design_doc.md §3）。

expression 用 Python 的比較語法寫，但**不是** eval——用 ast 解析後只放行
白名單節點，所以 spec 裡的字串永遠不可能執行任意程式碼。

影像本身的欄位：
    file_name, original_set, batch_version, width, height,
    blake3_hash, date_captured, subject_id, area

這張影像在**目前候選集合裡**的標註（會隨前面的步驟變動）：
    labels          local category 名稱，例如 ['Pneumonia']
    targets         映射後的 target category，映射之前是空的
    annotators      標註者名稱
    n_annotations   標註筆數

範例：
    date_captured >= '2022-01-01'
    width >= 1024 and height >= 1024
    'Pneumonia' in labels
    'pneumonia' in targets and not ('normal' in targets)
    n_annotations >= 2
    'radiologist_senior' in annotators
"""

from __future__ import annotations

import ast
import datetime as dt
import operator
import re
from typing import Any

from cxr_dataset_manager.core.types import Catalog, ImageMeta, SpecError

_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.Compare,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Call,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
)

_IMAGE_FIELDS = {
    "file_name",
    "original_set",
    "batch_version",
    "width",
    "height",
    "blake3_hash",
    "date_captured",
    "subject_id",
    "area",
}

# 這些是「這張影像在目前候選集合裡的標註」，所以求值時需要候選集合的上下文，
# 光看 ImageMeta 是算不出來的
_LABEL_FIELDS = {"labels", "targets", "annotators", "n_annotations"}

_FIELDS = _IMAGE_FIELDS | _LABEL_FIELDS

_DATE_FIELDS = {"date_captured"}


def compile_predicate(expression: str) -> ast.Expression:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:  # pragma: no cover - 訊息就是全部價值
        raise SpecError(f"predicate syntax error: {expression!r} ({exc.msg})") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise SpecError(
                f"predicate uses unsupported syntax {type(node).__name__}: {expression!r}"
                " (only comparisons, and/or/not, in and regex() are allowed)"
            )
        if isinstance(node, ast.Call) and (
            not isinstance(node.func, ast.Name) or node.func.id != "regex"
        ):
            raise SpecError("a predicate may only call regex(field, pattern)")
        if isinstance(node, ast.Name) and node.id not in _FIELDS | {
            "regex",
            "None",
            "True",
            "False",
        }:
            raise SpecError(
                f"predicate: unknown field {node.id!r}; available fields: {', '.join(sorted(_FIELDS))}"
            )
    return tree


def row_of(meta: ImageMeta, labels: dict[str, Any] | None = None) -> dict[str, Any]:
    """把一張影像攤成 predicate 看得到的欄位。

    labels 由呼叫端事先算好（見 ops._label_context）——每張圖都去掃一次
    候選集合的話，一個 filter 就是 O(影像數 × 標註數)。
    """
    return {
        **(
            labels
            or {"labels": [], "targets": [], "annotators": [], "n_annotations": 0}
        ),
        "file_name": meta.file_name,
        "original_set": meta.original_set_name,
        "batch_version": meta.batch_version,
        "width": meta.width,
        "height": meta.height,
        "blake3_hash": meta.blake3_hash,
        "date_captured": meta.date_captured,
        "subject_id": meta.subject_id,
        "area": meta.width * meta.height,
    }


def _coerce(field_value: Any, literal: Any) -> tuple[Any, Any]:
    """把字面值調成跟欄位同型別，讓 `date_captured >= '2022-01-01'` 成立。"""
    if isinstance(field_value, dt.date) and isinstance(literal, str):
        return field_value, dt.date.fromisoformat(literal)
    if isinstance(literal, dt.date) and isinstance(field_value, str):
        return dt.date.fromisoformat(field_value), literal
    return field_value, literal


def _eval(node: ast.AST, row: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, row)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return row.get(node.id)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e, row) for e in node.elts]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval(node.operand, row)
    if isinstance(node, ast.BoolOp):
        values = (_eval(v, row) for v in node.values)
        if isinstance(node.op, ast.And):
            return all(values)
        return any(values)
    if isinstance(node, ast.Call):
        field = _eval(node.args[0], row)
        pattern = _eval(node.args[1], row)
        if field is None:
            return False
        return re.search(str(pattern), str(field)) is not None
    if isinstance(node, ast.Compare):
        left = _eval(node.left, row)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, row)
            if isinstance(op, (ast.In, ast.NotIn)):
                hit = left in (right or [])
                result = hit if isinstance(op, ast.In) else not hit
            elif isinstance(op, ast.Is):
                result = left is right
            elif isinstance(op, ast.IsNot):
                result = left is not right
            else:
                if left is None or right is None:
                    # NULL 比較一律 false（跟 SQL 的三值邏輯一致），
                    # 但 == None / != None 走上面的 Is/IsNot 分支，仍可判斷
                    result = isinstance(op, ast.NotEq) and left != right
                else:
                    lv, rv = _coerce(left, right)
                    result = {
                        ast.Eq: operator.eq,
                        ast.NotEq: operator.ne,
                        ast.Lt: operator.lt,
                        ast.LtE: operator.le,
                        ast.Gt: operator.gt,
                        ast.GtE: operator.ge,
                    }[type(op)](lv, rv)
            if not result:
                return False
            left = right
        return True
    raise SpecError(
        f"predicate evaluation hit an unsupported node {type(node).__name__}"
    )


def evaluate(
    tree: ast.Expression,
    catalog: Catalog,
    image_id: int,
    labels: dict[str, Any] | None = None,
) -> bool:
    return bool(_eval(tree, row_of(catalog.image(image_id), labels)))
