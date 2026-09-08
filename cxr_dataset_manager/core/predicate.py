"""`filter` 的 predicate 求值器（design_doc.md §3）。

expression 用 Python 的比較語法寫，但**不是** eval——用 ast 解析後只放行
白名單節點，所以 spec 裡的字串永遠不可能執行任意程式碼。

可用欄位：
    file_name, original_set, batch_version, width, height,
    blake3_hash, date_captured, subject_id, area

範例：
    date_captured >= '2022-01-01'
    width >= 1024 and height >= 1024
    original_set in ['aws_images', 'DrLee'] and subject_id != None
    regex(file_name, '^DL_2023')
"""

from __future__ import annotations

import ast
import datetime as dt
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

_FIELDS = {
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

_DATE_FIELDS = {"date_captured"}


def compile_predicate(expression: str) -> ast.Expression:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:  # pragma: no cover - 訊息就是全部價值
        raise SpecError(f"predicate 語法錯誤: {expression!r} ({exc.msg})") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise SpecError(
                f"predicate 不支援的語法 {type(node).__name__}: {expression!r}"
                "（只允許比較、and/or/not、in、regex()）"
            )
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id != "regex":
                raise SpecError("predicate 只允許呼叫 regex(field, pattern)")
        if isinstance(node, ast.Name) and node.id not in _FIELDS | {"regex", "None", "True", "False"}:
            raise SpecError(
                f"predicate 未知欄位 {node.id!r}；可用欄位: {', '.join(sorted(_FIELDS))}"
            )
    return tree


def row_of(meta: ImageMeta) -> dict[str, Any]:
    return {
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
                    result = (isinstance(op, ast.NotEq) and left != right)
                else:
                    lv, rv = _coerce(left, right)
                    result = {
                        ast.Eq: lambda: lv == rv,
                        ast.NotEq: lambda: lv != rv,
                        ast.Lt: lambda: lv < rv,
                        ast.LtE: lambda: lv <= rv,
                        ast.Gt: lambda: lv > rv,
                        ast.GtE: lambda: lv >= rv,
                    }[type(op)]()
            if not result:
                return False
            left = right
        return True
    raise SpecError(f"predicate 求值遇到未支援的節點 {type(node).__name__}")


def evaluate(tree: ast.Expression, catalog: Catalog, image_id: int) -> bool:
    return bool(_eval(tree, row_of(catalog.image(image_id))))
