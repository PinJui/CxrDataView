"""Op 的行為契約——這些是規則本身，改動時應該先改測試。"""

import pytest

from cxr_dataset_manager.core import ops
from cxr_dataset_manager.core.schema import (
    CategoryMapStep,
    ConflictResolveStep,
    ConflictRule,
    DedupStep,
    FilterStep,
    ImportListStep,
    SourceStep,
    UnionStep,
)
from cxr_dataset_manager.core.types import SpecError


def src(catalog, original_set, **kw):
    return ops.op_source(catalog, [], SourceStep(id="s", original_set=original_set, **kw)).candidates


# ---------------------------------------------------------------------------
# 不變條件
# ---------------------------------------------------------------------------


def test_source_from_annotation_batch_pulls_in_its_images(catalog):
    """annotation_batch 與 image_batch 是兩條獨立版本軸，
    只拿標註不拿影像的話 CandidateSet 當場就破了。"""
    cand = src(catalog, "TB-portal", annotation_batch="V1")
    assert cand.cls and cand.images
    for ann_id in cand.cls:
        assert catalog.cls(ann_id).image_id in cand.images


def test_sourcing_an_image_batch_brings_its_annotations(catalog):
    """拉一批影像通常就是要它的標籤——不帶標註的資料集沒有用。"""
    with_ann = src(catalog, "aws_images", image_batch="V1")
    assert with_ann.cls, "image batch 也該帶進既有的標註"
    for ann_id in with_ann.cls:
        assert catalog.cls(ann_id).image_id in with_ann.images

    without = src(catalog, "aws_images", image_batch="V1", with_annotations=False)
    assert without.images == with_ann.images
    assert not without.cls and not without.det


def test_sourcing_images_pulls_every_annotation_batch_that_covers_them(catalog):
    """一張圖被多個 batch 標過是衝突的來源——全部帶進來攤開，不偷偷挑一個。"""
    result = ops.op_source(
        catalog, [], SourceStep(id="s", original_set="aws_images", image_batch="V1")
    )
    assert len(result.stats["annotation_sources"]) > 1, (
        "aws_images 的影像同時被 V1 與 V2 標註批次覆蓋，兩者都該出現"
    )


def test_dropping_images_drops_their_annotations(catalog):
    cand = src(catalog, "aws_images", annotation_batch="V1")
    victim = next(iter(cand.images))
    doomed = {a for a in cand.cls if catalog.cls(a).image_id == victim}
    assert doomed, "測試前提：這張圖要有標註"
    ops._drop_images(cand, catalog, {victim}, "test")
    assert victim not in cand.images
    assert not (doomed & cand.cls)


# ---------------------------------------------------------------------------
# dedup
# ---------------------------------------------------------------------------


def test_dedup_keeps_one_image_per_hash(catalog):
    a = src(catalog, "aws_images", image_batch="V1")
    b = src(catalog, "DrLee", image_batch="V1")
    merged = ops.op_union(catalog, [a, b], UnionStep(id="u", inputs=["a", "b"])).candidates

    result = ops.op_dedup(
        catalog, [merged], DedupStep(id="d", input="u", source_priority=["DrLee", "aws_images"])
    )
    hashes = [catalog.image(i).blake3_hash for i in result.candidates.images]
    assert len(hashes) == len(set(hashes)), "去重後每個 blake3 只該剩一張"
    assert result.stats["duplicate_groups"] > 0, "測試資料本來就該有重複"


def test_dedup_respects_source_priority(catalog):
    """兩份都沒有標註時，source_priority 是唯一的裁決依據。"""
    a = src(catalog, "aws_images", image_batch="V1", with_annotations=False)
    b = src(catalog, "DrLee", image_batch="V1", with_annotations=False)
    merged = ops.op_union(catalog, [a, b], UnionStep(id="u", inputs=["a", "b"])).candidates

    for winner in ("DrLee", "aws_images"):
        loser = "aws_images" if winner == "DrLee" else "DrLee"
        kept = ops.op_dedup(
            catalog, [merged],
            DedupStep(id="d", input="u", source_priority=[winner, loser]),
        ).candidates
        # 找一組真的跨來源重複的 hash，確認留下的是優先權高的那邊
        by_hash: dict[str, set[str]] = {}
        for image_id in merged.images:
            meta = catalog.image(image_id)
            by_hash.setdefault(meta.blake3_hash, set()).add(meta.original_set_name)
        cross = {h for h, sources in by_hash.items() if len(sources) > 1}
        assert cross, "測試資料要有跨來源重複"
        for image_id in kept.images:
            meta = catalog.image(image_id)
            if meta.blake3_hash in cross:
                assert meta.original_set_name == winner


