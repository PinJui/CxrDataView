"""探索式 session：checkpoint/rollback 只動記憶體，compile 產出乾淨 spec。"""

import uuid

import pytest
from sqlalchemy import text

from cxr_dataset_manager.core.engine import Author, execute_spec
from cxr_dataset_manager.core.schema import BuildSpec
from cxr_dataset_manager.core.types import Catalog, SpecError
from cxr_dataset_manager.db import crud
from cxr_dataset_manager.session.builder import ManualSetSession


@pytest.fixture
def session(db):
    return ManualSetSession(db, name=f"pytest_{uuid.uuid4().hex[:8]}")


def test_rollback_restores_the_exact_previous_state(session):
    session.add_source(original_set="aws_images", annotation_batch="V1")
    session.checkpoint("start")
    before = dict(session.current.counts())
    steps_before = len(session.steps)

    session.split(mod=3, keep_remainder=[0], seed="try-a-third")
    assert session.current.counts() != before

    session.rollback("start")
    assert session.current.counts() == before
    assert len(session.steps) == steps_before


def test_rolled_back_branches_never_reach_the_spec(session):
    session.add_source(original_set="aws_images", annotation_batch="V1")
    session.checkpoint("cp")
    session.split(mod=2, keep_remainder=[0], seed="abandoned-attempt")
    session.rollback("cp")
    session.split(mod=2, keep_remainder=[1], seed="the-real-one")

    spec = session.compile(strict_conflicts=False)
    seeds = [s.seed for s in spec.steps if getattr(s, "seed", None)]
    assert seeds == ["the-real-one"]


def test_the_full_exploration_log_is_kept_separately(session):
    session.add_source(original_set="aws_images", annotation_batch="V1")
    session.checkpoint("cp")
    session.split(mod=2, keep_remainder=[0], seed="abandoned-attempt")
    session.rollback("cp")

    actions = [entry["action"] for entry in session.action_log]
    assert "rollback" in actions and "checkpoint" in actions


def test_open_branches_are_unioned_explicitly_at_compile(session):
    session.add_source(original_set="aws_images", annotation_batch="V1")
    session.add_source(original_set="DrLee", annotation_batch="V1")
    spec = session.compile(strict_conflicts=False)
    final = spec.step(spec.final)
    assert final.op == "union", "沒合併的分支要補一個看得見的 union，不是隱形行為"
    assert len(final.inputs) == 2


def test_compiled_spec_reproduces_the_session_state(db, session):
    session.add_source(original_set="aws_images", annotation_batch="V1")
    session.add_source(original_set="TB-portal", annotation_batch="V1")
    session.union()
    session.dedup(source_priority=["TB-portal", "aws_images"])
    session.merge_identical_category()
    expected = session.current

    spec = session.compile(strict_conflicts=False)
    replayed = execute_spec(db, spec, Catalog(db)).final
    assert replayed.images == expected.images
    assert replayed.cls == expected.cls


def test_compile_tightens_conflict_handling(session):
    session.add_source(original_set="aws_images", annotation_batch="V1")
    session.add_source(original_set="aws_images", annotation_batch="V2")
    session.union()
    session.merge_identical_category()
    session.resolve_conflicts_by_annotator_precedence(
        ["radiologist_senior", "radiologist_junior"]
    )
    # 探索時寬鬆（先看規則覆蓋到哪），compile 出來的正式 spec 要收緊
    assert session.steps[-1].strict is False
    assert session.compile().step("conflict_resolve_1").strict is True


def test_import_report_lets_you_fix_the_whole_list_at_once(session, catalog):
    batch = catalog.resolve_image_batch("DrLee", "V1")
    names = [catalog.image(i).file_name for i in catalog.load_image_batch(batch)[:3]]
    session.import_list(
        original_set="DrLee",
        image_batch="V1",
        file_names=names + ["ghost_a.png", "ghost_b.png"],
        on_missing="warn",
    )
    report = session.last_import_report()
    assert report.matched_count == 3
    assert sorted(report.missing) == ["ghost_a.png", "ghost_b.png"]


def test_a_failed_step_leaves_the_session_usable(session):
    session.add_source(original_set="aws_images", annotation_batch="V1")
    good = len(session.steps)
    with pytest.raises(SpecError):
        session.import_list(
            original_set="DrLee",
            image_batch="V1",
            file_names=["ghost.png"],
            on_missing="error",
        )
    assert len(session.steps) == good, "打錯一個參數不該把整個 session 弄壞"
    session.split(mod=2, keep_remainder=[0], seed="still-works")
    assert session.head is not None


