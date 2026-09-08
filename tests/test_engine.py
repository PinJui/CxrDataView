"""端到端：可重現性、落庫的完整性、溯源查得到。"""

import uuid



import pytest
from sqlalchemy import text

from cxr_dataset_manager.core.engine import Author, build, execute_spec
from cxr_dataset_manager.core.schema import BuildSpec
from cxr_dataset_manager.core.types import Catalog, SpecError
from cxr_dataset_manager.db import crud

TEST_AUTHOR = Author(name="pytest", email="pytest@example.com")

SPEC = """
steps:
  - {id: aws, op: source, original_set: aws_images, annotation_batch: V1}
  - {id: tb,  op: source, original_set: TB-portal, annotation_batch: V1}
  - {id: pooled, op: union, inputs: [aws, tb]}
  - {id: deduped, op: dedup, input: pooled, source_priority: [TB-portal, aws_images]}
  - {id: half, op: filter, input: deduped, criterion: sample,
     key_field: subject_id, mod: 2, keep_remainder: [0], seed: engine-test}
  - id: mapped
    op: category_map
    input: half
    mapping:
      aws_images@V1: {Pneumonia: pneumonia, Normal: normal, Effusion: effusion}
      TB-portal@V1:  {TB: tb, Normal: normal, Pneumonia: pneumonia}
  - id: resolved
    op: conflict_resolve
    input: mapped
    rules:
      - {rule: annotator_precedence,
         annotator_precedence: [radiologist_senior, radiologist_junior]}
      - {rule: highest_score}
final: resolved
"""


@pytest.fixture
def spec():
    return BuildSpec.from_yaml(SPEC)


TRACKED_TABLES = [
    "manual_sets", "manual_set_versions", "manual_set_images",
    "manual_set_cls_annotations", "manual_set_det_annotations",
    "manual_set_target_categories", "manual_set_category_mappings",
    "manual_set_build_specs",
]


def _row_counts(db) -> dict[str, int]:
    db.rollback()
    return {
        t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in TRACKED_TABLES
    }


def _cleanup(db, name):
    db.execute(
        text(
            """
            DELETE FROM manual_sets WHERE name = :n
            """
        ),
        {"n": name},
    )
    db.commit()


def test_same_spec_always_produces_the_same_set(db, spec):
    """整套設計的核心承諾：spec 是可重現的，包含切割抽樣在內。"""
    first = execute_spec(db, spec, Catalog(db)).final
    second = execute_spec(db, spec, Catalog(db)).final
    assert first.images == second.images
    assert first.cls == second.cls
    assert first.category_targets == second.category_targets


def test_final_set_satisfies_the_schema_invariant(db, spec):
    """每筆標註的影像都必須在集合裡，否則 commit 時複合外鍵會擋下來。"""
    execution = execute_spec(db, spec, Catalog(db))
    final, catalog = execution.final, execution.catalog
    for ann_id in final.cls:
        assert catalog.cls(ann_id).image_id in final.images
    for ann_id in final.det:
        assert catalog.det(ann_id).image_id in final.images


def test_build_writes_a_complete_version(db, spec):
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    try:
        result = build(db, spec, name, "V1", author=TEST_AUTHOR)
        version_id = result.manual_set_version_id
        summary = crud.version_summary(db, version_id)

        assert summary["images"] == result.counts["images"]
        assert summary["cls"] == result.counts["cls"]
        assert summary["det"] == result.counts["det"]
        assert summary["spec_sha256"] == spec.sha256()
        # 每個 target category 都要有 local category 對應過來，不留死映射
        assert summary["category_mappings"]
        for target, locals_ in summary["category_mappings"].items():
            assert locals_, f"target {target} 沒有任何 local category"
    finally:
        _cleanup(db, name)


