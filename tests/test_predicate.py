"""predicate 求值器：能算對，而且不能執行任意程式碼。"""

import datetime as dt

import pytest

from cxr_dataset_manager.core import predicate as pred
from cxr_dataset_manager.core.types import SpecError


class FakeMeta:
    def __init__(self, **kw):
        self.file_name = kw.get("file_name", "a.png")
        self.original_set_name = kw.get("original_set", "aws_images")
        self.batch_version = kw.get("batch_version", "V1")
        self.width = kw.get("width", 512)
        self.height = kw.get("height", 512)
        self.blake3_hash = kw.get("blake3_hash", "0" * 64)
        self.date_captured = kw.get("date_captured", dt.date(2022, 6, 1))
        self.subject_id = kw.get("subject_id", "S1")


def run(expression: str, **kw) -> bool:
    tree = pred.compile_predicate(expression)
    return bool(pred._eval(tree, pred.row_of(FakeMeta(**kw))))


def test_date_string_is_coerced():
    assert run("date_captured >= '2022-01-01'")
    assert not run("date_captured >= '2023-01-01'")


def test_null_date_never_passes_a_range_test():
    assert not run("date_captured >= '2020-01-01'", date_captured=None)


def test_boolean_composition():
    assert run("width >= 256 and height >= 256")
    assert not run("width >= 256 and height >= 1024")
    assert run("original_set == 'aws_images' or width < 10")
    assert run("not (width < 100)")


def test_in_and_regex():
    assert run("original_set in ['aws_images', 'DrLee']")
    assert not run("original_set in ['DrLee']")
    assert run("regex(file_name, '^a')")
    assert not run("regex(file_name, '^zzz')")


def test_is_none_checks_work():
    assert run("subject_id is None", subject_id=None)
    assert run("subject_id is not None")


def test_derived_area_field():
    assert run("area >= 262144")


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo pwned')",
        "open('/etc/passwd').read()",
        "width.__class__",
        "[x for x in range(3)]",
        "lambda: 1",
    ],
)
def test_arbitrary_code_is_refused(expression):
    """spec 是會被 review 與重跑的資料，絕不能是可執行的程式碼。"""
    with pytest.raises(SpecError):
        pred.compile_predicate(expression)


def test_unknown_field_is_refused():
    with pytest.raises(SpecError, match="未知欄位"):
        pred.compile_predicate("patient_name == 'x'")