def test_dedup_prefers_the_copy_that_carries_annotations(catalog):
    """blake3 相同就是同一張照片——留下沒標註的那份等於白白丟掉標籤。

    所以 source_priority 只在兩份都有標註時才決定勝負；有標註的那一份
    永遠優先，即使它的來源排在後面。
    """
    annotated = src(catalog, "DrLee", annotation_batch="V1")
    bare = src(catalog, "aws_images", image_batch="V1", with_annotations=False)
    merged = ops.op_union(
        catalog, [annotated, bare], UnionStep(id="u", inputs=["a", "b"])
    ).candidates

    # 刻意把沒標註的那個來源排在最前面
    step = DedupStep(id="d", input="u", source_priority=["aws_images", "DrLee"])
    result = ops.op_dedup(catalog, [merged], step)
    assert result.stats["duplicate_groups"] > 0, "測試前提：要有跨來源重複"
    assert result.stats["annotations_dropped_with_duplicates"] == 0, (
        "優先留有標註的那份，就不該有標註被丟掉"
    )

    off = ops.op_dedup(
        catalog, [merged], step.model_copy(update={"prefer_annotated": False})
    )
    assert off.stats["annotations_dropped_with_duplicates"] > 0, (
        "關掉之後才會照 source_priority 硬選，也才會丟掉標註"
    )


def test_duplicates_are_listed_with_what_each_copy_carries(catalog):
    """決定留哪一張之前，要看得到每一張各自帶了什麼標註。"""
    a = src(catalog, "aws_images", image_batch="V1")
    b = src(catalog, "DrLee", image_batch="V1")
    merged = ops.op_union(catalog, [a, b], UnionStep(id="u", inputs=["a", "b"])).candidates

    groups = ops.find_duplicates(catalog, merged)
    assert groups, "測試資料本來就有內容重複"
    assert any(g["cross_source"] for g in groups)
    for group in groups:
        assert len(group["candidates"]) > 1
        for c in group["candidates"]:
            assert c["image_id"] in merged.images
            assert set(c["cls_annotation_ids"]) <= merged.cls


def test_dedup_keeps_the_image_you_name(catalog):
    """`duplicates` 看完之後，keep 指定哪一張活下來。"""
    a = src(catalog, "aws_images", image_batch="V1")
    b = src(catalog, "DrLee", image_batch="V1")
    merged = ops.op_union(catalog, [a, b], UnionStep(id="u", inputs=["a", "b"])).candidates

    group = next(g for g in ops.find_duplicates(catalog, merged) if g["cross_source"])
    # 刻意挑「照預設規則不會贏」的那一張
    default_winner = ops.op_dedup(
        catalog, [merged], DedupStep(id="d", input="u")
    ).candidates.images
    losers = [c["image_id"] for c in group["candidates"] if c["image_id"] not in default_winner]
    assert losers, "測試前提：這一組要有落敗者"

    kept = ops.op_dedup(
        catalog, [merged], DedupStep(id="d", input="u", keep=[losers[0]])
    ).candidates.images
    assert losers[0] in kept
    for c in group["candidates"]:
        if c["image_id"] != losers[0]:
            assert c["image_id"] not in kept


def test_dedup_refuses_two_choices_in_one_group(catalog):
    a = src(catalog, "aws_images", image_batch="V1")
    b = src(catalog, "DrLee", image_batch="V1")
    merged = ops.op_union(catalog, [a, b], UnionStep(id="u", inputs=["a", "b"])).candidates
    group = next(g for g in ops.find_duplicates(catalog, merged) if g["cross_source"])
    both = [c["image_id"] for c in group["candidates"]][:2]

    with pytest.raises(SpecError, match="每組只能留一張"):
        ops.op_dedup(catalog, [merged], DedupStep(id="d", input="u", keep=both))


def test_filter_annotated_drops_unlabelled_images(catalog):
    """manual-set 是 training-ready 的，沒標註的影像必須被顯式剔除。"""
    cand = src(catalog, "aws_images", image_batch="V1")
    annotated = {catalog.cls(x).image_id for x in cand.cls} | {
        catalog.det(x).image_id for x in cand.det
    }
    assert cand.images - annotated, "測試前提：這批影像要有沒被標註的"

    result = ops.op_filter(
        catalog, [cand], FilterStep(id="f", input="x", criterion="annotated")
    )
    assert result.candidates.images == cand.images & annotated
    assert result.stats["images_dropped"] == len(cand.images - annotated)