def test_rebuilding_the_stored_spec_reproduces_the_result(db, spec):
    """從資料庫把 spec 撈回來重跑，結果必須一模一樣。"""
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    try:
        result = build(db, spec, name, "V1", author=TEST_AUTHOR)
        stored = crud.version_summary(db, result.manual_set_version_id)["spec"]
        replayed = execute_spec(db, BuildSpec.model_validate(stored), Catalog(db)).final
        assert replayed.images == result.execution.final.images
        assert replayed.cls == result.execution.final.cls
    finally:
        _cleanup(db, name)


def test_provenance_explains_every_dropped_image(db, spec):
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    try:
        result = build(db, spec, name, "V1", author=TEST_AUTHOR)
        version_id = result.manual_set_version_id
        catalog = result.execution.catalog

        # 找一張進來過又被踢掉的影像，確認 why 說得出是哪一步、什麼原因。
        # 「進來過」直接看第一步的 CandidateSet——source 不再逐筆記錄 added，
        # 因為成員關係本身就是那個資訊。
        first_step = result.execution.reports[0]
        entered = result.execution.results[first_step.step_id].images
        dropped = entered - result.execution.final.images
        assert dropped, "測試前提：這份 spec 應該要踢掉一些影像"

        image_id = sorted(dropped)[0]
        trail = crud.explain(db, version_id, image_id=image_id)
        assert trail["found"] and not trail["in_final_set"]
        assert trail["build_spec_id"] == result.build_spec_id
        decisions = [t["decision"] for t in trail["trail"] if t["entity_kind"] == "image"]
        assert "added" in decisions and "dropped" in decisions
        assert all(t["reason"] for t in trail["trail"]), "每筆裁決都要說得出原因"
    finally:
        _cleanup(db, name)


def test_version_numbers_are_immutable(db, spec):
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    try:
        build(db, spec, name, "V1", author=TEST_AUTHOR)
        with pytest.raises(SpecError, match="已經有版本"):
            build(db, spec, name, "V1", author=TEST_AUTHOR)
    finally:
        _cleanup(db, name)


def test_dry_run_writes_absolutely_nothing(db, spec):
    """試跑就只是試跑：算得出結果，但資料庫一個 row 都不該多。"""
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    before = _row_counts(db)

    result = build(db, spec, name, "V1", dry_run=True)

    assert result.counts["images"] > 0
    assert result.manual_set_version_id is None
    assert result.build_spec_id is None
    assert crud.resolve_version(db, name, "V1") is None
    assert _row_counts(db) == before


def test_a_failed_build_rolls_everything_back(db):
    """失敗的 build 要讓資料庫回到什麼都沒發生的狀態。"""
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    broken = BuildSpec.from_yaml(
        """
        steps:
          - {id: aws, op: source, original_set: aws_images, annotation_batch: V1}
          - {id: mapped, op: category_map, input: aws,
             mapping: {"aws_images@V1": {Pneumonia: pneumonia}}, require_total: true}
        final: mapped
        """
    )
    before = _row_counts(db)
    with pytest.raises(SpecError):
        build(db, broken, name, "V1", author=TEST_AUTHOR)

    assert crud.resolve_version(db, name, "V1") is None
    assert _row_counts(db) == before, "失敗的 build 不該留下任何殘跡"


def test_incremental_category_mapping_does_not_destroy_other_sources(db):
    """一次映射一個 annotation_batch 時，還沒輪到的 batch 的標註必須留著。

    回歸測試：早期版本的 category_map 一律剔除未映射的標註，
    導致「先映射 A、再映射 B」的自然操作順序會在第一步就把 B 的標註全丟光。
    """
    from cxr_dataset_manager.session.builder import ManualSetSession

    session = ManualSetSession(db, name="pytest_incremental")
    session.add_source(original_set="aws_images", annotation_batch="V1")
    session.add_source(original_set="TB-portal", annotation_batch="V1")
    session.union()
    before = session.current.counts()

    session.map_category("aws_images@V1",
                         {"Pneumonia": "pneumonia", "Normal": "normal", "Effusion": "effusion"})
    assert session.current.counts()["det"] == before["det"], "TB 的 det 標註不該被丟掉"

    session.map_category("TB-portal@V1", {"TB": "tb", "Normal": "normal", "Pneumonia": "pneumonia"})
    assert session.current.counts() == before
    assert not session.preview_categories()["unmapped"]


