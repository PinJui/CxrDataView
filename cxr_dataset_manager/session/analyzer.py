"""preview() 的統計摘要（design_doc.md §3）。

回傳的永遠是**摘要**（數量、分布、diff），不是把上萬張圖列出來——
探索才會快。摘要至少包含：這一步前後的數量變化、被排除原因的分布、
category 覆蓋率與尚未映射的 local category、以及切割時 fallback 用
image_id 的張數（讓使用者知道病患層級保證的強度）。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from cxr_dataset_manager.core.ops import find_conflicts, local_categories_in
from cxr_dataset_manager.core.types import CandidateSet, Catalog


def _date_range(catalog: Catalog, image_ids: set[int]) -> dict[str, Any]:
    dates = [d for d in (catalog.image(i).date_captured for i in image_ids) if d]
    return {
        "min": min(dates).isoformat() if dates else None,
        "max": max(dates).isoformat() if dates else None,
        "unknown": len(image_ids) - len(dates),
    }


def summarize(
    catalog: Catalog,
    cand: CandidateSet,
    previous: Optional[CandidateSet] = None,
    step_stats: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    images = cand.images
    subjects = Counter()
    subject_unknown = 0
    by_source = Counter()
    sizes = Counter()

    for image_id in images:
        meta = catalog.image(image_id)
        by_source[meta.source] += 1
        sizes[f"{meta.width}x{meta.height}"] += 1
        if meta.subject_id:
            subjects[meta.subject_id] += 1
        else:
            subject_unknown += 1

    local_counts = Counter()
    target_counts = Counter()
    annotator_counts = Counter()
    unmapped: Counter[str] = Counter()
    for ann_id in cand.cls:
        ann = catalog.cls(ann_id)
        cat = catalog.category(ann.category_id)
        local_counts[cat.ref] += 1
        annotator_counts[ann.annotator_name] += 1
        target = cand.category_targets.get(ann.category_id)
        if target:
            target_counts[target] += 1
        else:
            unmapped[cat.ref] += 1
    for ann_id in cand.det:
        ann = catalog.det(ann_id)
        cat = catalog.category(ann.category_id)
        local_counts[cat.ref] += 1
        annotator_counts[ann.annotator_name] += 1
        target = cand.category_targets.get(ann.category_id)
        if target:
            target_counts[target] += 1
        else:
            unmapped[cat.ref] += 1

    annotated_images = {catalog.cls(a).image_id for a in cand.cls} | {
        catalog.det(a).image_id for a in cand.det
    }

    summary: dict[str, Any] = {
        "counts": cand.counts(),
        "by_source": dict(by_source.most_common()),
        "by_local_category": dict(local_counts.most_common()),
        "by_target_category": dict(target_counts.most_common()),
        "by_annotator": dict(annotator_counts.most_common()),
        "image_sizes": dict(sizes.most_common(5)),
        "date_captured": _date_range(catalog, images),
        "subjects": {
            "distinct": len(subjects),
            # design_doc §1 principle 5：明確顯示保證的強度，而不是讓使用者以為切割一定安全
            "images_without_subject": subject_unknown,
            "max_images_per_subject": max(subjects.values()) if subjects else 0,
        },
        "annotation_coverage": {
            "images_with_annotation": len(annotated_images),
            "images_without_annotation": len(images - annotated_images),
        },
        "categories": {
            "local_present": len(local_categories_in(catalog, cand)),
            "mapped_targets": sorted(target_counts),
            # 該在這一步就報出來，不要等到最終 build 才發現漏映射
            "unmapped_local": dict(unmapped.most_common()),
        },
    }

    if previous is not None:
        summary["delta"] = {
            "images": len(cand.images) - len(previous.images),
            "cls": len(cand.cls) - len(previous.cls),
            "det": len(cand.det) - len(previous.det),
            "images_added": len(cand.images - previous.images),
            "images_removed": len(previous.images - cand.images),
        }
    if step_stats:
        summary["step"] = step_stats
    return summary


def conflict_summary(catalog: Catalog, cand: CandidateSet) -> dict[str, Any]:
    groups = find_conflicts(catalog, cand)
    contradictions = [g for g in groups if g.kind == "contradiction"]
    pairs = Counter()
    for group in groups:
        pairs[" vs ".join(sorted(group.by_source))] += 1
    return {
        "total": len(groups),
        "contradictions": len(contradictions),
        "duplicates": len(groups) - len(contradictions),
        "by_source_pair": dict(pairs.most_common()),
        "sample": [g.to_dict() for g in groups[:5]],
    }


def category_report(catalog: Catalog, cand: CandidateSet) -> dict[str, Any]:
    """design_doc §3：列出仍未映射的 local category，以及沒有任何 local 映射過來的
    target category（強制顯式處理，不悄悄丟棄）。"""
    present = local_categories_in(catalog, cand)
    catalog.ensure_categories(present)
    usage = Counter()
    for ann_id in cand.cls:
        usage[catalog.cls(ann_id).category_id] += 1
    for ann_id in cand.det:
        usage[catalog.det(ann_id).category_id] += 1

    mapped, unmapped = [], []
    for category_id in sorted(present, key=lambda c: catalog.category(c).ref):
        cat = catalog.category(category_id)
        row = {
            "category_id": category_id,
            "scope": cat.scope,
            "local_name": cat.name,
            "supercategory": cat.supercategory,
            "annotations": usage[category_id],
            "target": cand.category_targets.get(category_id),
        }
        (mapped if row["target"] else unmapped).append(row)

    targets = Counter(
        cand.category_targets[c] for c in present if c in cand.category_targets
    )
    declared = set(cand.category_targets.values())
    return {
        "mapped": mapped,
        "unmapped": unmapped,
        "targets": [
            {"name": name, "local_categories": [
                catalog.category(c).ref for c in sorted(present)
                if cand.category_targets.get(c) == name
            ]}
            for name in sorted(targets)
        ],
        "targets_without_source": sorted(declared - set(targets)),
        "fully_mapped": not unmapped,
    }