def test_dedup_is_order_independent(catalog):
    a = src(catalog, "aws_images", image_batch="V1")
    b = src(catalog, "DrLee", image_batch="V1")
    forward = ops.op_union(catalog, [a, b], UnionStep(id="u", inputs=["a", "b"])).candidates
    backward = ops.op_union(catalog, [b, a], UnionStep(id="u", inputs=["b", "a"])).candidates
    step = DedupStep(id="d", input="u", source_priority=["DrLee", "aws_images"])
    assert (
        ops.op_dedup(catalog, [forward], step).candidates.images
        == ops.op_dedup(catalog, [backward], step).candidates.images
    )


# ---------------------------------------------------------------------------
# filter: sample
# ---------------------------------------------------------------------------


def _sample(catalog, cand, seed="seed-a", mod=2, keep=(0,), key_field="subject_id"):
    return ops.op_filter(
        catalog, [cand],
        FilterStep(id="f", input="x", criterion="sample", mod=mod,
                   keep_remainder=list(keep), seed=seed, key_field=key_field),
    )


def test_sample_split_is_deterministic(catalog):
    cand = src(catalog, "aws_images", image_batch="V1")
    first = _sample(catalog, cand).candidates.images
    second = _sample(catalog, cand).candidates.images
    assert first == second, "同一份 spec 必須永遠切出同一批圖"


def test_changing_the_seed_changes_the_split(catalog):
    cand = src(catalog, "aws_images", image_batch="V1")
    assert _sample(catalog, cand, seed="a").candidates.images != _sample(
        catalog, cand, seed="b"
    ).candidates.images


def test_split_never_separates_a_subject(catalog):
    """這條是整套 leakage 防禦的地基：同一位病患的所有影像必須同進同出。"""
    cand = src(catalog, "aws_images", image_batch="V1")
    kept = _sample(catalog, cand, mod=4, keep=(0, 1)).candidates.images
    dropped = cand.images - kept

    def subjects(ids):
        return {
            catalog.image(i).subject_id for i in ids if catalog.image(i).subject_id
        }

    assert not (subjects(kept) & subjects(dropped))


def test_complementary_splits_partition_the_input(catalog):
    cand = src(catalog, "aws_images", image_batch="V1")
    train = _sample(catalog, cand, mod=4, keep=(0, 1, 2)).candidates.images
    val = _sample(catalog, cand, mod=4, keep=(3,)).candidates.images
    assert not (train & val)
    assert train | val == cand.images


