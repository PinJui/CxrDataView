#!/usr/bin/env python
"""建立 image_lineage：記錄哪張影像是由哪張影像處理而來。

    python scripts/tools/build_lineage.py \
        --dataset aws_images --parent-version V1 --child-version V2 [--dry-run]

在同一個 original-set 底下，用 file_name 把 parent 與 child 兩個 image batch
配對起來，每一組同名的 (parent, child) 建一條邊。實際的處理過程（縮放、
去雜訊…）由 child 那個 image batch 自己的文件說明，這張表只記關係。

為什麼獨立成一支腳本而不是併進匯入流程（這個理由現在仍然成立）：
血緣描述的是「兩個 image batch 之間」的關係，跟某一次 annotation 匯入的
時間點無關。你可能兩個版本都匯入很久之後才補建、或發現方向搞反要重來，
這些操作沒有一個天然對應的匯入動作可以掛。而且匯入是「失敗就整批 rollback」
的單位，配對是「逐張找不找得到對應」的單位，兩者的重跑粒度不一樣。

跟舊版腳本的差別：
  * 走 SQLAlchemy 與專案的 models，連線設定從 .env 讀
  * 檔名對不上時列出實際的例子，而不是只給一個數字——名稱規則不同
    （例如前處理批次加了前綴）時，看得到才知道要改用 --strip-prefix
  * --child-sub / --parent-sub：配對前先用正則改寫檔名。前處理批次改名的
    方式五花八門（加前綴、插中綴、換副檔名），只支援「剝前綴」蓋不住
  * 擋下反向邊：已經有 B→A 就不讓你再建 A→B，血緣必須是有向無環的
"""

from __future__ import annotations

import argparse
import re
from typing import Optional

from sqlalchemy import select

from _common import console, die, summary_table

from cxr_dataset_manager.db import models as m
from cxr_dataset_manager.db.engine import new_session


def _images_of(db, original_set_id: int, version: str) -> dict[str, int]:
    rows = db.execute(
        select(m.Image.file_name, m.Image.id)
        .join(m.ImageBatch, m.ImageBatch.id == m.Image.image_batch_id)
        .where(m.ImageBatch.original_set_id == original_set_id, m.ImageBatch.version == version)
    ).all()
    return {file_name: image_id for file_name, image_id in rows}


def _normalize(names: dict[str, int], sub: Optional[tuple[str, str]]) -> dict[str, int]:
    if not sub:
        return names
    pattern, replacement = sub
    return {re.sub(pattern, replacement, name): image_id for name, image_id in names.items()}


def build_lineage(
    dataset: str, parent_version: str, child_version: str,
    dry_run: bool = False,
    child_sub: Optional[tuple[str, str]] = None,
    parent_sub: Optional[tuple[str, str]] = None,
) -> dict[str, int]:
    if parent_version == child_version:
        die("parent 與 child 不能是同一個版本")

    db = new_session()
    try:
        original_set = db.execute(
            select(m.OriginalSet).filter_by(name=dataset)
        ).scalar_one_or_none()
        if original_set is None:
            die(f"original-set '{dataset}' 不存在，請先匯入")

        parents = _images_of(db, original_set.id, parent_version)
        children_raw = _images_of(db, original_set.id, child_version)
        if not parents:
            die(f"'{dataset}' 底下找不到 image batch {parent_version} 的任何影像")
        if not children_raw:
            die(f"'{dataset}' 底下找不到 image batch {child_version} 的任何影像")

        # 前處理批次常常改名，配對前先把兩邊化到同一個形式
        children = _normalize(children_raw, child_sub)
        parents = _normalize(parents, parent_sub)

        matched = parents.keys() & children.keys()
        only_parent = sorted(parents.keys() - children.keys())
        only_child = sorted(children.keys() - parents.keys())

        summary_table(
            f"{dataset}: {parent_version} → {child_version}",
            [
                (f"parent（{parent_version}）影像", len(parents)),
                (f"child（{child_version}）影像", len(children)),
                ("可配對", len(matched)),
                ("只在 parent", len(only_parent)),
                ("只在 child", len(only_child)),
            ],
        )
        if only_parent or only_child:
            # 名稱規則不同時，光看數字不知道該怎麼修，要看到實際例子
            if only_parent:
                console.print(f"  [dim]只在 parent 的例子：{', '.join(only_parent[:3])}[/]")
            if only_child:
                console.print(f"  [dim]只在 child 的例子：{', '.join(only_child[:3])}[/]")
            if not matched:
                console.print(
                    "  [yellow]⚠[/] 一張都配不上。兩批的檔名規則不同——"
                    "看上面的例子，用 --child-sub '<正則>' '<取代>' 把 child 化成 parent 的形式，"
                    "例如 --child-sub '_PP_' '_'"
                )

        if dry_run:
            console.print("[yellow]--dry-run：沒有寫入任何東西[/]")
            return {"matched": len(matched), "created": 0}

        existing = {
            (p, c)
            for p, c in db.execute(
                select(m.ImageLineage.parent_image_id, m.ImageLineage.child_image_id)
            ).all()
        }
        created = skipped = 0
        for name in sorted(matched):
            parent_id, child_id = parents[name], children[name]
            if parent_id == child_id:
                continue
            if (child_id, parent_id) in existing:
                # 血緣是有向的：同一對已經有反方向的邊，多半是參數下反了
                die(
                    f"影像 {name} 已經有 {child_id} → {parent_id} 的血緣邊。"
                    "再建反方向會造成環——請確認 --parent-version / --child-version 有沒有下反。"
                )
            if (parent_id, child_id) in existing:
                skipped += 1
                continue
            db.add(m.ImageLineage(parent_image_id=parent_id, child_image_id=child_id))
            created += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    console.print(
        f"[green]✓[/] 新增 {created:,} 條血緣邊"
        + (f"，{skipped:,} 條已存在（略過）" if skipped else "")
    )
    return {"matched": len(matched), "created": created, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", required=True, help="original-set 名稱")
    parser.add_argument("--parent-version", required=True, help="來源的 image batch 版本")
    parser.add_argument("--child-version", required=True, help="衍生的 image batch 版本")
    parser.add_argument("--child-sub", nargs=2, metavar=("PATTERN", "REPLACE"), default=None,
                        help="配對前用正則改寫 child 檔名，例如 --child-sub '_PP_' '_'")
    parser.add_argument("--parent-sub", nargs=2, metavar=("PATTERN", "REPLACE"), default=None,
                        help="同上，但改寫 parent 檔名")
    parser.add_argument("--dry-run", action="store_true", help="只顯示配對統計，不寫入")
    args = parser.parse_args()
    build_lineage(
        args.dataset, args.parent_version, args.child_version, args.dry_run,
        tuple(args.child_sub) if args.child_sub else None,
        tuple(args.parent_sub) if args.parent_sub else None,
    )


if __name__ == "__main__":
    main()
