"""上萬筆檔名清單的處理。

存在的理由：`file_names_ref`（spec 只存 sha256、本體在資料庫）原本是
半成品——引擎讀得出來，但沒有任何程式碼寫得進去，所以那條路徑必定失敗。
"""

import uuid



import pytest
from sqlalchemy import text

from cxr_dataset_manager.core.engine import Author, build, execute_spec
from cxr_dataset_manager.core.schema import BuildSpec
from cxr_dataset_manager.core.types import Catalog, SpecError
from cxr_dataset_manager.db import crud
from cxr_dataset_manager.session.builder import INLINE_LIST_MAX, ManualSetSession

TEST_AUTHOR = Author(name="pytest", email="pytest@example.com")


@pytest.fixture
def real_names(db):
    """DrLee@V1 裡真實存在的檔名。"""
    return list(
        db.execute(
            text(
                """
                SELECT i.file_name FROM images i
                JOIN image_batches ib ON ib.id = i.image_batch_id
                JOIN original_sets os ON os.id = ib.original_set_id
                WHERE os.name = 'DrLee' AND ib.version = 'V1'
                ORDER BY i.file_name
                """
            )
        ).scalars()
    )


def _big(real_names: list[str], total: int = 10_000) -> list[str]:
    ghosts = [f"GHOST_{i:05d}.png" for i in range(total - len(real_names))]
    return real_names + ghosts


# ---------------------------------------------------------------------------
# 儲存
# ---------------------------------------------------------------------------


def test_registering_is_content_addressed_and_idempotent(db, real_names):
    names = _big(real_names)
    first, created_first = crud.register_import_list(db, names, "test")
    second, created_second = crud.register_import_list(db, names, "另一個說明")

    assert first == second, "同樣的內容必須得到同樣的 sha256"
    assert created_first != created_second or not created_second
    assert not created_second, "第二次註冊不該再新增一列"
    assert crud.get_import_list(db, first) == names


def test_hash_ignores_whitespace_and_blank_lines(db, real_names):
    a = crud.import_list_sha256(real_names)
    b = crud.import_list_sha256([f"  {n}  " for n in real_names] + ["", "   "])
    assert a == b, "同一份清單多幾個空白或空行，不該變成另一份清單"


def test_duplicates_are_preserved_not_silently_collapsed(db):
    """清單裡的重複要留著，`import_list` 才報得出「你的清單有重複」。"""
    names = ["a.png", "b.png", "a.png"]
    digest, _ = crud.register_import_list(db, names, "dup test")
    assert crud.get_import_list(db, digest) == names


def test_empty_list_is_refused(db):
    with pytest.raises(SpecError, match="空的"):
        crud.register_import_list(db, ["", "   "])


# ---------------------------------------------------------------------------
# spec 執行
# ---------------------------------------------------------------------------


def test_spec_with_a_ref_resolves_and_runs(db, real_names):
    names = _big(real_names)
    digest, _ = crud.register_import_list(db, names, "ref test")

    spec = BuildSpec.from_yaml(
        f"""
        steps:
          - id: picked
            op: import_list
            original_set: DrLee
            image_batch: V1
            file_names_ref: {{sha256: "{digest}", source: big.txt}}
            on_missing: warn
        final: picked
        """
    )
    # spec 本身必須是小的——這才是 ref 存在的理由
    assert len(spec.to_yaml()) < 500

    execution = execute_spec(db, spec, Catalog(db))
    assert len(execution.final.images) == len(real_names)
    assert execution.reports[0].stats["missing_count"] == len(names) - len(real_names)


def test_an_unregistered_ref_fails_with_a_usable_message(db):
    spec = BuildSpec.from_yaml(
        """
        steps:
          - id: picked
            op: import_list
            original_set: DrLee
            image_batch: V1
            file_names_ref: {sha256: "%s", source: nope.txt}
        final: picked
        """
        % ("0" * 64)
    )
    with pytest.raises(SpecError, match="cxr lists add"):
        execute_spec(db, spec, Catalog(db))