def test_commit_leaves_no_trace_of_the_exploration(db, session):
    """探索過程完全不落庫：試了什麼、rollback 過幾次，commit 之後都查不到。

    留下來的只有「這份 spec 產出了這個版本」——被 rollback 掉的分支
    連 spec 都進不去，更不會有獨立的探索歷史表。
    """
    session.add_source(original_set="TB-portal", annotation_batch="V1")
    session.checkpoint("cp")
    session.split(mod=2, keep_remainder=[0], seed="abandoned")
    session.rollback("cp")
    session.merge_identical_category()
    try:
        result = session.commit(
            version="V1", author=Author(name="pytest", email="pytest@example.com")
        )
        assert result.manual_set_version_id is not None
        assert result.spec_key is not None

        stored = BuildSpec.from_yaml(
            crud.version_summary(db, result.manual_set_version_id)["spec_yaml"]
        )
        seeds = [getattr(step, "seed", None) for step in stored.steps]
        assert "abandoned" not in seeds

        # 一個版本一份 spec，位置是算出來的，不會有第二份
        assert result.spec_key == f"{session.name}/annotations/V1/spec.yaml"
    finally:
        db.execute(text("DELETE FROM manual_sets WHERE name = :n"), {"n": session.name})
        db.commit()


def test_undo_removes_the_step_you_are_standing_on(session):
    """回歸測試：undo 原本刪的是清單最後一筆。

    有了 checkout 之後那可能是別條分支上的東西——你站在 source_1，
    打 undo 卻刪掉 source_2，而且你根本沒在看那裡。
    """
    session.add_source(original_set="aws_images", annotation_batch="V1")
    session.add_source(original_set="DrLee", annotation_batch="V1")
    session.checkout("source_1")

    session.undo()
    remaining = [s.id for s in session.steps]
    assert remaining == ["source_2"], "該被移除的是 head 所在的 source_1"


def test_undo_refuses_to_break_a_dependency(session):
    """拔掉別人正在用的步驟會讓圖壞掉，所以要擋。"""
    session.add_source(original_set="aws_images", annotation_batch="V1")
    session.split(mod=2, keep_remainder=[0], seed="s")
    session.checkout("source_1")

    with pytest.raises(SpecError, match="still used as input by filter_1"):
        session.undo()
    assert len(session.steps) == 2


def test_undo_moves_head_to_the_input_of_what_it_removed(session):
    session.add_source(original_set="aws_images", annotation_batch="V1")
    session.split(mod=2, keep_remainder=[0], seed="s")
    assert session.head == "filter_1"

    session.undo()
    assert session.head == "source_1"
    assert [s.id for s in session.steps] == ["source_1"]


def test_rollback_returns_you_to_where_you_were_standing(session):
    """checkpoint 記的不只是步數，還有位置——否則 rollback 會把你丟到別條分支。"""
    session.add_source(original_set="aws_images", annotation_batch="V1")
    session.add_source(original_set="DrLee", annotation_batch="V1")
    session.checkout("source_1")
    session.checkpoint("here")

    session.add_source(original_set="TB-portal", annotation_batch="V1")
    assert session.head == "source_3"

    session.rollback("here")
    assert session.head == "source_1", "應該回到標記時所在的位置"
    assert [s.id for s in session.steps] == ["source_1", "source_2"]


def test_preview_and_show_report_the_same_category_distribution(session, db):
    """探索中看到的數字，跟 commit 之後 `cxr show` 看到的必須是同一組。

    這兩份統計是兩套獨立實作（analyzer 走記憶體、crud 走 SQL），鍵名或定義
    一旦分歧，使用者就會在 commit 前後看到兜不起來的數字——`cxr show` 曾經
    因為同一類分歧（distinct vs distinct_subjects）必定 crash。
    """
    session.add_source(original_set="aws_images", annotation_batch="V1")
    session.map_category(
        "aws_images@V1",
        {"Pneumonia": "pneumonia", "Normal": "normal", "Effusion": "effusion"},
    )
    from_preview = {r["target"]: r for r in session.preview()["category_distribution"]}

    result = session.commit(
        version="V1", author=Author(name="pytest", email="pytest@example.com")
    )
    try:
        from_show = {
            r["target"]: r
            for r in crud.version_summary(db, result.manual_set_version_id)[
                "category_distribution"
            ]
        }
        assert set(from_preview) == set(from_show)
        for target, row in from_show.items():
            assert row == from_preview[target], target
    finally:
        db.execute(text("DELETE FROM manual_sets WHERE name = :n"), {"n": session.name})
        db.commit()