def test_build_refuses_annotations_without_a_target_category(db):
    """沒有 target category 的標註 = 訓練時讀不到 class name，不能讓它落庫。"""
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    no_mapping = BuildSpec.from_yaml(
        """
        steps:
          - {id: aws, op: source, original_set: aws_images, annotation_batch: V1}
        final: aws
        """
    )
    with pytest.raises(SpecError, match="沒有對應的 target category"):
        build(db, no_mapping, name, "V1", author=TEST_AUTHOR)
    assert crud.resolve_version(db, name, "V1") is None


def test_provenance_never_attributes_another_images_annotation(db, spec):
    """cls 與 det 的 id 是兩條獨立序列，一定會撞號。

    回歸測試：早期版本把兩種 id 混在同一個陣列裡比對，導致別張圖的
    det #4 會出現在 cls #4 所屬影像的溯源紀錄裡——溯源講錯話比沒有溯源更糟。
    """
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    try:
        result = build(db, spec, name, "V1", author=TEST_AUTHOR)
        catalog = result.execution.catalog

        # 找一張「它的 cls annotation id 剛好也是某個 det annotation id」的影像
        collisions = db.execute(
            text(
                """
                SELECT c.image_id, c.id FROM cls_annotations c
                JOIN det_annotations d ON d.id = c.id AND d.image_id <> c.image_id
                LIMIT 1
                """
            )
        ).all()
        if not collisions:
            pytest.skip("這份 mock 資料沒有 id 撞號的情況")
        image_id, shared_id = collisions[0]

        trail = crud.explain(db, result.manual_set_version_id, image_id=image_id)
        for entry in trail["trail"]:
            if entry["entity_kind"] == "det_annotation":
                assert catalog.det(entry["entity_id"]).image_id == image_id, (
                    f"det #{entry['entity_id']} 不屬於這張影像，卻出現在它的溯源裡"
                )
            if entry["entity_kind"] == "cls_annotation":
                assert catalog.cls(entry["entity_id"]).image_id == image_id
    finally:
        _cleanup(db, name)


def test_build_refuses_a_manual_set_containing_unannotated_images(db):
    """manual-set 是 training-ready 的資料集，schema 也有 trigger 擋。
    應用層要先擋一次，因為 trigger 只講得出第一張出問題的圖。"""
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    spec = BuildSpec.from_yaml(
        """
        steps:
          - {id: imgs, op: source, original_set: aws_images, image_batch: V1}
          - {id: mapped, op: category_map, input: imgs, merge_identical: true}
        final: mapped
        """
    )
    with pytest.raises(SpecError, match="沒有任何標註"):
        build(db, spec, name, "V1", author=TEST_AUTHOR)
    assert crud.resolve_version(db, name, "V1") is None


def test_filtering_to_annotated_images_makes_that_build_valid(db):
    """加一步 filter criterion=annotated 就過得了——錯誤訊息說的就是這個。"""
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    spec = BuildSpec.from_yaml(
        """
        steps:
          - {id: imgs, op: source, original_set: aws_images, image_batch: V1}
          - {id: labelled, op: filter, input: imgs, criterion: annotated}
          - {id: mapped, op: category_map, input: labelled, merge_identical: true}
          - id: resolved
            op: conflict_resolve
            input: mapped
            rules:
              - {rule: annotator_precedence,
                 annotator_precedence: [radiologist_senior, radiologist_junior]}
              - {rule: highest_score}
        final: resolved
        """
    )
    try:
        result = build(db, spec, name, "V1", author=TEST_AUTHOR)
        assert result.counts["images"] > 0
        # 資料庫的 trigger 也同意：沒有任何一張沒標註的影像進得去
        bare = db.execute(
            text(
                """
                SELECT count(*) FROM manual_set_images msi
                WHERE msi.manual_set_version_id = :v
                  AND NOT EXISTS (SELECT 1 FROM manual_set_cls_annotations a
                                  WHERE a.manual_set_version_id = :v AND a.image_id = msi.image_id)
                  AND NOT EXISTS (SELECT 1 FROM manual_set_det_annotations d
                                  WHERE d.manual_set_version_id = :v AND d.image_id = msi.image_id)
                """
            ),
            {"v": result.manual_set_version_id},
        ).scalar_one()
        assert bare == 0
    finally:
        _cleanup(db, name)