def test_filter_explicit_list_also_supports_a_ref(db, real_names):
    """`pick` 有跟 import 一模一樣的長清單問題，所以 filter 也要能用 ref。"""
    digest, _ = crud.register_import_list(db, _big(real_names), "filter ref")
    spec = BuildSpec.from_yaml(
        f"""
        steps:
          - {{id: base, op: source, original_set: DrLee, image_batch: V1}}
          - id: narrowed
            op: filter
            input: base
            criterion: explicit_list
            file_names_ref: {{sha256: "{digest}"}}
            on_missing: warn
        final: narrowed
        """
    )
    execution = execute_spec(db, spec, Catalog(db))
    assert len(execution.final.images) == len(real_names)


def test_spec_must_pick_exactly_one_list_form(db):
    for body in (
        'file_names: [a.png]\n            file_names_ref: {sha256: "%s"}' % ("0" * 64),
        "",
    ):
        with pytest.raises(Exception, match="file_names"):
            BuildSpec.from_yaml(
                f"""
                steps:
                  - id: picked
                    op: import_list
                    original_set: DrLee
                    image_batch: V1
                    {body}
                final: picked
                """
            )


# ---------------------------------------------------------------------------
# session 自動切換
# ---------------------------------------------------------------------------


def test_short_lists_stay_inline_for_readability(db, real_names):
    session = ManualSetSession(db, name=f"pytest_{uuid.uuid4().hex[:8]}")
    short = real_names[:20]
    session.import_list(original_set="DrLee", image_batch="V1", file_names=short)
    step = session.steps[-1]
    assert step.file_names == short and step.file_names_ref is None


def test_long_lists_switch_to_a_ref_automatically(db, real_names):
    session = ManualSetSession(db, name=f"pytest_{uuid.uuid4().hex[:8]}")
    names = _big(real_names)
    assert len(names) > INLINE_LIST_MAX

    session.import_list(
        original_set="DrLee", image_batch="V1", file_names=names, on_missing="warn"
    )
    step = session.steps[-1]
    assert step.file_names is None
    assert step.file_names_ref is not None

    # 編譯出來的 spec 必須是小的，而且 sha256 指到的內容要完整
    spec = session.compile()
    assert len(spec.to_yaml()) < 600
    assert crud.get_import_list(db, step.file_names_ref.sha256) == names


def test_the_report_gives_the_real_missing_count_not_the_sample(db, real_names):
    """回歸測試：REPL 曾經拿 200 筆的樣本長度當作對不上的總數。"""
    session = ManualSetSession(db, name=f"pytest_{uuid.uuid4().hex[:8]}")
    names = _big(real_names)
    session.import_list(
        original_set="DrLee", image_batch="V1", file_names=names, on_missing="warn"
    )
    report = session.last_import_report()
    assert report.missing_count == len(names) - len(real_names)
    assert len(report.missing) <= 200 < report.missing_count


def test_a_long_list_survives_the_full_round_trip(db, real_names):
    """探索 → compile → build → 從資料庫讀回 spec → 重跑，結果要一致。"""
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    session = ManualSetSession(db, name=name)
    session.import_list(
        original_set="DrLee", image_batch="V1", file_names=_big(real_names),
        on_missing="warn",
    )
    # manual-set 不接受沒標註的影像，所以匯入之後要明確剔除掉那些
    session.keep_annotated_only()
    session.merge_identical_category()
    expected = set(session.current.images)
    try:
        result = build(db, session.compile(), name, "V1", author=TEST_AUTHOR)
        assert set(result.execution.final.images) == expected

        stored = crud.version_summary(db, result.manual_set_version_id)["spec"]
        # 存進資料庫的 spec 不該內嵌上萬個檔名
        assert "GHOST_00001.png" not in str(stored)
        replayed = execute_spec(db, BuildSpec.model_validate(stored), Catalog(db)).final
        assert set(replayed.images) == expected
    finally:
        db.execute(text("DELETE FROM manual_sets WHERE name = :n"), {"n": name})
        db.commit()
