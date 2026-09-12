"""每個 op 一支純函式（design_doc.md §3、§1 principle 1）。

簽名一律是 `(catalog, inputs, step) -> StepResult`，沒有 DB 寫入、沒有
框架依賴、沒有全域狀態——所以規則要改就是改這裡，不用動 schema
或 trigger，也方便單元測試。

慣例：
  * 每支 op 都回傳「新的」CandidateSet，不就地改輸入。
  * 影像被剔除時，掛在它身上的標註一定跟著被剔除（呼叫 `prune`），
    維持 CandidateSet 的不變條件，這樣 commit 時 DB 的複合外鍵一定收得下。
  * `StepResult.decisions` 只解釋「為什麼」，不記錄「有哪些」——某個 entity
    在哪一步進來、哪一步出去，engine 從各步的 CandidateSet 比對就知道了。
    所以 source / import_list 這種「整批帶進來」的步驟不產生逐筆裁決。
    這些裁決不落庫，只在 `cxr why` 重跑 spec 時當作說明用。
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from cxr_dataset_manager.core import predicate as pred
from cxr_dataset_manager.core.schema import (
    CategoryMapStep,
    ConflictResolveStep,
    DedupStep,
    ExceptStep,
    FilterStep,
    ImportListStep,
    IntersectStep,
    ManualOverrideStep,
    SourceStep,
    UnionStep,
)
from cxr_dataset_manager.core.types import (
    CandidateSet,
    Catalog,
    Decision,
    SpecError,
    StepResult,
)


def _drop_images(
    cand: CandidateSet,
    catalog: Catalog,
    image_ids: Iterable[int],
    reason: str,
    detail_of: Callable[[int], dict[str, Any]] | None = None,
) -> list[Decision]:
    """移除影像並連帶移除其標註，回傳對應的裁決記錄。"""
    decisions: list[Decision] = []
    dropped = set(image_ids) & cand.images
    if not dropped:
        return decisions
    for image_id in dropped:
        decisions.append(
            Decision(
                "image",
                image_id,
                "dropped",
                reason,
                detail_of(image_id) if detail_of else None,
            )
        )
    cand.images -= dropped
    for ann_id in [a for a in cand.cls if catalog.cls(a).image_id in dropped]:
        cand.cls.discard(ann_id)
        decisions.append(
            Decision(
                "cls_annotation", ann_id, "dropped", f"{reason}:image_dropped", None
            )
        )
    for ann_id in [a for a in cand.det if catalog.det(a).image_id in dropped]:
        cand.det.discard(ann_id)
        decisions.append(
            Decision(
                "det_annotation", ann_id, "dropped", f"{reason}:image_dropped", None
            )
        )
    return decisions


def _merge_targets(inputs: list[CandidateSet], step_id: str) -> dict[int, str]:
    """合併多個輸入的 category 映射；同一個 local category 被映射到不同
    target 是真正的衝突，靜默挑一個會讓結果無法解釋，所以直接報錯。"""
    merged: dict[int, str] = {}
    for cand in inputs:
        for category_id, target in cand.category_targets.items():
            if merged.setdefault(category_id, target) != target:
                raise SpecError(
                    f"step '{step_id}': local category {category_id} is mapped to "
                    f"'{merged[category_id]}' and '{target}' on different branches; make them agree before merging"
                )
    return merged


# ---------------------------------------------------------------------------
# 葉節點
# ---------------------------------------------------------------------------


def op_source(
    catalog: Catalog, inputs: list[CandidateSet], step: SourceStep
) -> StepResult:
    cand = CandidateSet()
    decisions: list[Decision] = []

    warnings: list[str] = []

    if step.image_batch:
        batch_id = catalog.resolve_image_batch(step.original_set, step.image_batch)
        image_ids = catalog.load_image_batch(batch_id)
        cand.images = set(image_ids)
        stats = {
            "batch_kind": "image",
            "batch_id": batch_id,
            "source": f"{step.original_set}@{step.image_batch}",
            "images_added": len(image_ids),
        }

        if step.with_annotations:
            # 拉一批影像通常就是要它們的標籤。一張圖可能被好幾個
            # annotation_batch 標過——全部帶進來，矛盾交給 conflict_resolve 攤開，
            # 而不是在這裡偷偷挑一個。
            cls_ids, det_ids = catalog.load_annotations_for_images(image_ids)
            cand.cls = set(cls_ids)
            cand.det = set(det_ids)
            sources = sorted(
                catalog.annotation_batches_of(cls_ids, "cls")
                | catalog.annotation_batches_of(det_ids, "det")
            )
            stats.update(
                {
                    "cls_added": len(cls_ids),
                    "det_added": len(det_ids),
                    "annotation_sources": sources,
                }
            )
            unannotated = len(
                cand.images
                - (
                    {catalog.cls(a).image_id for a in cls_ids}
                    | {catalog.det(a).image_id for a in det_ids}
                )
            )
            if sources:
                warnings.append(
                    f"step '{step.id}': Bringing {len(cls_ids) + len(det_ids)} annotations on these images"
                    f" (from {', '.join(sources)})"
                    + (
                        f"; {unannotated} images are not annotated"
                        if unannotated
                        else ""
                    )
                )
            elif cand.images:
                warnings.append(
                    f"step '{step.id}': this image batch has no annotations yet"
                )
    else:
        assert step.annotation_batch
        batch_id = catalog.resolve_annotation_batch(
            step.original_set, step.annotation_batch
        )
        cls_ids, det_ids = catalog.load_annotation_batch(batch_id)
        cand.cls = set(cls_ids)
        cand.det = set(det_ids)
        # annotation_batch 與 image_batch 是兩條獨立版本軸：標註指向的影像
        # 一併帶進來，否則 CandidateSet 的不變條件當場就破了。
        cand.images = {catalog.cls(a).image_id for a in cls_ids} | {
            catalog.det(a).image_id for a in det_ids
        }
        stats = {
            "batch_kind": "annotation",
            "batch_id": batch_id,
            "source": f"{step.original_set}@{step.annotation_batch}",
            "images_added": len(cand.images),
            "cls_added": len(cls_ids),
            "det_added": len(det_ids),
            "categories": len(catalog.categories_of_batch(batch_id)),
        }

    # 沒有逐筆 decision：「這一批全部進來」由 spec 的 source 參數本身說明，
    # 哪些 entity 進來了則由 CandidateSet 的成員關係決定。
    return StepResult(cand, stats, decisions, warnings)


def op_import_list(
    catalog: Catalog,
    inputs: list[CandidateSet],
    step: ImportListStep,
    file_names: list[str] | None = None,
) -> StepResult:
    """外部清單匯入。缺漏一定留痕，讓使用者能一次修完整份清單（design_doc §1 principle 6）。"""
    names = file_names if file_names is not None else (step.file_names or [])
    if not names:
        raise SpecError(
            f"step '{step.id}': the list behind file_names_ref is empty"
            " (the engine should load it from manual_set_import_lists before calling)"
        )

    batch_id = catalog.resolve_image_batch(step.original_set, step.image_batch)
    catalog.load_image_batch(batch_id)
    by_name = {catalog.image(i).file_name: i for i in catalog.images_of_batch(batch_id)}

    cand = CandidateSet()
    decisions: list[Decision] = []
    matched: list[str] = []
    missing: list[str] = []
    duplicated_in_list: list[str] = []
    seen: set[str] = set()

    for name in names:
        if name in seen:
            duplicated_in_list.append(name)
            continue
        seen.add(name)
        image_id = by_name.get(name)
        if image_id is None:
            missing.append(name)
            continue
        matched.append(name)
        cand.images.add(image_id)

    warnings: list[str] = []

    if step.with_annotations and cand.images:
        # 跟 source --image 一致：匯入一批影像通常就是要它們的標籤
        cls_ids, det_ids = catalog.load_annotations_for_images(cand.images)
        cand.cls = set(cls_ids)
        cand.det = set(det_ids)
        sources = sorted(
            catalog.annotation_batches_of(cls_ids, "cls")
            | catalog.annotation_batches_of(det_ids, "det")
        )
        if sources:
            warnings.append(
                f"step '{step.id}': Bringing {len(cls_ids) + len(det_ids)} annotations on these images"
                f" (from {', '.join(sources)})"
            )

    if missing:
        msg = (
            f"step '{step.id}': {len(missing)} names in the list match nothing in "
            f"{step.original_set}@{step.image_batch}"
            f" (first few: {', '.join(missing[:5])})"
        )
        if step.on_missing == "error":
            raise SpecError(msg)
        if step.on_missing == "warn":
            warnings.append(msg)
    if duplicated_in_list:
        warnings.append(
            f"step '{step.id}': the list repeats {len(duplicated_in_list)} file names; the repeats were ignored"
        )

    stats = {
        "source": f"{step.original_set}@{step.image_batch}",
        "requested": len(names),
        "matched_count": len(matched),
        "missing_count": len(missing),
        "missing": missing[:200],
        "duplicated_in_list": len(duplicated_in_list),
        "ambiguous_count": 0,  # UNIQUE(image_batch_id, file_name) 保證不會有
        "on_missing": step.on_missing,
        "cls_added": len(cand.cls),
        "det_added": len(cand.det),
    }
    return StepResult(cand, stats, decisions, warnings)


# ---------------------------------------------------------------------------
# 集合運算
# ---------------------------------------------------------------------------


def op_union(
    catalog: Catalog, inputs: list[CandidateSet], step: UnionStep
) -> StepResult:
    cand = CandidateSet(category_targets=_merge_targets(inputs, step.id))
    for other in inputs:
        cand.images |= other.images
        cand.cls |= other.cls
        cand.det |= other.det
    per_input = [c.counts() for c in inputs]
    overlap = (
        len(set.intersection(*[c.images for c in inputs])) if len(inputs) > 1 else 0
    )
    stats = {"inputs": per_input, "image_overlap": overlap, **cand.counts()}
    return StepResult(cand.prune(catalog), stats)


def op_intersect(
    catalog: Catalog, inputs: list[CandidateSet], step: IntersectStep
) -> StepResult:
    cand = CandidateSet(category_targets=_merge_targets(inputs, step.id))
    cand.images = set.intersection(*[c.images for c in inputs])
    cand.cls = set.intersection(*[c.cls for c in inputs])
    cand.det = set.intersection(*[c.det for c in inputs])
    stats = {"inputs": [c.counts() for c in inputs], **cand.counts()}
    return StepResult(cand.prune(catalog), stats)


def op_except(
    catalog: Catalog, inputs: list[CandidateSet], step: ExceptStep
) -> StepResult:
    base, *rest = inputs
    cand = base.copy()
    removed: set[int] = set()
    for other in rest:
        removed |= cand.images & other.images
        cand.images -= other.images
        cand.cls -= other.cls
        cand.det -= other.det
    decisions = [Decision("image", i, "dropped", "except") for i in removed]
    cand.prune(catalog)
    stats = {
        "inputs": [c.counts() for c in inputs],
        "images_removed": len(removed),
        **cand.counts(),
    }
    return StepResult(cand, stats, decisions)


# ---------------------------------------------------------------------------
# filter
# ---------------------------------------------------------------------------


def _hash_bucket(seed: str, key: str, mod: int) -> int:
    digest = hashlib.sha256(f"{seed}\x00{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % mod


def op_filter(
    catalog: Catalog,
    inputs: list[CandidateSet],
    step: FilterStep,
    file_names: list[str] | None = None,
) -> StepResult:
    """file_names 由 engine 傳入：step 用 file_names_ref 時，本體在資料庫裡，
    op 本身不查資料庫（保持純函式）。"""
    cand = inputs[0].copy()
    before = cand.counts()

    if step.criterion == "sample":
        return _filter_sample(catalog, cand, step, before)
    if step.criterion == "predicate":
        return _filter_predicate(catalog, cand, step, before)
    if step.criterion == "annotated":
        return _filter_annotated(catalog, cand, step, before)
    if step.criterion == "balance":
        return _filter_balance(catalog, cand, step, before)
    return _filter_explicit_list(catalog, cand, step, before, file_names)


def _filter_annotated(
    catalog: Catalog, cand: CandidateSet, step: FilterStep, before: dict[str, int]
) -> StepResult:
    """只留下帶有標註的影像。

    manual-set 是 training-ready 的資料集，schema 不允許裡面有沒標註的影像。
    要丟掉它們必須是顯式的一步，這樣 spec 上看得見、`cxr why` 也答得出來，
    而不是在 commit 時被系統偷偷清掉。
    """
    annotated = {catalog.cls(a).image_id for a in cand.cls} | {
        catalog.det(a).image_id for a in cand.det
    }
    drop = cand.images - annotated
    decisions = _drop_images(cand, catalog, drop, "filter:annotated")
    cand.prune(catalog)
    stats = {
        "criterion": "annotated",
        "before": before,
        "after": cand.counts(),
        "images_dropped": len(drop),
    }
    return StepResult(cand, stats, decisions)


def _filter_sample(
    catalog: Catalog, cand: CandidateSet, step: FilterStep, before: dict[str, int]
) -> StepResult:
    """決定性切割：hash(seed + key) % mod，不是真隨機——同一份 spec 永遠
    切出同一批圖（design_doc §1 principle 5）。key 優先用 subject_id，同病患的影像必定
    同進同出，避免 train/val 之間的 patient-level leakage。"""
    assert step.mod and step.keep_remainder is not None and step.seed is not None
    keep_set = set(step.keep_remainder)

    drop: set[int] = set()
    fallback_images = 0
    subjects_kept: set[str] = set()
    subjects_dropped: set[str] = set()
    detail: dict[int, dict[str, Any]] = {}

    for image_id in sorted(cand.images):
        if step.key_field == "subject_id":
            key, is_fallback = catalog.subject_key(image_id)
            fallback_images += int(is_fallback)
        elif step.key_field == "file_name":
            key, is_fallback = catalog.image(image_id).file_name, False
        else:
            key, is_fallback = str(image_id), False

        bucket = _hash_bucket(step.seed, key, step.mod)
        if bucket in keep_set:
            subjects_kept.add(key)
        else:
            drop.add(image_id)
            subjects_dropped.add(key)
            detail[image_id] = {
                "key": key,
                "bucket": bucket,
                "subject_unknown": is_fallback,
            }

    decisions = _drop_images(
        cand, catalog, drop, "filter:sample", lambda i: detail.get(i)
    )
    cand.prune(catalog)

    stats = {
        "criterion": "sample",
        "method": step.method,
        "key_field": step.key_field,
        "mod": step.mod,
        "keep_remainder": step.keep_remainder,
        "seed": step.seed,
        "before": before,
        "after": cand.counts(),
        "images_dropped": len(drop),
        "subjects_kept": len(subjects_kept),
        "subjects_dropped": len(subjects_dropped),
        # design_doc §1 principle 5：明確告訴使用者「保證的強度」——這些圖沒有病患資訊，
        # 是用 image_id 各自獨立切的
        "subject_unknown_images": fallback_images,
    }
    warnings = []
    if step.key_field == "subject_id" and fallback_images:
        warnings.append(
            f"step '{step.id}': {fallback_images} images have no subject information "
            "and were split by image_id instead — no patient-level leakage guarantee for them"
        )
    # 同一個 key 同時出現在留下與丟棄兩邊，代表切割根本沒生效（不該發生）
    assert not (subjects_kept & subjects_dropped), (
        "a hash_mod split must keep each key on one side"
    )
    return StepResult(cand, stats, decisions, warnings)


def _label_context(catalog: Catalog, cand: CandidateSet) -> dict[int, dict[str, Any]]:
    """每張影像在目前候選集合裡的標註摘要，供 predicate 與 balance 使用。

    一次算完再查表：每張圖各自去掃一遍候選集合的話，一個 filter 就變成
    O(影像數 × 標註數)。
    """
    context: dict[int, dict[str, Any]] = {
        image_id: {
            "labels": set(),
            "targets": set(),
            "annotators": set(),
            "n_annotations": 0,
        }
        for image_id in cand.images
    }
    for kind, pool in (("cls", cand.cls), ("det", cand.det)):
        for ann_id in pool:
            ann = catalog.annotation(kind, ann_id)
            entry = context.get(ann.image_id)
            if entry is None:
                continue
            entry["labels"].add(catalog.category(ann.category_id).name)
            entry["annotators"].add(ann.annotator_name)
            entry["n_annotations"] += 1
            target = cand.category_targets.get(ann.category_id)
            if target:
                entry["targets"].add(target)
    # predicate 的 `in` 對 list 比對，排序過的 list 也讓 stats 穩定
    return {
        image_id: {
            "labels": sorted(entry["labels"]),
            "targets": sorted(entry["targets"]),
            "annotators": sorted(entry["annotators"]),
            "n_annotations": entry["n_annotations"],
        }
        for image_id, entry in context.items()
    }


def _filter_balance(
    catalog: Catalog, cand: CandidateSet, step: FilterStep, before: dict[str, int]
) -> StepResult:
    """把每個類別的影像數壓到 max_per_class 以下。

    多標籤讓「每類剛好 N 張」無法同時成立——一張同時是 pneumonia 與
    effusion 的圖，留下它會同時佔用兩個配額。做法是**從最罕見的類別開始**
    依序配額，並優先選已經被選中的影像：罕見類別先拿滿自己的份，共用的
    影像順便幫常見類別填數，常見類別才不會把罕見類別的樣本吃掉。

    挑選順序是 hash(seed + image_id)，不是真隨機——同一份 spec 永遠挑出
    同一批（跟 sample 切割一致）。
    """
    assert step.max_per_class and step.seed
    context = _label_context(catalog, cand)
    key = "targets" if step.by == "target" else "labels"

    by_class: dict[str, list[int]] = defaultdict(list)
    for image_id, entry in context.items():
        for name in entry[key]:
            by_class[name].append(image_id)
    if not by_class:
        raise SpecError(
            f"step '{step.id}': nothing to balance — the candidate set has no "
            + (
                "target category (run category_map first)"
                if step.by == "target"
                else "categories"
            )
        )

    before_counts = {name: len(ids) for name, ids in by_class.items()}
    keep: set[int] = set()
    # 罕見的先配額，否則常見類別會先把共用的多標籤影像用掉
    for name in sorted(by_class, key=lambda n: (len(by_class[n]), n)):
        members = sorted(
            by_class[name],
            # 已經被選中的排前面，讓多標籤影像同時滿足多個配額
            key=lambda i: (i not in keep, _hash_bucket(step.seed, str(i), 2**32), i),
        )
        keep.update(members[: step.max_per_class])

    drop = cand.images - keep
    decisions = _drop_images(cand, catalog, drop, "filter:balance")
    cand.prune(catalog)

    after_context = _label_context(catalog, cand)
    after_counts: Counter[str] = Counter()
    for entry in after_context.values():
        for name in entry[key]:
            after_counts[name] += 1

    warnings: list[str] = []
    over = {n: c for n, c in after_counts.items() if c > step.max_per_class}
    if over:
        warnings.append(
            f"step '{step.id}': {', '.join(f'{n}={c}' for n, c in sorted(over.items()))} "
            f"still exceed the cap of {step.max_per_class}: those images also carry other labels "
            "and were kept to fill the quotas of those classes"
        )

    stats = {
        "criterion": "balance",
        "by": step.by,
        "max_per_class": step.max_per_class,
        "seed": step.seed,
        "before": before,
        "after": cand.counts(),
        "images_dropped": len(drop),
        "class_counts_before": dict(sorted(before_counts.items())),
        "class_counts_after": dict(sorted(after_counts.items())),
    }
    return StepResult(cand, stats, decisions, warnings)


def _filter_predicate(
    catalog: Catalog, cand: CandidateSet, step: FilterStep, before: dict[str, int]
) -> StepResult:
    assert step.expression
    tree = pred.compile_predicate(step.expression)
    context = _label_context(catalog, cand)
    drop = {
        i for i in cand.images if not pred.evaluate(tree, catalog, i, context.get(i))
    }
    decisions = _drop_images(cand, catalog, drop, "filter:predicate")
    cand.prune(catalog)
    stats = {
        "criterion": "predicate",
        "expression": step.expression,
        "before": before,
        "after": cand.counts(),
        "images_dropped": len(drop),
    }
    return StepResult(cand, stats, decisions)


def _filter_explicit_list(
    catalog: Catalog,
    cand: CandidateSet,
    step: FilterStep,
    before: dict[str, int],
    file_names: list[str] | None = None,
) -> StepResult:
    """在既有候選集合裡縮限——清單裡的檔名必須已經在 input 的結果中，
    這是它跟 import_list 的差別。"""
    names = file_names if file_names is not None else (step.file_names or [])
    if not names:
        raise SpecError(
            f"step '{step.id}': the list behind file_names_ref is empty"
            " (the engine should load it from manual_set_import_lists before calling)"
        )
    wanted = set(names)
    by_name: dict[str, list[int]] = defaultdict(list)
    for image_id in cand.images:
        by_name[catalog.image(image_id).file_name].append(image_id)

    keep: set[int] = set()
    missing: list[str] = []
    for name in wanted:
        hits = by_name.get(name)
        if not hits:
            missing.append(name)
            continue
        keep.update(hits)

    drop = cand.images - keep
    decisions = _drop_images(cand, catalog, drop, "filter:explicit_list")
    cand.prune(catalog)

    warnings: list[str] = []
    if missing:
        msg = (
            f"step '{step.id}': {len(missing)} names in the list are not in the result of input '{step.input}'"
            f" (first few: {', '.join(sorted(missing)[:5])})"
        )
        if step.on_missing == "error":
            raise SpecError(msg)
        if step.on_missing == "warn":
            warnings.append(msg)

    stats = {
        "criterion": "explicit_list",
        "requested": len(wanted),
        "matched_count": len(keep),
        "missing_count": len(missing),
        "missing": sorted(missing)[:200],
        "before": before,
        "after": cand.counts(),
        "images_dropped": len(drop),
    }
    return StepResult(cand, stats, decisions, warnings)


# ---------------------------------------------------------------------------
# dedup
# ---------------------------------------------------------------------------


def find_duplicates(catalog: Catalog, cand: CandidateSet) -> list[dict[str, Any]]:
    """把候選集合裡 blake3 相同的影像分組攤開，連同各自帶的標註。

    dedup 就是明確地丟掉重複影像和它們的標註，所以決定「留哪一張」之前
    應該先看清楚每一張各自帶了什麼——這支就是 `duplicates` 指令的資料來源。
    """
    by_hash: dict[str, list[int]] = defaultdict(list)
    for image_id in cand.images:
        digest = catalog.image(image_id).blake3_hash
        if digest is not None:
            by_hash[digest].append(image_id)

    groups: list[dict[str, Any]] = []
    for digest, members in sorted(by_hash.items()):
        if len(members) < 2:
            continue
        candidates = []
        for image_id in sorted(members):
            meta = catalog.image(image_id)
            cls_here = sorted(
                a for a in cand.cls if catalog.cls(a).image_id == image_id
            )
            det_here = sorted(
                a for a in cand.det if catalog.det(a).image_id == image_id
            )
            candidates.append(
                {
                    "image_id": image_id,
                    "ref": meta.ref,
                    "source": meta.source,
                    "subject_id": meta.subject_id,
                    "cls_annotation_ids": cls_here,
                    "det_annotation_ids": det_here,
                    "labels": sorted(
                        {
                            catalog.category(catalog.cls(a).category_id).name
                            for a in cls_here
                        }
                        | {
                            catalog.category(catalog.det(a).category_id).name
                            for a in det_here
                        }
                    ),
                }
            )
        groups.append(
            {
                "blake3": digest,
                "cross_source": len({c["source"] for c in candidates}) > 1,
                "candidates": candidates,
            }
        )
    return groups


def op_dedup(
    catalog: Catalog, inputs: list[CandidateSet], step: DedupStep
) -> StepResult:
    """依 blake3_hash 去重，同一內容只留優先權最高的來源那一張。

    注意：被淘汰那張圖身上的標註也會一起消失，不會改掛到留下來的那張——
    schema 的複合外鍵要求標註必須跟著它自己的影像走，改掛等於偽造標註
    歸屬。這件事會明確算進 stats，不靜默發生。
    """
    cand = inputs[0].copy()
    before = cand.counts()
    priority = {name: rank for rank, name in enumerate(step.source_priority)}
    fallback_rank = len(priority)

    # 哪些影像身上有標註（在目前候選集合裡）
    annotated = {catalog.cls(a).image_id for a in cand.cls} | {
        catalog.det(a).image_id for a in cand.det
    }
    pinned = set(step.keep)

    by_hash: dict[str, list[int]] = defaultdict(list)
    no_hash = 0
    for image_id in cand.images:
        digest = catalog.image(image_id).blake3_hash
        if digest is None:
            no_hash += 1
            continue
        by_hash[digest].append(image_id)

    drop: set[int] = set()
    detail: dict[int, dict[str, Any]] = {}
    dup_groups = 0
    cross_source_groups = 0
    manual_groups = 0

    for digest, group in by_hash.items():
        if len(group) == 1:
            continue
        dup_groups += 1
        sources = {catalog.image(i).original_set_name for i in group}
        if len(sources) > 1:
            cross_source_groups += 1
        # 排序鍵：人工指定 → 有標註 → 來源優先權 → image_id
        # （最後一項讓結果穩定，與輸入順序無關）。
        # blake3 相同就是同一張照片，留下沒標註的那份等於白白丟掉標籤，
        # 所以 source_priority 只在前兩項分不出勝負時才決定。
        group.sort(
            key=lambda i: (
                0 if i in pinned else 1,
                0 if (step.prefer_annotated and i in annotated) else 1,
                priority.get(catalog.image(i).original_set_name, fallback_rank),
                i,
            )
        )
        chosen = [i for i in group if i in pinned]
        if len(chosen) > 1:
            raise SpecError(
                f"step '{step.id}': keep names {len(chosen)} images in the same duplicate group"
                f" ({', '.join(catalog.image(i).ref for i in chosen)}); keep one per group"
            )
        if chosen:
            manual_groups += 1
        winner, losers = group[0], group[1:]
        for loser in losers:
            drop.add(loser)
            detail[loser] = {
                "blake3": digest,
                "kept_image_id": winner,
                "kept_source": catalog.image(winner).ref,
                "dropped_source": catalog.image(loser).ref,
            }

    cls_before, det_before = len(cand.cls), len(cand.det)
    decisions = _drop_images(
        cand, catalog, drop, "dedup:blake3", lambda i: detail.get(i)
    )
    cand.prune(catalog)

    warnings: list[str] = []
    lost_annotations = (cls_before - len(cand.cls)) + (det_before - len(cand.det))
    if lost_annotations:
        warnings.append(
            f"step '{step.id}': dedup also removed {lost_annotations} annotations attached to duplicate images"
            " (use duplicates to see what each group carries, then keep to choose the survivor)"
        )
    if no_hash:
        warnings.append(
            f"step '{step.id}': {no_hash} images have no blake3_hash and were kept without deduplication"
        )

    stats = {
        "key": step.key,
        "source_priority": step.source_priority,
        "before": before,
        "after": cand.counts(),
        "duplicate_groups": dup_groups,
        "cross_source_groups": cross_source_groups,
        "images_dropped": len(drop),
        "annotations_dropped_with_duplicates": lost_annotations,
        "images_without_hash": no_hash,
        "prefer_annotated": step.prefer_annotated,
        "manually_chosen_groups": manual_groups,
    }
    return StepResult(cand, stats, decisions, warnings)


# ---------------------------------------------------------------------------
# category_map
# ---------------------------------------------------------------------------


def local_categories_in(catalog: Catalog, cand: CandidateSet) -> set[int]:
    return {catalog.cls(a).category_id for a in cand.cls} | {
        catalog.det(a).category_id for a in cand.det
    }


def op_category_map(
    catalog: Catalog, inputs: list[CandidateSet], step: CategoryMapStep
) -> StepResult:
    """local category → target category。

    刻意要求顯式且完整：沒被映射到的 local category 不會悄悄留著也不會
    悄悄消失，require_total=True 時直接讓 build 失敗，逼使用者處理（design_doc §3）。
    """
    cand = inputs[0].copy()
    present = local_categories_in(catalog, cand)
    catalog.ensure_categories(present)

    targets = dict(cand.category_targets)
    decisions: list[Decision] = []

    if step.merge_identical:
        for category_id in present:
            targets.setdefault(category_id, catalog.category(category_id).name)

    unknown_scopes: list[str] = []
    unknown_locals: list[str] = []
    for scope, table in step.mapping.items():
        scope_categories = {
            catalog.category(c).name: c
            for c in present
            if catalog.category(c).scope == scope
        }
        if not scope_categories:
            unknown_scopes.append(scope)
        for local_name, target_name in table.items():
            category_id = scope_categories.get(local_name)
            if category_id is None:
                unknown_locals.append(f"{scope}:{local_name}")
                continue
            targets[category_id] = target_name
            decisions.append(
                Decision(
                    "category",
                    category_id,
                    "remapped",
                    "category_map",
                    {"scope": scope, "local": local_name, "target": target_name},
                )
            )

    unmapped = sorted(catalog.category(c).ref for c in present if c not in targets)
    if unmapped and step.require_total:
        raise SpecError(
            f"step '{step.id}': {len(unmapped)} local categories are not mapped: "
            + ", ".join(unmapped[:10])
            + ("…" if len(unmapped) > 10 else "")
            + " (to allow it set require_total: false; their annotations are then dropped explicitly)"
        )

    warnings: list[str] = []
    if unmapped and step.drop_unmapped:
        unmapped_ids = {c for c in present if c not in targets}
        dropped_cls = {
            a for a in cand.cls if catalog.cls(a).category_id in unmapped_ids
        }
        dropped_det = {
            a for a in cand.det if catalog.det(a).category_id in unmapped_ids
        }
        cand.cls -= dropped_cls
        cand.det -= dropped_det
        for ann_id in dropped_cls:
            decisions.append(
                Decision("cls_annotation", ann_id, "dropped", "category_map:unmapped")
            )
        for ann_id in dropped_det:
            decisions.append(
                Decision("det_annotation", ann_id, "dropped", "category_map:unmapped")
            )
        warnings.append(
            f"step '{step.id}': {len(unmapped)} local categories are not mapped; "
            f"dropped their {len(dropped_cls) + len(dropped_det)} annotations"
        )
    # drop_unmapped=False 時「還有幾個沒映射」是**當下的待辦狀態**，不是這一步
    # 做了什麼的事實——後面補上映射它就不成立了。放進 stats 讓 preview 的
    # category 面板即時反映，不當成 step warning 一路累積下去。
    if unknown_scopes:
        warnings.append(
            f"step '{step.id}': mapping scope {', '.join(unknown_scopes)} "
            "has no annotations in the candidate set, so that mapping does nothing"
        )
    if unknown_locals:
        warnings.append(
            f"step '{step.id}': mapping names local categories that do not exist or are not in the candidate set: "
            + ", ".join(unknown_locals[:10])
        )

    cand.category_targets = targets
    kept_targets = {targets[c] for c in present if c in targets}
    stats = {
        "local_categories_present": len(present),
        "mapped": len([c for c in present if c in targets]),
        "unmapped": unmapped,
        "drop_unmapped": step.drop_unmapped,
        "target_categories": sorted(kept_targets),
        # target 被宣告卻沒有任何 local 映射過來——通常是打錯字，要顯示出來
        "targets_without_source": sorted(
            {t for table in step.mapping.values() for t in table.values()}
            - kept_targets
        ),
        "after": cand.counts(),
    }
    return StepResult(cand, stats, decisions, warnings)


# ---------------------------------------------------------------------------
# conflict_resolve
# ---------------------------------------------------------------------------


@dataclass
class ConflictGroup:
    """同一張影像被多個 annotation_batch 標註時的完整攤開。

    分兩種：
      * `contradiction`——各來源給的 target category 集合不一樣，
        這是真的矛盾（一邊說 normal，一邊說 pneumonia）。
      * `duplicate`——各來源給的標籤一致，內容沒有矛盾，但同一個
        (image, target) 有多筆標註，訓練時仍然只能留一筆。
    """

    image_id: int
    image_ref: str
    kind: str
    by_source: dict[str, dict[str, Any]]

    @property
    def annotation_ids(self) -> list[int]:
        return sorted(
            a for src in self.by_source.values() for a in src["annotation_ids"]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_id": self.image_id,
            "image": self.image_ref,
            "kind": self.kind,
            "sources": self.by_source,
        }


def find_conflicts(catalog: Catalog, cand: CandidateSet) -> list[ConflictGroup]:
    """攤開所有「同一張圖被多個標註來源覆蓋」的情況（design_doc §6：先給人看，再決定規則）。

    刻意以「影像」而非「(影像, target_category)」為單位分組。只看
    (image, target_category) 會漏掉最重要的那種衝突——A 來源說 normal、
    B 來源說 pneumonia 時，兩筆標註分屬不同 target，各自成組、各自只有
    一筆，看起來毫無衝突，但那正是必須有人裁決的矛盾。

    det 標註不參與：框的分歧是幾何問題（IoU 高低），跟「同一張圖有兩個
    互斥的答案」不是同一回事，硬套同一套 precedence 只會產生假的確定性。
    """
    per_image: dict[int, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    for ann_id in cand.cls:
        ann = catalog.cls(ann_id)
        if ann.category_id in cand.category_targets:
            per_image[ann.image_id][ann.annotation_batch_id].append(ann_id)

    groups: list[ConflictGroup] = []
    for image_id, by_batch in per_image.items():
        if len(by_batch) < 2:
            continue
        by_source: dict[str, dict[str, Any]] = {}
        target_sets: list[frozenset[str]] = []
        for batch_id, ann_ids in by_batch.items():
            first = catalog.cls(ann_ids[0])
            targets = frozenset(
                cand.category_targets[catalog.cls(a).category_id] for a in ann_ids
            )
            target_sets.append(targets)
            by_source[first.source] = {
                "annotation_batch_id": batch_id,
                "batch_version": first.batch_version,
                "annotation_ids": sorted(ann_ids),
                "targets": sorted(targets),
                "annotators": sorted({catalog.cls(a).annotator_name for a in ann_ids}),
                "local_categories": sorted(
                    catalog.category(catalog.cls(a).category_id).name for a in ann_ids
                ),
                "max_score": max((catalog.cls(a).score or 0.0) for a in ann_ids),
            }
        kind = "duplicate" if len(set(target_sets)) == 1 else "contradiction"
        groups.append(
            ConflictGroup(image_id, catalog.image(image_id).ref, kind, by_source)
        )

    groups.sort(key=lambda g: (g.kind != "contradiction", g.image_id))
    return groups


def _rank_by(order: list[str], value: str) -> int:
    """不在清單裡的一律排在最後——但呼叫端會檢查是否真的命中清單，
    避免「沒有規則涵蓋」被靜默當成「排最後那個贏」。"""
    try:
        return order.index(value)
    except ValueError:
        return len(order)


def _pick_winner_batch(
    catalog: Catalog,
    group: ConflictGroup,
    rule,
    manual_index: dict[int, str],
) -> tuple[int | None, str]:
    """依單一規則挑出這張影像的勝出 annotation_batch。

    挑不出來（規則涵蓋不到、或並列）就回 None，交給下一條規則——
    刻意不做靜默 fallback，讓涵蓋不到的情況現形（design_doc §6）。
    """
    batches = {src["annotation_batch_id"]: src for src in group.by_source.values()}

    if rule.rule == "manual":
        # 一筆標註只屬於一張影像，所以 annotation_id 本身就定位得到這一組
        for keep_id, reason in manual_index.items():
            for batch_id, src in batches.items():
                if keep_id in src["annotation_ids"]:
                    return batch_id, f"manual:{reason}" if reason else "manual"
        return None, ""

    if rule.rule == "annotator_precedence":
        order = rule.annotator_precedence or []
        ranked = {
            batch_id: min(_rank_by(order, a) for a in src["annotators"])
            for batch_id, src in batches.items()
        }
        best = min(ranked.values())
        winners = [b for b, r in ranked.items() if r == best]
        if best < len(order) and len(winners) == 1:
            return winners[0], "annotator_precedence"
        return None, ""

    if rule.rule == "batch_version_precedence":
        order = rule.version_precedence or []
        ranked = {
            batch_id: _rank_by(order, src["batch_version"])
            for batch_id, src in batches.items()
        }
        best = min(ranked.values())
        winners = [b for b, r in ranked.items() if r == best]
        if best < len(order) and len(winners) == 1:
            return winners[0], "batch_version_precedence"
        return None, ""

    if rule.rule == "highest_score":
        best = max(src["max_score"] for src in batches.values())
        winners = [b for b, src in batches.items() if src["max_score"] == best]
        if len(winners) == 1:
            return winners[0], "highest_score"
        return None, ""

    return None, ""


def op_conflict_resolve(
    catalog: Catalog, inputs: list[CandidateSet], step: ConflictResolveStep
) -> StepResult:
    """依規則裁決衝突：每張有爭議的影像挑出一個勝出的標註來源，
    其餘來源在這張圖上的標註全部剔除。

    「以來源為單位」而不是「以單筆標註為單位」裁決，是因為一次判讀是一
    個整體：留下資深醫師說的 pneumonia、同時留下實習醫師說的 normal，
    等於自己造出一筆自相矛盾的訓練資料。
    """
    cand = inputs[0].copy()
    groups = find_conflicts(catalog, cand)
    decisions: list[Decision] = []
    resolved_by: Counter[str] = Counter()
    unresolved: list[dict[str, Any]] = []
    drop: set[int] = set()

    manual_index: dict[int, str] = {}
    for rule in step.rules:
        if rule.rule == "manual":
            for d in rule.decisions or []:
                manual_index[d.keep_annotation_id] = d.reason

    for group in groups:
        winner_batch: int | None = None
        winning_rule = ""
        for rule in step.rules:
            winner_batch, winning_rule = _pick_winner_batch(
                catalog, group, rule, manual_index
            )
            if winner_batch is not None:
                break

        if winner_batch is None:
            unresolved.append(group.to_dict())
            continue

        resolved_by[winning_rule.split(":")[0]] += 1
        winner_source = next(
            src
            for src in group.by_source.values()
            if src["annotation_batch_id"] == winner_batch
        )
        for source_label, src in group.by_source.items():
            if src["annotation_batch_id"] == winner_batch:
                continue
            for ann_id in src["annotation_ids"]:
                drop.add(ann_id)
                decisions.append(
                    Decision(
                        "cls_annotation",
                        ann_id,
                        "dropped",
                        f"conflict_resolve:{winning_rule}",
                        {
                            "image": group.image_ref,
                            "kind": group.kind,
                            "lost_source": source_label,
                            "lost_targets": src["targets"],
                            "kept_source": next(
                                lbl
                                for lbl, s in group.by_source.items()
                                if s["annotation_batch_id"] == winner_batch
                            ),
                            "kept_targets": winner_source["targets"],
                            "kept_annotation_ids": winner_source["annotation_ids"],
                        },
                    )
                )

    cand.cls -= drop

    # 勝出的來源自己在同一張圖同一個 target 上還留著多筆時，收斂成一筆
    # （同一個 batch 對同一張圖標了兩次同樣的類別，訓練時仍然只能留一筆）
    per_target: dict[tuple[int, str], list[int]] = defaultdict(list)
    for ann_id in cand.cls:
        ann = catalog.cls(ann_id)
        target = cand.category_targets.get(ann.category_id)
        if target:
            per_target[(ann.image_id, target)].append(ann_id)
    collapsed = 0
    for (image_id, target), members in per_target.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda a: (-(catalog.cls(a).score or 0.0), a))
        for loser in members[1:]:
            cand.cls.discard(loser)
            collapsed += 1
            decisions.append(
                Decision(
                    "cls_annotation",
                    loser,
                    "dropped",
                    "conflict_resolve:same_target_duplicate",
                    {
                        "image": catalog.image(image_id).ref,
                        "target_category": target,
                        "kept_annotation_id": members[0],
                    },
                )
            )

    contradictions = [g for g in groups if g.kind == "contradiction"]
    if unresolved and step.strict:
        sample = ", ".join(u["image"] for u in unresolved[:5])
        raise SpecError(
            f"step '{step.id}': {len(unresolved)} images have conflicts no rule could settle ({sample}). "
            "Add a rule or pick them one by one with rule=manual; to let them through set strict: false"
            " (unresolved images keep every source's annotations, carrying the contradiction into the training data)"
        )

    # 同一張圖有多個 target category：多標籤在 CXR 是合理的，不當衝突處理，
    # 只回報讓使用者自己判斷。
    per_image_targets: dict[int, set[str]] = defaultdict(set)
    for ann_id in cand.cls:
        ann = catalog.cls(ann_id)
        target = cand.category_targets.get(ann.category_id)
        if target:
            per_image_targets[ann.image_id].add(target)
    multi_label = Counter(len(t) for t in per_image_targets.values() if len(t) > 1)

    stats = {
        "conflict_groups": len(groups),
        "contradictions": len(contradictions),
        "duplicates": len(groups) - len(contradictions),
        "resolved": sum(resolved_by.values()),
        "resolved_by_rule": dict(resolved_by),
        "unresolved_count": len(unresolved),
        "unresolved": unresolved[:100],
        "annotations_dropped": len(drop),
        "same_target_collapsed": collapsed,
        "multi_label_images": {str(k): v for k, v in multi_label.items()},
        "strict": step.strict,
        "after": cand.counts(),
    }
    warnings = []
    if unresolved:
        warnings.append(
            f"step '{step.id}': {len(unresolved)} images have unresolved conflicts; "
            "every source's annotations were kept (strict=false), so the contradictions go into the training data"
        )
    return StepResult(cand, stats, decisions, warnings)


# ---------------------------------------------------------------------------
# manual_override
# ---------------------------------------------------------------------------


def op_manual_override(
    catalog: Catalog, inputs: list[CandidateSet], step: ManualOverrideStep
) -> StepResult:
    cand = inputs[0].copy()
    decisions: list[Decision] = []
    applied = Counter()
    failed: list[str] = []

    # 指名的標註不見得屬於已 source 的批次——先補載，使用者不該為了指名
    # 一筆標註而被迫先 source 整個批次
    for kind in ("cls", "det"):
        catalog.ensure_annotations(
            kind,
            [
                ov.annotation_id
                for ov in step.overrides
                if ov.annotation_id is not None and ov.annotation_kind == kind
            ],
        )

    # 指名的影像同樣先確保 catalog 認得（它不見得屬於已 source 的批次）
    catalog.ensure_images(
        [ov.image_id for ov in step.overrides if ov.image_id is not None]
    )

    for ov in step.overrides:
        if ov.action in ("exclude_image", "include_image"):
            assert ov.image_id is not None
            try:
                meta = catalog.image(ov.image_id)
            except KeyError:
                failed.append(
                    f"{ov.action} image #{ov.image_id} (no such image_id in the database)"
                )
                continue
            if ov.action == "exclude_image":
                if meta.id not in cand.images:
                    # 靜默成功最糟：你以為排除了，其實它本來就不在
                    failed.append(
                        f"exclude_image #{meta.id} {meta.ref} (it was not in the candidate set to begin with)"
                    )
                    continue
                decisions += _drop_images(
                    cand,
                    catalog,
                    {meta.id},
                    f"manual_override:{ov.reason or 'exclude_image'}",
                )
            else:
                if meta.id in cand.images:
                    failed.append(
                        f"include_image #{meta.id} {meta.ref} (it is already in the candidate set)"
                    )
                    continue
                cand.images.add(meta.id)
                decisions.append(
                    Decision(
                        "image",
                        meta.id,
                        "overridden",
                        f"manual_override:{ov.reason or 'include_image'}",
                        {"image": meta.ref},
                    )
                )
                # 跟 source --image 一致：納入影像就把它的標註一起帶進來，
                # 否則這張圖會沒有標籤，commit 時被約束擋下來
                cls_ids, det_ids = catalog.load_annotations_for_images([meta.id])
                cand.cls |= set(cls_ids)
                cand.det |= set(det_ids)
                for ann_id in cls_ids:
                    decisions.append(
                        Decision(
                            "cls_annotation",
                            ann_id,
                            "overridden",
                            f"manual_override:{ov.reason or 'include_image'}",
                        )
                    )
                for ann_id in det_ids:
                    decisions.append(
                        Decision(
                            "det_annotation",
                            ann_id,
                            "overridden",
                            f"manual_override:{ov.reason or 'include_image'}",
                        )
                    )
            applied[ov.action] += 1
            continue

        pool = cand.cls if ov.annotation_kind == "cls" else cand.det
        kind = f"{ov.annotation_kind}_annotation"
        ann_id = int(ov.annotation_id or 0)
        if ov.action == "exclude_annotation":
            if ann_id not in pool:
                failed.append(f"exclude_annotation {ann_id} (not in the candidate set)")
                continue
            pool.discard(ann_id)
            decisions.append(
                Decision(
                    kind,
                    ann_id,
                    "overridden",
                    f"manual_override:{ov.reason or 'exclude'}",
                )
            )
        else:
            try:
                ann = catalog.annotation(ov.annotation_kind, ann_id)
            except KeyError:
                failed.append(
                    f"include_annotation {ann_id} (the catalog did not load this annotation)"
                )
                continue
            if ann.image_id not in cand.images:
                # 標註納入時對應影像必須先在集合裡——這是 schema 的複合外鍵，
                # 這裡先擋掉，比等到 commit 才炸掉好懂得多
                failed.append(
                    f"include_annotation {ann_id} (its image "
                    f"{catalog.image(ann.image_id).ref} is not in the candidate set)"
                )
                continue
            if ann_id in pool:
                failed.append(
                    f"include_annotation {ann_id} (it is already in the candidate set)"
                )
                continue
            pool.add(ann_id)
            decisions.append(
                Decision(
                    kind,
                    ann_id,
                    "overridden",
                    f"manual_override:{ov.reason or 'include'}",
                )
            )
        applied[ov.action] += 1

    if failed:
        raise SpecError(
            f"step '{step.id}': {len(failed)} overrides could not be applied: "
            + "; ".join(failed[:5])
        )

    cand.prune(catalog)

    # 剔除標註可能讓影像變成沒有任何標籤——manual-set 不接受那種影像，
    # 與其等到 commit 才被約束擋下來，不如當場說清楚是哪幾張
    annotated = {catalog.cls(a).image_id for a in cand.cls} | {
        catalog.det(a).image_id for a in cand.det
    }
    orphaned = sorted(cand.images - annotated)
    if orphaned and any(o.action == "exclude_annotation" for o in step.overrides):
        sample = ", ".join(catalog.image(i).ref for i in orphaned[:3])
        raise SpecError(
            f"step '{step.id}': excluding those annotations leaves {len(orphaned)} images with no label"
            f" ({sample}). A manual-set does not accept unannotated images — "
            "exclude those images with exclude_image as well."
        )
    stats = {
        "applied": dict(applied),
        "override_count": len(step.overrides),
        **cand.counts(),
    }
    return StepResult(cand, stats, decisions)


OPS: dict[str, Callable[..., StepResult]] = {
    "source": op_source,
    "import_list": op_import_list,
    "union": op_union,
    "intersect": op_intersect,
    "except": op_except,
    "filter": op_filter,
    "dedup": op_dedup,
    "category_map": op_category_map,
    "conflict_resolve": op_conflict_resolve,
    "manual_override": op_manual_override,
}