def test_the_database_trigger_is_the_real_guarantee(db):
    """繞過應用層直接寫，資料庫仍然必須擋下來。"""
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    try:
        with pytest.raises(Exception, match="Unannotated image in manual-set"):
            db.execute(text("INSERT INTO manual_sets (name) VALUES (:n)"), {"n": name})
            ms_id = db.execute(
                text("SELECT id FROM manual_sets WHERE name = :n"), {"n": name}
            ).scalar_one()
            db.execute(
                text(
                    "INSERT INTO manual_set_versions (manual_set_id, version) VALUES (:m, 'V1')"
                ),
                {"m": ms_id},
            )
            v_id = db.execute(
                text("SELECT id FROM manual_set_versions WHERE manual_set_id = :m"),
                {"m": ms_id},
            ).scalar_one()
            image_id = db.execute(text("SELECT id FROM images LIMIT 1")).scalar_one()
            db.execute(
                text(
                    "INSERT INTO manual_set_images (manual_set_version_id, image_id)"
                    " VALUES (:v, :i)"
                ),
                {"v": v_id, "i": image_id},
            )
            db.commit()
    finally:
        db.rollback()
        db.execute(text("DELETE FROM manual_sets WHERE name = :n"), {"n": name})
        db.commit()


def test_the_builder_is_recorded_on_the_version(db, spec):
    """內部工具沒有帳號系統，但「誰建了這份資料集」還是得答得出來。"""
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    try:
        result = build(
            db, spec, name, "V1",
            author=Author(name="Jeff Huang", email="jeff@example.com"),
        )
        summary = crud.version_summary(db, result.manual_set_version_id)
        assert summary["created_by"] == "Jeff Huang <jeff@example.com>"
    finally:
        _cleanup(db, name)


def test_committing_without_an_author_is_refused(db, spec):
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    with pytest.raises(SpecError, match="是誰建的"):
        build(db, spec, name, "V1")
    assert crud.resolve_version(db, name, "V1") is None


def test_a_dry_run_needs_no_author(db, spec):
    """試跑什麼都不寫，自然也不需要署名。"""
    result = build(db, spec, f"pytest_{uuid.uuid4().hex[:8]}", "V1", dry_run=True)
    assert result.counts["images"] > 0


@pytest.mark.parametrize("bad", [("", "a@b.c"), ("  ", "a@b.c"), ("me", "not-an-email")])
def test_author_details_are_validated(bad):
    with pytest.raises(SpecError):
        Author(name=bad[0], email=bad[1])


def test_losing_a_version_race_says_who_won(db, spec, monkeypatch):
    """樂觀鎖：不預先上鎖，撞到唯一約束才處理。

    兩個人同時 commit 同一個版本號時，資料庫擋下其中一個——訊息要說得出
    是誰搶先建的，以及接下來該怎麼辦。
    """
    import cxr_dataset_manager.core.engine as engine_mod

    name = f"pytest_{uuid.uuid4().hex[:8]}"
    try:
        build(db, spec, name, "V1", author=Author(name="First", email="first@example.com"))

        # 停用預先檢查，模擬「兩個人都通過檢查後才寫入」的競態
        monkeypatch.setattr(engine_mod, "_assert_version_available", lambda *a, **k: None)
        with pytest.raises(SpecError) as caught:
            build(db, spec, name, "V1", author=Author(name="Second", email="second@example.com"))

        message = str(caught.value)
        assert "First <first@example.com>" in message
        assert "換一個版本號" in message

        # 輸的那一方不該留下任何殘跡
        assert db.execute(
            text(
                "SELECT count(*) FROM manual_set_build_specs bs"
                " JOIN manual_set_versions mv ON mv.id = bs.manual_set_version_id"
                " JOIN manual_sets ms ON ms.id = mv.manual_set_id"
                " WHERE ms.name = :n"
            ),
            {"n": name},
        ).scalar_one() == 1
    finally:
        _cleanup(db, name)