def test_sample_reports_subject_fallback(catalog):
    """沒有 subject 的影像用 image_id 切——必須明講，不能假裝有保證。"""
    cand = src(catalog, "indo_vnn", image_batch="V1")
    result = _sample(catalog, cand)
    assert result.stats["subject_unknown_images"] > 0
    assert any("沒有 subject 資訊" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# filter: predicate / explicit_list
# ---------------------------------------------------------------------------


def test_predicate_filter_selects_by_metadata(catalog):
    cand = src(catalog, "DrLee", image_batch="V1")
    result = ops.op_filter(
        catalog, [cand],
        FilterStep(id="f", input="x", criterion="predicate",
                   expression="date_captured >= '2024-01-01'"),
    )
    for image_id in result.candidates.images:
        assert catalog.image(image_id).date_captured.isoformat() >= "2024-01-01"
    assert len(result.candidates.images) < len(cand.images)


def test_explicit_list_missing_names_can_fail_loudly(catalog):
    cand = src(catalog, "DrLee", image_batch="V1")
    present = catalog.image(next(iter(cand.images))).file_name
    step = FilterStep(
        id="f", input="x", criterion="explicit_list",
        file_names=[present, "no_such_file.png"], on_missing="error",
    )
    with pytest.raises(SpecError, match="不在 input"):
        ops.op_filter(catalog, [cand], step)

    lenient = step.model_copy(update={"on_missing": "warn"})
    result = ops.op_filter(catalog, [cand], lenient)
    assert result.stats["matched_count"] == 1
    assert result.stats["missing"] == ["no_such_file.png"]


# ---------------------------------------------------------------------------
# import_list
# ---------------------------------------------------------------------------


def test_import_list_matches_and_reports_missing(catalog):
    batch = catalog.resolve_image_batch("DrLee", "V1")
    names = [catalog.image(i).file_name for i in catalog.load_image_batch(batch)[:5]]
    step = ImportListStep(
        id="i", original_set="DrLee", image_batch="V1",
        file_names=names + ["ghost.png"], on_missing="warn",
    )
    result = ops.op_import_list(catalog, [], step, step.file_names)
    assert result.stats["matched_count"] == 5
    assert result.stats["missing"] == ["ghost.png"]
    assert len(result.candidates.images) == 5


def test_import_list_default_is_zero_tolerance(catalog):
    step = ImportListStep(
        id="i", original_set="DrLee", image_batch="V1", file_names=["ghost.png"]
    )
    assert step.on_missing == "error"
    with pytest.raises(SpecError, match="對不上"):
        ops.op_import_list(catalog, [], step, step.file_names)


# ---------------------------------------------------------------------------
# category_map
# ---------------------------------------------------------------------------


def test_unmapped_categories_fail_the_build_by_default(catalog):
    cand = src(catalog, "aws_images", annotation_batch="V1")
    with pytest.raises(SpecError, match="沒有映射"):
        ops.op_category_map(
            catalog, [cand],
            CategoryMapStep(id="m", input="x",
                            mapping={"aws_images@V1": {"Pneumonia": "pneumonia"}},
                            require_total=True),
        )


def test_merge_identical_only_merges_exact_names(catalog):
    aws = src(catalog, "aws_images", annotation_batch="V1")
    drlee = src(catalog, "DrLee", annotation_batch="V1")
    merged = ops.op_union(catalog, [aws, drlee], UnionStep(id="u", inputs=["a", "b"])).candidates
    result = ops.op_category_map(
        catalog, [merged],
        CategoryMapStep(id="m", input="u", merge_identical=True, require_total=False),
    )
    targets = set(result.stats["target_categories"])
    # 'Pneumonia' 與 'pneumonia' 不同名，不該被自動併在一起
    assert {"Pneumonia", "pneumonia"} <= targets


def test_unmapped_annotations_are_dropped_not_silently_kept(catalog):
    cand = src(catalog, "aws_images", annotation_batch="V1")
    result = ops.op_category_map(
        catalog, [cand],
        CategoryMapStep(id="m", input="x",
                        mapping={"aws_images@V1": {"Pneumonia": "pneumonia"}},
                        require_total=False),
    )
    for ann_id in result.candidates.cls:
        assert catalog.cls(ann_id).category_id in result.candidates.category_targets


# ---------------------------------------------------------------------------
# conflict_resolve
# ---------------------------------------------------------------------------


def _conflicted(catalog):
    """aws V1（實習）與 V2（主治）標同一批圖，映射到同一組 target。"""
    junior = src(catalog, "aws_images", annotation_batch="V1")
    senior = src(catalog, "aws_images", annotation_batch="V2")
    merged = ops.op_union(catalog, [junior, senior], UnionStep(id="u", inputs=["a", "b"])).candidates
    return ops.op_category_map(
        catalog, [merged], CategoryMapStep(id="m", input="u", merge_identical=True)
    ).candidates


def test_disagreeing_sources_are_detected_as_contradictions(catalog):
    groups = ops.find_conflicts(catalog, _conflicted(catalog))
    assert any(g.kind == "contradiction" for g in groups), (
        "一邊說 normal、一邊說 pneumonia 必須被抓成矛盾——"
        "只用 (image, target) 分組會完全看不到這種衝突"
    )


def test_annotator_precedence_keeps_the_senior_read(catalog):
    cand = _conflicted(catalog)
    before = ops.find_conflicts(catalog, cand)
    result = ops.op_conflict_resolve(
        catalog, [cand],
        ConflictResolveStep(
            id="c", input="m", strict=True,
            rules=[ConflictRule(rule="annotator_precedence",
                                annotator_precedence=["radiologist_senior", "radiologist_junior"])],
        ),
    )
    assert result.stats["resolved"] == len(before)
    assert not ops.find_conflicts(catalog, result.candidates)
    for group in before:
        survivors = [a for a in group.annotation_ids if a in result.candidates.cls]
        assert survivors, "每組衝突都該留下一筆"
        assert all(catalog.cls(a).annotator_name == "radiologist_senior" for a in survivors)


def test_uncovered_conflicts_fail_loudly_instead_of_guessing(catalog):
    """規則涵蓋不到就報錯，不靜默 fallback——這是刻意的設計。"""
    cand = _conflicted(catalog)
    with pytest.raises(SpecError, match="沒有任何規則能裁決"):
        ops.op_conflict_resolve(
            catalog, [cand],
            ConflictResolveStep(
                id="c", input="m", strict=True,
                rules=[ConflictRule(rule="annotator_precedence",
                                    annotator_precedence=["nobody_in_this_dataset"])],
            ),
        )


def test_non_strict_keeps_the_contradiction_but_says_so(catalog):
    cand = _conflicted(catalog)
    result = ops.op_conflict_resolve(
        catalog, [cand],
        ConflictResolveStep(id="c", input="m", strict=False, rules=[]),
    )
    assert result.stats["unresolved_count"] > 0
    assert any("未解決" in w for w in result.warnings)


def test_resolution_leaves_at_most_one_annotation_per_image_and_target(catalog):
    cand = _conflicted(catalog)
    result = ops.op_conflict_resolve(
        catalog, [cand],
        ConflictResolveStep(
            id="c", input="m", strict=True,
            rules=[
                ConflictRule(rule="annotator_precedence",
                             annotator_precedence=["radiologist_senior", "radiologist_junior"]),
                ConflictRule(rule="highest_score"),
            ],
        ),
    ).candidates
    seen = set()
    for ann_id in result.cls:
        ann = catalog.cls(ann_id)
        key = (ann.image_id, result.category_targets[ann.category_id])
        assert key not in seen, f"{key} 出現了兩次"
        seen.add(key)


# ---------------------------------------------------------------------------
# manual_override —— 指名一個 id，納入或排除
# ---------------------------------------------------------------------------


def _override(catalog, cand, **kw):
    from cxr_dataset_manager.core.schema import ManualOverrideStep, Override

    return ops.op_manual_override(
        catalog, [cand],
        ManualOverrideStep(id="ov", input="x", overrides=[Override(**kw)]),
    )


def test_override_can_name_an_annotation_from_a_batch_never_sourced(catalog, db):
    """使用者指名一個 id，系統就該去找得到它。

    回歸測試：Catalog 原本只認得已 source 批次裡的標註，所以指名別的批次的
    標註會失敗——但那是實作缺了 ensure_annotations，不是設計上的限制。
    """
    from sqlalchemy import text

    cand = src(catalog, "aws_images", annotation_batch="V1")
    ann_id, image_id = db.execute(
        text(
            """
            SELECT c.id, c.image_id FROM cls_annotations c
            JOIN annotation_batches ab ON ab.id = c.annotation_batch_id
            JOIN original_sets os ON os.id = ab.original_set_id
            WHERE os.name = 'aws_images' AND ab.version = 'V2'
              AND c.image_id = ANY(:imgs) LIMIT 1
            """
        ),
        {"imgs": sorted(cand.images)},
    ).one()
    assert ann_id not in cand.cls, "測試前提：這筆標註不在已 source 的批次裡"

    result = _override(catalog, cand, action="include_annotation",
                       annotation_id=ann_id, reason="主治判讀")
    assert ann_id in result.candidates.cls


def test_including_an_image_brings_its_annotations(catalog):
    """跟 source --image 一致——否則加進來的影像沒有標籤，commit 會被擋。"""
    full = src(catalog, "aws_images", annotation_batch="V1")
    victim = next(iter(full.images))

    reduced = full.copy()
    ops._drop_images(reduced, catalog, {victim}, "test")
    reduced.prune(catalog)
    assert victim not in reduced.images

    result = _override(catalog, reduced, action="include_image", image_id=victim,
                       reason="要回來")
    assert victim in result.candidates.images
    assert any(catalog.cls(a).image_id == victim for a in result.candidates.cls)


def test_overrides_never_silently_do_nothing(catalog, db):
    """排除一個不在集合裡的東西如果靜默成功，你會以為做了事其實沒有。"""
    from sqlalchemy import text

    cand = src(catalog, "aws_images", annotation_batch="V1")
    outsider = db.execute(
        text("SELECT id FROM images WHERE id <> ALL(:ids) LIMIT 1"),
        {"ids": sorted(cand.images)},
    ).scalar_one()

    with pytest.raises(SpecError, match="本來就不在"):
        _override(catalog, cand, action="exclude_image", image_id=outsider)
    with pytest.raises(SpecError, match="不在候選集合裡"):
        _override(catalog, cand, action="exclude_annotation", annotation_id=99999)
    with pytest.raises(SpecError, match="沒有這個 image_id"):
        _override(catalog, cand, action="exclude_image", image_id=99999999)


def test_excluding_the_last_annotation_is_caught_immediately(catalog):
    """剔除標註可能讓影像變成沒標籤——那在 commit 時會被約束擋，
    但當場說清楚是哪幾張比較有用。"""
    cand = src(catalog, "aws_images", annotation_batch="V1")
    only_child = next(
        a for a in cand.cls
        if sum(1 for b in cand.cls if catalog.cls(b).image_id == catalog.cls(a).image_id) == 1
    )
    with pytest.raises(SpecError, match="沒有任何標籤"):
        _override(catalog, cand, action="exclude_annotation", annotation_id=only_child)
