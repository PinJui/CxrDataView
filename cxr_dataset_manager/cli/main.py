"""cxr —— 終端機介面（Layer 4）。

只是皮：每個命令都是「呼叫 core/session 的函式 + 把結果印漂亮」，
不含任何業務邏輯（design_doc §1 principle 7）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from cxr_dataset_manager.core import export as export_mod
from cxr_dataset_manager.core.engine import build as run_build
from cxr_dataset_manager.core.schema import BuildSpec
from cxr_dataset_manager.core.types import SpecError
from cxr_dataset_manager.db import crud
from cxr_dataset_manager.db.engine import apply_schema, get_engine, new_session
from cxr_dataset_manager.settings import settings

app = typer.Typer(
    help="CXR dataset 管理工具：把散落各處的 original-set 組成可重現的 manual-set。",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="資料庫與 mock 資料", no_args_is_help=True)
ls_app = typer.Typer(help="瀏覽目錄", no_args_is_help=True)
lists_app = typer.Typer(help="外部檔名清單", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(ls_app, name="ls")
app.add_typer(lists_app, name="lists")

console = Console()


def _die(message: str) -> None:
    console.print(f"[bold red]✗[/] {message}")
    raise typer.Exit(1)


def _resolve_author(name: Optional[str], email: Optional[str]) -> "Author":
    """誰建的這份資料集：指令旗標 → .env → 互動詢問。

    非互動情境（腳本、CI）問不到就直接失敗並說明怎麼設，
    比留下一份不知道誰建的資料集好。
    """
    from cxr_dataset_manager.core.engine import Author

    name = name or settings.author_name
    email = email or settings.author_email
    if not name or not email:
        if not sys.stdin.isatty():
            _die(
                "不知道是誰要建這份資料集。用 --author-name / --author-email 指定，"
                "或在 .env 裡設 CXR_AUTHOR_NAME 與 CXR_AUTHOR_EMAIL。"
            )
        console.print("[dim]這份資料集會記下建立者（設 CXR_AUTHOR_NAME / CXR_AUTHOR_EMAIL 可免問）[/]")
        name = name or typer.prompt("你的名字")
        email = email or typer.prompt("你的 email")
    try:
        return Author(name=name, email=email)
    except SpecError as exc:
        _die(str(exc))
        raise


def _resolve(db, ref: str) -> int:
    """`name@version` → manual_set_version_id，找不到就結束並說清楚。

    六個查詢指令都要做同一件事，抽出來才不會有的擋有的不擋。
    """
    try:
        name, version = crud.parse_ref(ref)
    except SpecError as exc:
        _die(str(exc))
        raise
    version_id = crud.resolve_version(db, name, version)
    if version_id is None:
        _die(f"找不到 {ref}（用 [cyan]cxr ls manual-sets[/] 看有哪些）")
    return version_id


def _table(title: str, columns: list[str], rows: list[list]) -> Table:
    table = Table(title=title, header_style="bold cyan", title_justify="left")
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*["" if c is None else str(c) for c in row])
    return table


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------


@db_app.command("init")
def db_init(drop: bool = typer.Option(False, "--drop", help="先清空 public schema 再建")):
    """套用 db/*.sql 建立 schema。"""
    apply_schema(get_engine(), drop_first=drop)
    console.print(f"[green]✓[/] schema 已套用到 {settings.database_url}")


@db_app.command("seed")
def db_seed(
    with_images: bool = typer.Option(True, help="同時把合成影像上傳到 MinIO"),
):
    """產生 mock 資料（刻意帶重複、衝突、命名不一致）。"""
    from cxr_dataset_manager.seed import seed_demo
    from cxr_dataset_manager.storage import get_store

    store = None
    if with_images:
        store = get_store()
        if not store.alive():
            _die(f"連不上物件儲存 {settings.s3_endpoint_url}（用 --no-with-images 可只灌 metadata）")
        store.ensure_buckets()

    counts = seed_demo(new_session(), store)
    console.print(_table("Mock 資料", ["表", "筆數"], [[k, v] for k, v in counts.items()]))


@db_app.command("reset")
def db_reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="不要問，直接做"),
):
    """清空並重建 schema，然後重灌 mock 資料。"""
    if not yes and not typer.confirm(f"這會清空 {settings.database_url} 的所有資料，確定嗎？"):
        raise typer.Abort()
    apply_schema(get_engine(), drop_first=True)
    db_seed(with_images=True)


@db_app.command("status")
def db_status():
    """檢查 Postgres / MinIO 通不通，順便印目前的資料量。"""
    from cxr_dataset_manager.storage import get_store

    rows = []
    try:
        db = new_session()
        sets = crud.list_original_sets(db)
        rows.append(["postgres", settings.database_url, "[green]ok[/]"])
        rows.append(["  original-sets", str(len(sets)), str(sum(s["images"] for s in sets)) + " images"])
        rows.append(["  manual-sets", str(len(crud.list_manual_sets(db))), ""])
    except Exception as exc:
        rows.append(["postgres", settings.database_url, f"[red]{exc}[/]"])
    store = get_store()
    rows.append(
        ["minio", settings.s3_endpoint_url or "-", "[green]ok[/]" if store.alive() else "[red]連不上[/]"]
    )
    console.print(_table("服務狀態", ["元件", "位置", "狀態"], rows))


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


@ls_app.command("sets")
def ls_sets():
    """列出所有 original-set。"""
    rows = crud.list_original_sets(new_session())
    console.print(
        _table(
            "Original sets",
            ["名稱", "影像批次", "標註批次", "影像數"],
            [[r["name"], r["image_batches"], r["annotation_batches"], r["images"]] for r in rows],
        )
    )


@ls_app.command("batches")
def ls_batches(original_set: Optional[str] = typer.Argument(None)):
    """列出 image/annotation batch（spec 的 source 就是用這裡的 名稱@版本）。"""
    rows = crud.list_batches(new_session(), original_set)
    console.print(
        _table(
            "Batches",
            ["original_set", "種類", "版本", "spec 寫法", "數量", "類別數"],
            [
                [
                    r["original_set_name"], r["batch_kind"], r["version"],
                    f"{r['original_set_name']}@{r['version']}", r["item_count"], r["categories"] or "",
                ]
                for r in rows
            ],
        )
    )


@ls_app.command("categories")
def ls_categories():
    """列出每個 annotation_batch 的 local category 命名空間。"""
    rows = crud.list_categories(new_session())
    console.print(
        _table(
            "Local categories",
            ["scope", "名稱", "supercategory", "id"],
            [
                [f"{r['original_set']}@{r['version']}", r["name"], r["supercategory"], r["id"]]
                for r in rows
            ],
        )
    )


@ls_app.command("annotators")
def ls_annotators():
    """列出標註者與各自的標註量。"""
    rows = crud.list_annotators(new_session())
    console.print(
        _table("Annotators", ["名稱", "cls", "det"], [[r["name"], r["cls"], r["det"]] for r in rows])
    )


@ls_app.command("manual-sets")
def ls_manual_sets():
    """列出所有 manual-set 及其版本。"""
    rows = crud.list_manual_sets(new_session())
    table_rows = []
    for entry in rows:
        for v in entry["versions"] or [{}]:
            table_rows.append(
                [
                    entry["name"], v.get("version", "—"), v.get("images", ""),
                    v.get("cls", ""), v.get("det", ""), v.get("targets", ""),
                    str(v.get("created_at", ""))[:19],
                ]
            )
    console.print(
        _table(
            "Manual sets", ["名稱", "版本", "影像", "cls", "det", "target 類別", "建立時間"], table_rows
        )
    )


@ls_app.command("history")
def ls_history(limit: int = 20):
    """列出建構歷史：每個版本 + 產生它的 spec。

    探索過程不落庫，所以這裡看到的就是全部——一個版本一份 spec。
    """
    rows = crud.build_history(new_session(), limit)
    console.print(
        _table(
            "建構歷史",
            ["manual-set", "版本", "影像", "步驟", "建立者", "建立時間"],
            [
                [
                    r["manual_set"], r["version"], r["images"], r["steps"],
                    r["created_by_name"], str(r["created_at"])[:19],
                ]
                for r in rows
            ],
        )
    )

# ---------------------------------------------------------------------------
# 外部檔名清單
# ---------------------------------------------------------------------------


@lists_app.command("add")
def lists_add(
    file: Path = typer.Argument(..., help="一行一個檔名的文字檔"),
    note: Optional[str] = typer.Option(None, "--note", "-n", help="給人看的說明"),
):
    """把檔名清單存進資料庫，回傳它的 sha256。

    spec 裡用 file_names_ref 引用這個 sha256，就不必把上萬個檔名內嵌進去。
    內容定址：同一份清單存幾次都只有一列。
    """
    if not file.exists():
        _die(f"找不到 {file}")
    names = file.read_text().splitlines()
    digest, created = crud.register_import_list(
        new_session(), names, note or file.name
    )
    count = len(crud.normalize_file_names(names))
    console.print(
        f"[green]✓[/] {'已存入' if created else '已存在（內容相同）'} "
        f"{count:,} 筆\n  sha256 [bold]{digest}[/]"
    )
    console.print(
        "\n  spec 裡這樣引用：\n"
        f"[dim]    file_names_ref:\n"
        f"      sha256: {digest}\n"
        f"      source: {note or file.name}[/]"
    )


@lists_app.command("ls")
def lists_ls(limit: int = 30):
    """列出已存入的清單。"""
    rows = crud.list_import_lists(new_session(), limit)
    console.print(
        _table(
            "檔名清單",
            ["sha256", "筆數", "說明", "建立時間"],
            [
                [r["sha256"][:16] + "…", f"{r['n']:,}", r["source_note"] or "",
                 str(r["created_at"])[:19]]
                for r in rows
            ],
        )
    )


@lists_app.command("show")
def lists_show(
    sha256: str = typer.Argument(..., help="完整或前綴皆可"),
    limit: int = typer.Option(20, "--limit", "-n", help="最多印幾筆，0 表示全部"),
):
    """印出某份清單的內容。"""
    db = new_session()
    names = crud.get_import_list(db, sha256)
    if names is None:
        matches = [r for r in crud.list_import_lists(db, 500)
                   if r["sha256"].startswith(sha256)]
        if len(matches) != 1:
            _die(f"找不到 sha256 開頭為 {sha256} 的清單"
                 if not matches else f"{sha256} 對到 {len(matches)} 份清單，請給更長的前綴")
        names = crud.get_import_list(db, matches[0]["sha256"])
    assert names is not None
    shown = names if limit == 0 else names[:limit]
    for name in shown:
        console.print(f"  {name}")
    if len(shown) < len(names):
        console.print(f"  [dim]… 還有 {len(names) - len(shown):,} 筆（-n 0 印全部）[/]")


# ---------------------------------------------------------------------------
# spec 的驗證與執行
# ---------------------------------------------------------------------------


def _load_spec(path: Path) -> BuildSpec:
    if not path.exists():
        _die(f"找不到 spec 檔 {path}")
    try:
        return BuildSpec.from_yaml(path.read_text())
    except Exception as exc:
        _die(f"spec 格式錯誤：\n{exc}")
        raise


@app.command()
def validate(spec_file: Path = typer.Argument(..., help="spec YAML 檔")):
    """只檢查 spec 語法與 step 依賴，不碰資料庫。"""
    spec = _load_spec(spec_file)
    console.print(f"[green]✓[/] spec 合法，{len(spec.steps)} 個 step，final = [bold]{spec.final}[/]")
    console.print(f"  sha256 = {spec.sha256()}")
    console.print(
        _table(
            "Steps",
            ["#", "step_id", "op", "inputs"],
            [[i, s.id, s.op, ", ".join(s.input_ids()) or "—"] for i, s in enumerate(spec.steps)],
        )
    )


@app.command()
def build(
    spec_file: Path = typer.Argument(..., help="spec YAML 檔"),
    manual_set: str = typer.Option(..., "--manual-set", "-m", help="要寫入的 manual-set 名稱"),
    version: str = typer.Option("V1", "--version", "-v"),
    dry_run: bool = typer.Option(False, "--dry-run", help="跑完但不落庫，只看結果"),
    author_name: Optional[str] = typer.Option(None, "--author-name", help="建立者姓名"),
    author_email: Optional[str] = typer.Option(None, "--author-email", help="建立者 email"),
):
    """執行一份 spec，產出 manual-set 版本。"""
    spec = _load_spec(spec_file)
    db = new_session()
    author = None if dry_run else _resolve_author(author_name, author_email)
    try:
        result = run_build(db, spec, manual_set, version, dry_run=dry_run, author=author)
    except SpecError as exc:
        _die(str(exc))
        raise

    console.print(
        _table(
            "執行過程",
            ["#", "step_id", "op", "影像", "cls", "det"],
            [
                [r.step_index, r.step_id, r.op, r.counts["images"], r.counts["cls"], r.counts["det"]]
                for r in result.execution.reports
            ],
        )
    )
    for warning in result.execution.warnings:
        console.print(f"[yellow]⚠[/]  {warning}")

    if dry_run:
        console.print(
            Panel(
                f"影像 [bold]{result.counts['images']}[/]  "
                f"cls [bold]{result.counts['cls']}[/]  det [bold]{result.counts['det']}[/]\n"
                f"spec sha256 {spec.sha256()[:16]}…",
                title="[yellow]試跑完成[/]（資料庫沒有任何寫入）",
            )
        )
        return
    console.print(
        Panel(
            f"影像 [bold]{result.counts['images']}[/]  "
            f"cls [bold]{result.counts['cls']}[/]  det [bold]{result.counts['det']}[/]\n"
            f"target category: {', '.join(result.target_categories) or '—'}\n"
            f"spec sha256 {spec.sha256()[:16]}…",
            title=f"[green]✓[/] {manual_set}@{version}",
        )
    )


@app.command()
def show(ref: str = typer.Argument(..., help="manual-set@版本，例如 pneumonia_v4@V1")):
    """看一個 manual-set 版本的組成。"""
    db = new_session()
    version_id = _resolve(db, ref)
    summary = crud.version_summary(db, version_id)

    console.print(
        Panel(
            f"影像 [bold]{summary['images']}[/]  cls [bold]{summary['cls']}[/]  "
            f"det [bold]{summary['det']}[/]\n"
            f"病患數 {summary['subjects']['distinct']}"
            f"（{summary['subjects']['images_without_subject']} 張無病患資訊）\n"
            f"建立者 {summary.get('created_by') or '—'}\n"
            f"spec sha256 {(summary['spec_sha256'] or '—')[:16]}…",
            title=f"[cyan]{ref}[/]",
        )
    )
    console.print(
        _table("來源組成", ["來源", "影像數"], [[k, v] for k, v in summary["by_source"].items()])
    )
    console.print(
        _table(
            "Target category",
            ["target", "標註數", "來自哪些 local category"],
            [
                [k, v, ", ".join(summary["category_mappings"].get(k, []))]
                for k, v in summary["by_target_category"].items()
            ],
        )
    )


@app.command()
def spec(ref: str = typer.Argument(..., help="manual-set@版本")):
    """把產生某個版本的 spec 印出來（可直接存檔重跑）。"""
    db = new_session()
    version_id = _resolve(db, ref)
    summary = crud.version_summary(db, version_id)
    if not summary["spec"]:
        _die(f"{ref} 沒有對應的 spec")
    parsed = BuildSpec.model_validate(summary["spec"])
    console.print(Syntax(parsed.to_yaml(), "yaml", theme="ansi_dark"))


@app.command()
def why(
    ref: str = typer.Argument(..., help="manual-set@版本"),
    image: str = typer.Option(..., "--image", "-i", help="檔名，或 original_set/版本/檔名"),
):
    """回答「這張圖是在哪一步、依據什麼規則被選中或排除的」。"""
    db = new_session()
    version_id = _resolve(db, ref)

    result = crud.explain(db, version_id, file_name=image)
    if not result["found"]:
        _die(result["reason"])

    status = "[green]在最終集合裡[/]" if result["in_final_set"] else "[red]不在最終集合裡[/]"
    console.print(Panel(f"{image}\n{status}", title=f"cxr why · {ref}"))

    if not result["trail"]:
        console.print("[yellow]這次 build 沒有任何一步提到這張影像——它從未進入候選集合。[/]")
        return

    colours = {
        "added": "green", "dropped": "red", "remapped": "yellow",
        "overridden": "magenta", "missing": "red",
    }
    for entry in result["trail"]:
        kind = entry["entity_kind"].replace("_annotation", " 標註").replace("image", "影像")
        colour = colours.get(entry["decision"], "white")
        console.print(
            f"  [dim]step {entry['step_index']}[/] [bold]{entry['step_id']}[/] "
            f"([cyan]{entry['op']}[/])  {kind} #{entry['entity_id']} "
            f"→ [{colour}]{entry['decision']}[/]  [dim]{entry['reason']}[/]"
        )
        for key, value in (entry["detail"] or {}).items():
            console.print(f"      [dim]{key}:[/] {value}")


@app.command()
def diff(
    left: str = typer.Argument(..., help="manual-set@版本"),
    right: str = typer.Argument(..., help="manual-set@版本"),
):
    """比較兩個 manual-set 版本。"""
    db = new_session()
    ids = []
    for ref in (left, right):
        ids.append(_resolve(db, ref))

    result = crud.diff_versions(db, ids[0], ids[1])
    console.print(
        _table(
            "影像",
            ["", "數量"],
            [
                [f"{left} 有", result["left"]["images"]],
                [f"{right} 有", result["right"]["images"]],
                ["新增", f"[green]+{result['images']['added']}[/]"],
                ["移除", f"[red]-{result['images']['removed']}[/]"],
                ["兩邊都有", result["images"]["unchanged"]],
            ],
        )
    )
    console.print(
        _table(
            "cls 標註",
            ["", "數量"],
            [
                ["新增", f"[green]+{result['cls_annotations']['added']}[/]"],
                ["移除", f"[red]-{result['cls_annotations']['removed']}[/]"],
                ["標註有變動的影像", result["cls_annotations"]["images_with_changed_annotations"]],
            ],
        )
    )
    if result["category_mapping_changes"]:
        console.print(
            _table(
                "Category 映射變動",
                ["local category", left, right],
                [
                    [k, v["from"] or "—", v["to"] or "—"]
                    for k, v in result["category_mapping_changes"].items()
                ],
            )
        )
    if result["images"]["added_sample"]:
        console.print("[green]新增範例:[/] " + ", ".join(result["images"]["added_sample"][:5]))
    if result["images"]["removed_sample"]:
        console.print("[red]移除範例:[/] " + ", ".join(result["images"]["removed_sample"][:5]))


@app.command("check-leakage")
def check_leakage(
    refs: list[str] = typer.Argument(..., help="兩個以上的 manual-set@版本"),
):
    """檢查數個版本之間有沒有內容重複或共用病患（train/val leakage）。"""
    if len(refs) < 2:
        _die("至少要給兩個版本才能比較")
    db = new_session()
    ids = []
    for ref in refs:
        ids.append(_resolve(db, ref))

    report = crud.duplicate_report(db, ids)
    content = report["identical_content"]["groups"]
    subjects = report["shared_subjects"]["count"]
    style = "red" if (content or subjects) else "green"
    console.print(
        Panel(
            f"完全相同的影像內容（blake3 相同）: [bold]{content}[/] 組\n"
            f"跨版本共用的病患: [bold]{subjects}[/] 人"
            f"（涉及 {report['shared_subjects']['images_involved']} 張影像）",
            title=f"[{style}]Leakage 檢查[/] " + " ↔ ".join(refs),
        )
    )
    for group in report["identical_content"]["sample"][:5]:
        console.print(f"  · {group['blake3_hash'][:12]}… → {', '.join(group['refs'])}")
    for group in report["shared_subjects"]["sample"][:5]:
        console.print(f"  · 病患 {group['subject_id']} 出現在版本 {group['versions']}")


@app.command()
def rm(
    ref: str = typer.Argument(..., help="manual-set@版本，或只給名稱刪掉整個"),
    yes: bool = typer.Option(False, "--yes", "-y", help="不要問，直接刪"),
):
    """刪除一個 manual-set 版本，或整個 manual-set。

    版本本來是不可變的記錄，這是那條規則的例外——build 打錯想重用版本號、
    清掉實驗留下的東西之類。原始的影像與標註完全不受影響，manual-set
    從來就只是「選了哪些」的記錄。

    產生它的 spec 會一起消失，刪掉之後就再也重現不出這份資料集了。
    """
    db = new_session()
    manual_set, _, version = ref.partition("@")
    plan = crud.describe_deletion(db, manual_set, version or None)
    if not plan["found"]:
        _die(f"找不到 {ref}（用 [cyan]cxr ls manual-sets[/] 看有哪些）")

    console.print(
        _table(
            f"即將刪除 [bold]{manual_set}[/]",
            ["版本", "影像", "cls", "det", "target", "溯源步驟", "建立者", "建立時間"],
            [
                [
                    v["version"], v["images"], v["cls"], v["det"], v["targets"],
                    v["steps"], v["created_by"] or "—", str(v["created_at"])[:19],
                ]
                for v in plan["versions"]
            ],
        )
    )
    with_spec = [v for v in plan["versions"] if v["spec_sha256"]]
    if with_spec:
        console.print(
            f"  [yellow]⚠[/] {len(with_spec)} 份 spec 會一起消失——"
            "刪掉之後就再也重現不出這些資料集了。要留存請先執行："
        )
        for v in with_spec:
            console.print(
                f"      [dim]cxr spec {manual_set}@{v['version']} > "
                f"{manual_set}_{v['version']}.yaml[/]"
            )
    if plan["removes_manual_set"]:
        console.print(f"  [dim]這會刪掉 {manual_set} 的所有版本，連同這個名稱本身[/]")

    if not yes:
        if not sys.stdin.isatty():
            _die("非互動模式下不會自動刪除。確定要刪請加 --yes。")
        if not typer.confirm("確定刪除嗎？"):
            console.print("  [dim]已取消[/]")
            raise typer.Abort()

    crud.delete_manual_set(db, manual_set, version or None)
    console.print(
        f"[green]✓[/] 已刪除 {len(plan['versions'])} 個版本"
        + ("（連同 manual-set 本身）" if plan["removes_manual_set"] else "")
    )
    console.print("  [dim]原始的影像與標註不受影響[/]")


@app.command()
def export(
    ref: str = typer.Argument(..., help="manual-set@版本"),
    out: Path = typer.Option(Path("."), "--out", "-o", help="輸出目錄"),
    fmt: str = typer.Option("zip", "--format", "-f", help="zip | coco | csv"),
):
    """匯出成訓練用的格式（COCO json / manifest csv / 打包 zip）。"""
    db = new_session()
    version_id = _resolve(db, ref)

    out.mkdir(parents=True, exist_ok=True)
    stem = ref.replace("@", "_")
    if fmt == "coco":
        path = out / f"{stem}_coco.json"
        path.write_text(json.dumps(export_mod.to_coco(db, version_id), indent=2, default=str))
    elif fmt == "csv":
        path = out / f"{stem}_manifest.csv"
        path.write_text(export_mod.to_manifest_csv(db, version_id))
    else:
        path = out / f"{stem}.zip"
        path.write_bytes(export_mod.to_zip(db, version_id))
    console.print(f"[green]✓[/] 已匯出 {path}  ({path.stat().st_size:,} bytes)")


@app.command()
def explore(
    name: str = typer.Argument("untitled", help="這次探索的名稱，也是 commit 時的預設 manual-set 名"),
):
    """開一個互動式的探索 session（跟 Notebook 同一套 API）。

    探索狀態只活在這個 process 的記憶體裡，離開就沒了——要留下來請在
    裡面用 save 存成 spec 檔，或 commit 產出正式版本。
    """
    from cxr_dataset_manager.cli import repl

    repl.run(name)


def main() -> None:
    """CLI 進入點。

    刻意把例外收成一行訊息——使用者要的是「哪裡寫錯了」，
    不是 SQLAlchemy 的 traceback。除錯時設 CXR_DEBUG=1 可以看完整堆疊。
    """
    import os

    try:
        app()
    except SpecError as exc:
        console.print(f"[bold red]✗[/] {exc}")
        sys.exit(1)
    except Exception as exc:
        if os.environ.get("CXR_DEBUG"):
            raise
        console.print(f"[bold red]✗[/] {type(exc).__name__}: {exc}")
        console.print("[dim]（設 CXR_DEBUG=1 可看完整堆疊）[/]")
        sys.exit(1)


if __name__ == "__main__":
    main()