def test_deleting_a_version_leaves_the_source_data_untouched(db, spec):
    """manual-set 只是「選了哪些」的記錄，刪掉它不該動到原始影像或標註。"""
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    before = {
        t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
        for t in ("images", "cls_annotations", "det_annotations", "image_subjects")
    }
    result = build(db, spec, name, "V1", author=TEST_AUTHOR)
    version_id = result.manual_set_version_id

    plan = crud.delete_manual_set(db, name, "V1")
    assert plan["versions"][0]["images"] > 0
    assert crud.resolve_version(db, name, "V1") is None

    after = {
        t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
        for t in ("images", "cls_annotations", "det_annotations", "image_subjects")
    }
    assert after == before, "原始資料被動到了"

    # 成員關係、spec、溯源都該跟著消失
    for table, column in [
        ("manual_set_images", "manual_set_version_id"),
        ("manual_set_cls_annotations", "manual_set_version_id"),
        ("manual_set_target_categories", "manual_set_version_id"),
        ("manual_set_build_specs", "manual_set_version_id"),
    ]:
        assert db.execute(
            text(f"SELECT count(*) FROM {table} WHERE {column} = :v"), {"v": version_id}
        ).scalar_one() == 0, table


def test_deleting_the_last_version_removes_the_manual_set_itself(db, spec):
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    build(db, spec, name, "V1", author=TEST_AUTHOR)

    plan = crud.delete_manual_set(db, name, "V1")
    assert plan["removes_manual_set"]
    assert db.execute(
        text("SELECT count(*) FROM manual_sets WHERE name = :n"), {"n": name}
    ).scalar_one() == 0


def test_deleting_one_of_several_versions_keeps_the_others(db, spec):
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    try:
        build(db, spec, name, "V1", author=TEST_AUTHOR)
        build(db, spec, name, "V2", author=TEST_AUTHOR)

        plan = crud.delete_manual_set(db, name, "V1")
        assert not plan["removes_manual_set"]
        assert crud.resolve_version(db, name, "V1") is None
        assert crud.resolve_version(db, name, "V2") is not None
    finally:
        _cleanup(db, name)


def test_deleting_a_whole_manual_set_takes_every_version(db, spec):
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    build(db, spec, name, "V1", author=TEST_AUTHOR)
    build(db, spec, name, "V2", author=TEST_AUTHOR)

    plan = crud.delete_manual_set(db, name)
    assert len(plan["versions"]) == 2 and plan["removes_manual_set"]
    assert crud.resolve_version(db, name, "V2") is None


def test_describe_deletion_warns_about_the_spec(db, spec):
    """spec 是唯一能重現這份資料集的東西，刪除前一定要講。"""
    name = f"pytest_{uuid.uuid4().hex[:8]}"
    try:
        build(db, spec, name, "V1", author=TEST_AUTHOR)
        plan = crud.describe_deletion(db, name, "V1")
        assert plan["versions"][0]["spec_sha256"] == spec.sha256()
        assert plan["versions"][0]["created_by"] == "pytest <pytest@example.com>"
        assert plan["versions"][0]["steps"] > 0
    finally:
        _cleanup(db, name)


def test_deleting_something_that_does_not_exist_fails_cleanly(db):
    with pytest.raises(SpecError, match="找不到"):
        crud.delete_manual_set(db, "definitely_not_here", "V1")
