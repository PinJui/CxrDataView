"""`cxr explore` —— 終端機裡的探索式 session（Layer 4）。

跟 Notebook 走的是同一個 `ManualSetSession`：這裡只負責把
指令列翻譯成方法呼叫、把結果印漂亮，一行業務邏輯都沒有（design_doc §1 principle 7）。

探索狀態活在這個 process 的記憶體裡，離開就沒了——這跟整套設計一致：
值得留下來的是 `save` 出去的 spec，或 `commit` 產生的版本。
"""

from __future__ import annotations

import cmd
import shlex
import sys
import traceback
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from cxr_dataset_manager.core.schema import BuildSpec
from cxr_dataset_manager.core.types import SpecError
from cxr_dataset_manager.db import crud
from cxr_dataset_manager.db.engine import new_session
from cxr_dataset_manager.session.builder import ManualSetSession

console = Console()


def _table(title: str, columns: list[str], rows: list[list[Any]]) -> Table:
    table = Table(title=title, header_style="bold cyan", title_justify="left")
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*["" if c is None else str(c) for c in row])
    return table


class ExploreShell(cmd.Cmd):
    intro = ""
    doc_header = "指令（打 help <指令> 看細節）"
    ruler = "─"

    def __init__(self, name: str) -> None:
        super().__init__()
        # readline 預設把 @ 和 - 當成斷詞字元，`source aws@V<Tab>` 只會拿 "V"
        # 去比對，`TB-portal` 也會被 - 切斷。batch 名稱本來就含這兩個字元，
        # 所以把分隔符收窄成純空白。
        try:
            import readline

            readline.set_completer_delims(" \t\n")
        except ImportError:  # pragma: no cover - 沒有 readline 的平台
            pass
        self.db = new_session()
        self.session = ManualSetSession(
            self.db, name=name, on_warning=lambda msg: console.print(f"  [yellow]⚠[/]  {msg}")
        )
        self.batches = crud.list_batches(self.db)
        self._batch_index: dict[str, list[str]] = {}
        for b in self.batches:
            key = f"{b['original_set_name']}@{b['version']}"
            self._batch_index.setdefault(key, []).append(b["batch_kind"])
        self._update_prompt()

    # -- 基礎設施 ---------------------------------------------------------

    def _update_prompt(self) -> None:
        counts = self.session.current.counts()
        state = f"{counts['images']}img" if self.session.head else "空"
        plain = f"cxr({self.session.name} {state})> "
        # \001/\002 是 readline 用來標記「不佔寬度」的字元；沒有 tty 時
        # readline 不介入，這些標記會直接印出來，所以只在互動模式加上色。
        if self.use_rawinput and sys.stdin.isatty():
            self.prompt = f"\001\033[36m\002{plain.rstrip()}\001\033[0m\002 "
        else:
            self.prompt = plain

    def _report(self) -> None:
        """每個會改變狀態的指令後面都印這一行——探索時最想看的就是它。"""
        counts = self.session.current.counts()
        console.print(
            f"  [bold]{counts['images']}[/] img · [bold]{counts['cls']}[/] cls · "
            f"[bold]{counts['det']}[/] det"
            + (f"   [dim]head={self.session.head}[/]" if self.session.head else "")
        )
        self._update_prompt()

    def onecmd(self, line: str) -> bool:
        try:
            return super().onecmd(line)
        except SpecError as exc:
            console.print(f"  [red]✗[/] {exc}")
        except (KeyError, IndexError, ValueError) as exc:
            console.print(f"  [red]✗[/] {type(exc).__name__}: {exc}")
        except Exception as exc:  # 探索中不該因為打錯字就整個掉出去
            console.print(f"  [red]✗[/] {type(exc).__name__}: {exc}")
            console.print("  [dim]完整堆疊：debug on[/]")
            if getattr(self, "_debug", False):
                console.print(traceback.format_exc())
        return False

    def emptyline(self) -> bool:
        return False

    def default(self, line: str) -> None:
        console.print(f"  [red]✗[/] 不認得的指令 [bold]{line.split()[0]}[/]，打 [cyan]help[/] 看清單")

    def _args(self, arg: str) -> list[str]:
        return shlex.split(arg)

    def _resolve_batch(self, token: str, kind_flag: Optional[str]) -> tuple[str, str, str]:
        """`aws_images@V1` → (original_set, version, kind)。

        同一個 名稱@版本 可能同時有 image 與 annotation batch，
        這時一定要講清楚是哪一個，不猜。
        """
        if "@" not in token:
            raise SpecError(f"來源格式應為 名稱@版本（例如 aws_images@V1），收到 {token!r}")
        name, version = token.rsplit("@", 1)
        kinds = self._batch_index.get(f"{name}@{version}")
        if not kinds:
            raise SpecError(f"找不到 {token}，用 [cyan]batches[/] 看有哪些")
        if kind_flag:
            if kind_flag not in kinds:
                raise SpecError(f"{token} 沒有 {kind_flag} batch（它有：{', '.join(kinds)}）")
            return name, version, kind_flag
        if len(kinds) > 1:
            raise SpecError(
                f"{token} 同時有 {' 和 '.join(kinds)} batch，請加上 --image 或 --annotation 指定"
            )
        return name, version, kinds[0]

    # -- 來源 -------------------------------------------------------------

    def do_source(self, arg: str) -> None:
        """加入一整批來源。

        source <名稱@版本> [--image | --annotation]

        image batch 只帶影像；annotation batch 會連同它標註到的影像一起帶進來。
        同一個 名稱@版本 兩種都有時必須指定。
        """
        args = self._args(arg)
        if not args:
            return console.print("  用法：source <名稱@版本> [--image|--annotation]")
        kind = None
        if "--image" in args:
            kind, args = "image", [a for a in args if a != "--image"]
        elif "--annotation" in args:
            kind, args = "annotation", [a for a in args if a != "--annotation"]
        name, version, kind = self._resolve_batch(args[0], kind)
        if kind == "image":
            self.session.add_source(original_set=name, image_batch=version)
        else:
            self.session.add_source(original_set=name, annotation_batch=version)
        self._report()

    def complete_source(self, text, line, begidx, endidx):
        return [k for k in self._batch_index if k.startswith(text)]

    def do_import(self, arg: str) -> None:
        """從檔名清單匯入（一行一個檔名）。

        import <名稱@版本> <清單檔> [--strict]

        預設對不上的檔名只警告不中斷（探索時先看看匹配狀況）；
        --strict 則任何一筆對不上就整步失敗。
        """
        args = self._args(arg)
        if len(args) < 2:
            return console.print("  用法：import <名稱@版本> <清單檔> [--strict]")
        strict = "--strict" in args
        args = [a for a in args if a != "--strict"]
        name, version, _ = self._resolve_batch(args[0], "image")
        path = Path(args[1]).expanduser()
        if not path.exists():
            raise SpecError(f"找不到清單檔 {path}")
        names = path.read_text().splitlines()
        self.session.import_list(
            original_set=name, image_batch=version, file_names=names,
            on_missing="error" if strict else "warn", source_note=path.name,
        )
        report = self.session.last_import_report()
        if report:
            console.print(
                f"  比對到 [bold]{report.matched_count}[/]/{report.requested}"
                + (f"，[red]{report.missing_count:,} 筆對不上[/]" if report.missing_count else "")
            )
            for missing in report.missing[:10]:
                console.print(f"    [dim]· {missing}[/]")
            if report.missing_count > 10:
                console.print(f"    [dim]… 還有 {report.missing_count - 10:,} 筆[/]")
        self._report()

    complete_import = complete_source

    # -- 縮限 -------------------------------------------------------------

    def do_split(self, arg: str) -> None:
        """決定性切割（同一組 seed 永遠切出同一批）。

        split --mod 4 --keep 0,1,2 --seed my-seed [--key subject_id|image_id|file_name]

        預設用 subject_id 當 key，同一位病患的影像永遠同進同出。
        """
        args = self._args(arg)
        opts = self._kv(args)
        if not {"mod", "keep", "seed"} <= opts.keys():
            return console.print("  用法：split --mod 4 --keep 0,1,2 --seed <seed> [--key subject_id]")
        self.session.split(
            mod=int(opts["mod"]),
            keep_remainder=[int(x) for x in opts["keep"].split(",")],
            seed=opts["seed"],
            key_field=opts.get("key", "subject_id"),
        )
        self._report()

    def do_balance(self, arg: str) -> None:
        """把每個類別的影像數壓到上限以下。

        balance 500 --seed b1              # 每個 target category 最多 500 張
        balance 500 --seed b1 --by local   # 改用映射前的 local category

        多標籤讓「每類剛好 N 張」無法同時成立——一張同時是 pneumonia 與
        effusion 的圖會佔用兩個配額。做法是從最罕見的類別開始配額，罕見的
        先拿滿，共用的影像順便幫常見類別填數。挑選用 hash(seed + image_id)，
        同一組 seed 永遠挑出同一批。
        """
        args = self._args(arg)
        opts = self._kv(args)
        positional = [a for a in args if not a.startswith("-") and a.isdigit()]
        if not positional:
            return console.print('  用法：balance <每類上限> --seed <seed> [--by target|local]')
        if "seed" not in opts:
            return console.print("  [red]✗[/] 需要 --seed：挑哪幾張必須是決定性的")
        self.session.balance(
            max_per_class=int(positional[0]), seed=opts["seed"], by=opts.get("by", "target")
        )
        self._report()
        report = self.session.reports[self.session.head].stats
        console.print(
            _table(
                "類別分布",
                ["類別", "平衡前", "平衡後"],
                [
                    [name, before, report["class_counts_after"].get(name, 0)]
                    for name, before in report["class_counts_before"].items()
                ],
            )
        )

    def do_filter(self, arg: str) -> None:
        """依 metadata 條件篩選。

        filter date_captured >= '2022-01-01'
        filter width >= 512 and original_set in ['aws_images', 'DrLee']
        filter regex(file_name, '^DL_2023')
        filter --annotated                    # 只留有標註的影像
        filter 'Pneumonia' in labels          # 依標註篩選
        filter 'pneumonia' in targets and not ('normal' in targets)
        filter n_annotations >= 2             # 至少兩位標註過

        影像欄位：file_name, original_set, batch_version, width, height,
        area, blake3_hash, date_captured, subject_id

        標註欄位（隨前面的步驟變動）：labels, targets, annotators, n_annotations
        """
        if arg.strip() in ("--annotated", "annotated"):
            self.session.keep_annotated_only()
            return self._report()
        if not arg.strip():
            return console.print(
                "  用法：filter <條件式>，例如 filter width >= 512"
                "\n        filter --annotated   # 只留有標註的影像"
            )
        self.session.filter(criterion="predicate", expression=arg.strip())
        self._report()

    def do_pick(self, arg: str) -> None:
        """用檔名清單在目前結果裡縮限（清單裡的檔名必須已經在集合中）。

        pick <清單檔> [--strict]
        """
        args = self._args(arg)
        if not args:
            return console.print("  用法：pick <清單檔> [--strict]")
        strict = "--strict" in args
        path = Path([a for a in args if a != "--strict"][0]).expanduser()
        if not path.exists():
            raise SpecError(f"找不到清單檔 {path}")
        self.session.filter(
            criterion="explicit_list",
            file_names=path.read_text().splitlines(),
            on_missing="error" if strict else "warn",
            source_note=path.name,
        )
        self._report()

    def do_duplicates(self, arg: str) -> None:
        """列出目前集合裡 blake3 相同的影像，以及每一張各自帶的標註。

        duplicates [幾組]

        去重就是明確丟掉重複影像和它們的標註，所以決定留哪一張之前先看清楚。
        挑好之後用 `dedup --keep <image_id>,<image_id>` 指定。
        """
        limit = int(arg.strip()) if arg.strip().isdigit() else 10
        groups = self.session.find_duplicates()
        if not groups:
            return console.print("  [green]✓[/] 目前沒有內容重複的影像")

        cross = sum(1 for g in groups if g["cross_source"])
        console.print(
            f"  共 [bold]{len(groups)}[/] 組內容重複（blake3 相同），"
            f"其中 [bold]{cross}[/] 組跨來源"
        )
        for group in groups[:limit]:
            console.print(f"  [dim]{group['blake3'][:16]}…[/]")
            for c in group["candidates"]:
                labels = ", ".join(c["labels"]) or "[red]無標註[/]"
                ann = len(c["cls_annotation_ids"]) + len(c["det_annotation_ids"])
                console.print(
                    f"    [bold]#{c['image_id']}[/] {c['ref']}"
                    f"  [dim]{ann} 筆標註 · {labels}"
                    f" · 病患 {c['subject_id'] or '未知'}[/]"
                )
        if len(groups) > limit:
            console.print(f"  [dim]… 還有 {len(groups) - limit} 組（duplicates <n> 看更多）[/]")
        console.print("  [dim]挑好之後：dedup --keep <image_id>,<image_id>,…[/]")

    def do_dedup(self, arg: str) -> None:
        """依 blake3 去重——丟掉重複影像連同它們的標註。

        dedup                                  # 有標註的優先，其次 image_id 最小
        dedup TB-portal,DrLee,aws_images       # 加上來源優先權
        dedup --keep 5,712,918                 # 人工指定每組要留哪一張
        dedup TB-portal,aws_images --keep 5    # 兩者可以並用

        先用 `duplicates` 看每一組帶了什麼再決定。沒被 --keep 指定的組，
        優先留有標註的那張（blake3 相同就是同一張照片，留沒標註的等於丟掉標籤）。
        """
        args = self._args(arg)
        keep: list[int] = []
        if "--keep" in args:
            idx = args.index("--keep")
            if idx + 1 >= len(args):
                return console.print("  用法：dedup [來源優先權] --keep <image_id>,<image_id>")
            keep = [int(x) for x in args[idx + 1].split(",") if x.strip()]
            args = args[:idx] + args[idx + 2:]
        priority = [p.strip() for p in " ".join(args).split(",") if p.strip()]
        self.session.dedup(source_priority=priority, keep=keep)
        self._report()

    def complete_dedup(self, text, line, begidx, endidx):
        names = {b["original_set_name"] for b in self.batches}
        return sorted(n for n in names if n.startswith(text))

    # -- 集合運算 ---------------------------------------------------------

    def do_union(self, arg: str) -> None:
        """把還沒合併的分支接起來（每次 source/import 都會開一條新分支）。"""
        self.session.union()
        self._report()

    def do_intersect(self, arg: str) -> None:
        """取所有未合併分支的交集。"""
        self.session.intersect()
        self._report()

    def do_except(self, arg: str) -> None:
        """第一條分支扣掉其餘分支。"""
        self.session.exclude()
        self._report()

    # -- 類別 -------------------------------------------------------------

    def do_categories(self, arg: str) -> None:
        """看類別映射的現況：哪些已映射、哪些還沒。"""
        report = self.session.preview_categories()
        if report["targets"]:
            console.print(
                _table("已映射", ["target", "來自哪些 local category"],
                       [[t["name"], ", ".join(t["local_categories"])] for t in report["targets"]])
            )
        if report["unmapped"]:
            console.print(
                _table("[red]尚未映射[/]", ["scope", "local category", "標註數"],
                       [[c["scope"], c["local_name"], c["annotations"]] for c in report["unmapped"]])
            )
            console.print("  [dim]正式 build 會拒絕未映射的類別。用 map 或 merge-identical 處理。[/]")
        elif report["targets"]:
            console.print("  [green]✓[/] 所有 local category 都已映射")
        else:
            console.print("  [dim]目前沒有標註[/]")

    def do_map(self, arg: str) -> None:
        """把某個 annotation batch 的類別映射到 target。

        map aws_images@V1 Pneumonia=pneumonia Normal=normal

        scope 一定要寫清楚是哪個 annotation batch——類別命名空間是綁在
        batch 底下的，同名不同義是常態。
        """
        args = self._args(arg)
        if len(args) < 2:
            return console.print("  用法：map <名稱@版本> Local=target [Local2=target2 ...]")
        scope = args[0]
        mapping = {}
        for pair in args[1:]:
            if "=" not in pair:
                raise SpecError(f"映射要寫成 Local=target，收到 {pair!r}")
            local, target = pair.split("=", 1)
            mapping[local.strip()] = target.strip()
        self.session.map_category(scope, mapping)
        self._report()

    def complete_map(self, text, line, begidx, endidx):
        scopes = {c["scope"] for c in self.session.preview_categories()["unmapped"]}
        return sorted(s for s in scopes if s.startswith(text))

    def do_merge_identical(self, arg: str) -> None:
        """把完全同名的 local category 併成同名的 target。

        只合併「一模一樣」的名字——Pneumonia 與 pneumonia 不會被自動合併，
        那種要用 map 顯式指定。
        """
        self.session.merge_identical_category()
        self._report()

    # -- 衝突 -------------------------------------------------------------

    def do_conflicts(self, arg: str) -> None:
        """攤開同一張圖被多個來源標註的情況。

        conflicts [幾筆範例]
        """
        limit = int(arg.strip()) if arg.strip().isdigit() else 5
        summary = self.session.conflict_summary()
        if not summary["total"]:
            return console.print("  [green]✓[/] 目前沒有同一張圖被多個來源標註的情況")
        console.print(
            f"  共 [bold]{summary['total']}[/] 張影像被多個來源標註，其中 "
            f"[red]{summary['contradictions']}[/] 張是真的矛盾"
            f"（各來源給的類別不一樣），{summary['duplicates']} 張只是重複標到同樣的類別"
        )
        console.print(
            _table("來源組合", ["組合", "影像數"],
                   [[k, v] for k, v in summary["by_source_pair"].items()])
        )
        for group in summary["sample"][:limit]:
            colour = "red" if group["kind"] == "contradiction" else "yellow"
            console.print(f"  [{colour}]{group['kind']}[/] {group['image']}")
            for src, info in group["sources"].items():
                ids = ", ".join(f"#{a}" for a in info["annotation_ids"])
                console.print(
                    f"    [bold]{ids}[/] [dim]{src}[/] → {', '.join(info['targets'])}"
                    f"  [dim]{', '.join(info['annotators'])} · score {info['max_score']}[/]"
                )
        console.print(
            "  [dim]要自己挑：resolve manual <annotation_id> [\"原因\"][/]"
        )

    def do_resolve(self, arg: str) -> None:
        """裁決衝突。規則涵蓋不到的會留著，不會靜默處理。

        resolve annotator radiologist_senior,radiologist_junior
        resolve version V3,V2,V1
        resolve score
        resolve manual 1887 "主治醫師的判讀才對"

        manual 是人工指定：`conflicts` 會印出每一筆的 annotation id，挑一個
        填進來，那一筆留下、同一張圖上其他來源的標註剔除。
        """
        args = self._args(arg)
        if not args:
            return console.print(
                "  用法：resolve annotator <a,b,c> | version <V3,V1> | score"
                " | manual <影像> <annotation_id> [\"原因\"]"
            )
        mode = args[0]

        if mode == "manual":
            if len(args) < 2 or not args[1].isdigit():
                return console.print(
                    '  用法：resolve manual <annotation_id> ["原因"]'
                    "\n  [dim]conflicts 會印出每一筆的 id，挑一個填進來[/]"
                )
            self.session.resolve_conflicts_by_manual_setting(
                designated_annotation_id=int(args[1]),
                reason=args[2] if len(args) > 2 else "",
            )
            self._report()
            remaining = len(self.session.find_conflicts())
            console.print(
                f"  [yellow]還有 {remaining} 張影像的衝突未裁決[/]" if remaining
                else "  [green]✓[/] 衝突都解決了"
            )
            return

        order = [x.strip() for x in args[1].split(",")] if len(args) > 1 else []
        if mode == "annotator":
            self.session.resolve_conflicts_by_annotator_precedence(order)
        elif mode == "version":
            self.session.resolve_conflicts_by_annotation_version(order)
        elif mode == "score":
            self.session.resolve_conflicts_by_score()
        else:
            raise SpecError(
                f"不認得的裁決方式 {mode!r}（可用：annotator / version / score / manual）"
            )
        remaining = len(self.session.find_conflicts())
        self._report()
        if remaining:
            console.print(f"  [yellow]還有 {remaining} 張影像的衝突沒被這條規則裁決[/]")
        else:
            console.print("  [green]✓[/] 衝突都解決了")

    def complete_resolve(self, text, line, begidx, endidx):
        return [m for m in ("annotator", "version", "score", "manual") if m.startswith(text)]

    def _override(self, include: bool, arg: str) -> None:
        verb = "include" if include else "exclude"
        args = self._args(arg)
        if len(args) < 2 or args[0] not in ("image", "cls", "det"):
            return console.print(
                f'  用法：{verb} <image|cls|det> <id> ["原因"]\n'
                f'        {verb} image 315 "拍攝品質不佳"\n'
                f'        {verb} cls 1887 "這筆標註是錯的"\n'
                "  [dim]id 從 images / duplicates / conflicts 的輸出取得[/]"
            )
        if not args[1].isdigit():
            return console.print(f"  [red]✗[/] id 要是數字，收到 {args[1]!r}")
        self.session.override_one(
            include=include, kind=args[0], target_id=int(args[1]),
            reason=args[2] if len(args) > 2 else "",
        )
        self._report()

    def complete_exclude(self, text, line, begidx, endidx):
        return [k for k in ("image", "cls", "det") if k.startswith(text)]

    complete_include = complete_exclude

    def do_exclude(self, arg: str) -> None:
        """排除一張影像或一筆標註。

        exclude image 315 "拍攝品質不佳"
        exclude cls 1887 "這筆標註是錯的"
        exclude det 42 "框錯位置"

        一律用 id——檔名不是全域唯一的，用路徑字串定位遲早會指錯。
        id 從 `images`、`duplicates`、`conflicts` 的輸出取得。
        理由會寫進 spec，之後 `cxr why` 查得到。
        """
        self._override(False, arg)

    def do_include(self, arg: str) -> None:
        """納入一張影像或一筆標註——把前面步驟排除掉的加回來。

        include image 1 "罕見表現，訓練集一定要有"
        include cls 291 "主治醫師的判讀"

        納入影像時會連同它既有的標註一起帶進來（跟 source --image 一致）。
        """
        self._override(True, arg)

    # -- 觀察 -------------------------------------------------------------

    def do_preview(self, arg: str) -> None:
        """目前狀態的完整統計摘要。"""
        if not self.session.head:
            return console.print("  [dim]還沒有任何步驟[/]")
        p = self.session.preview()
        counts, delta = p["counts"], p.get("delta")
        head = (
            f"影像 [bold]{counts['images']}[/]  cls [bold]{counts['cls']}[/]  "
            f"det [bold]{counts['det']}[/]"
        )
        if delta:
            head += (
                f"\n上一步變化：影像 {delta['images']:+d}"
                f"（進 {delta['images_added']} / 出 {delta['images_removed']}）"
            )
        head += (
            f"\n病患 {p['subjects']['distinct']} 人"
            f"（{p['subjects']['images_without_subject']} 張無病患資訊）"
            f"　未標註影像 {p['annotation_coverage']['images_without_annotation']}"
        )
        console.print(Panel(head, title=f"[cyan]{self.session.name}[/]"))
        if p["by_source"]:
            console.print(_table("來源分布", ["來源", "影像數"],
                                 [[k, v] for k, v in p["by_source"].items()]))
        dist = p["by_target_category"] or p["by_local_category"]
        if dist:
            console.print(_table("類別分布", ["類別", "標註數"],
                                 [[k, v] for k, v in dist.items()]))
        if p["categories"]["unmapped_local"]:
            console.print(
                f"  [yellow]⚠[/] 還有 {len(p['categories']['unmapped_local'])} 個 "
                "local category 沒映射（打 categories 看細節）"
            )

    def do_steps(self, arg: str) -> None:
        """列出目前累積的步驟——這就是會被編譯成 spec 的那串。"""
        rows = self.session.describe()
        if not rows:
            return console.print("  [dim]還沒有任何步驟[/]")
        console.print(
            _table(
                "Pipeline",
                ["#", "step_id", "op", "inputs", "影像", "cls", "det", "分支", ""],
                [
                    [
                        i, r["step_id"], r["op"], ", ".join(r["inputs"]) or "—",
                        r["counts"]["images"], r["counts"]["cls"], r["counts"]["det"],
                        "[yellow]末端[/]" if r["open"] else "",
                        "[cyan]← head[/]" if r["head"] else "",
                    ]
                    for i, r in enumerate(rows)
                ],
            )
        )
        open_ends = [r["step_id"] for r in rows if r["open"]]
        if len(open_ends) > 1:
            console.print(
                f"  [dim]{len(open_ends)} 條分支還沒合併（{', '.join(open_ends)}）——"
                "用 union 接起來，或 commit 時會自動補一個 union step[/]"
            )
        console.print("  [dim]checkout <step_id> 可以把 head 移到別條分支上[/]")

    def do_images(self, arg: str) -> None:
        """抽樣列出目前集合裡的影像。

        images [幾張]
        """
        limit = int(arg.strip()) if arg.strip().isdigit() else 10
        cand, catalog = self.session.current, self.session.catalog
        ids = sorted(cand.images)[:limit]
        if not ids:
            return console.print("  [dim]目前沒有影像[/]")
        rows = []
        for image_id in ids:
            meta = catalog.image(image_id)
            targets = sorted({
                cand.category_targets[catalog.cls(a).category_id]
                for a in cand.cls
                if catalog.cls(a).image_id == image_id
                and catalog.cls(a).category_id in cand.category_targets
            })
            rows.append(
                [f"#{image_id}", meta.ref, meta.subject_id or "—", ", ".join(targets) or "—"]
            )
        console.print(
            _table(f"影像（前 {len(ids)} / {len(cand.images)} 張）",
                   ["id", "影像", "病患", "類別"], rows)
        )
        console.print('  [dim]exclude image <id> ["原因"] 可以排除其中一張[/]')

    def do_batches(self, arg: str) -> None:
        """列出可以當來源的所有 batch。"""
        console.print(
            _table("Batches", ["spec 寫法", "種類", "數量", "類別數"],
                   [[f"{b['original_set_name']}@{b['version']}", b["batch_kind"],
                     b["item_count"], b["categories"] or ""] for b in self.batches])
        )

    # -- 試錯 -------------------------------------------------------------

    def do_checkpoint(self, arg: str) -> None:
        """記下目前位置，之後可以 rollback 回來。

        checkpoint after_dedup
        """
        label = arg.strip()
        if not label:
            return console.print("  用法：checkpoint <標記名稱>")
        self.session.checkpoint(label)
        console.print(f"  [green]✓[/] 記下 [bold]{label}[/]（第 {len(self.session.steps)} 步）")

    def do_rollback(self, arg: str) -> None:
        """退回某個 checkpoint，之後的步驟全部丟掉。

        rollback after_dedup
        """
        label = arg.strip()
        if not label:
            return console.print(
                f"  用法：rollback <標記>（現有：{', '.join(self.session.checkpoints) or '無'}）"
            )
        self.session.rollback(label)
        console.print(f"  [green]✓[/] 回到 [bold]{label}[/]")
        self._report()

    def complete_rollback(self, text, line, begidx, endidx):
        return [c for c in self.session.checkpoints if c.startswith(text)]

    def do_checkout(self, arg: str) -> None:
        """切換到某個步驟，接下來的操作都套在它上面。

        checkout source_2

        每次 source / import 都會開一條新分支並成為目前位置；用這個指令
        可以回到先前的分支繼續加工。`steps` 會標出目前在哪一步。
        """
        step_id = arg.strip()
        if not step_id:
            return console.print(
                f"  用法：checkout <step_id>（現有：{', '.join(s.id for s in self.session.steps) or '無'}）"
            )
        self.session.checkout(step_id)
        console.print(f"  [green]✓[/] 目前位置：[bold]{step_id}[/]")
        self._report()

    def complete_checkout(self, text, line, begidx, endidx):
        return [s.id for s in self.session.steps if s.id.startswith(text)]

    def do_undo(self, arg: str) -> None:
        """復原目前所在的那一步，位置退回它的 input。

        跟 rollback 的差別：undo 一次退一步、不需要事先做標記；
        rollback 是一次退回某個 checkpoint，中間幾步一起丟掉。
        """
        if not self.session.steps or self.session.head is None:
            return console.print("  [dim]沒有可以復原的步驟[/]")
        dropped = self.session.head
        self.session.undo()
        console.print(f"  [green]✓[/] 已復原 [bold]{dropped}[/]")
        self._report()

    # -- 產出 -------------------------------------------------------------

    def do_spec(self, arg: str) -> None:
        """印出目前會編譯出來的 spec。"""
        if not self.session.steps:
            return console.print("  [dim]還沒有任何步驟[/]")
        spec = self.session.compile(strict_conflicts=False)
        console.print(Syntax(spec.to_yaml(), "yaml", theme="ansi_dark"))

    def do_save(self, arg: str) -> None:
        """把 spec 存成 YAML 檔（之後可以用 cxr build 跑，或 load 回來繼續）。

        save pneumonia_v5.yaml
        """
        path = arg.strip()
        if not path:
            return console.print("  用法：save <檔名.yaml>")
        spec = self.session.compile()
        Path(path).expanduser().write_text(spec.to_yaml())
        console.print(f"  [green]✓[/] 已存到 [bold]{path}[/]  sha256 {spec.sha256()[:16]}…")

    def do_load(self, arg: str) -> None:
        """載入一份既有 spec，接著往下探索。

        load pneumonia_v5.yaml
        """
        path = Path(arg.strip()).expanduser()
        if not path.exists():
            raise SpecError(f"找不到 {path}")
        self.session.replay(BuildSpec.from_yaml(path.read_text()))
        console.print(f"  [green]✓[/] 已載入 {len(self.session.steps)} 個步驟")
        self._report()

    def do_commit(self, arg: str) -> None:
        """產出正式的 manual-set 版本。

        commit -m pneumonia -v V5 [--dry-run]

        --dry-run 會完整跑一遍但完全不寫資料庫。
        正式 commit 會記下建立者——沒設 CXR_AUTHOR_NAME / CXR_AUTHOR_EMAIL 就當場問。
        """
        from cxr_dataset_manager.cli.main import _resolve_author

        args = self._args(arg)
        opts = self._kv(args)
        dry = "--dry-run" in args
        name = opts.get("m") or opts.get("name") or self.session.name
        version = opts.get("v") or opts.get("version") or "V1"
        if not self.session.steps:
            return console.print("  [red]✗[/] 還沒有任何步驟")

        author = None if dry else _resolve_author(opts.get("author-name"), opts.get("author-email"))
        result = self.session.commit(
            version=version, manual_set_name=name, dry_run=dry, author=author
        )
        counts = result.counts
        body = (
            f"影像 [bold]{counts['images']}[/]  cls [bold]{counts['cls']}[/]  "
            f"det [bold]{counts['det']}[/]"
        )
        if dry:
            console.print(Panel(body, title="[yellow]試跑完成[/]（資料庫沒有任何寫入）"))
        else:
            body += f"\ntarget category: {', '.join(result.target_categories) or '—'}"
            console.print(Panel(body, title=f"[green]✓[/] {name}@{version}"))
            console.print(f"  [dim]建立者 {author}[/]")
            console.print(f"  [dim]cxr show {name}@{version}　cxr export {name}@{version} -f zip[/]")

    # -- 雜項 -------------------------------------------------------------

    def do_debug(self, arg: str) -> None:
        """開關完整錯誤堆疊：debug on / debug off"""
        self._debug = arg.strip() == "on"
        console.print(f"  debug = {'on' if self._debug else 'off'}")

    def do_quit(self, arg: str) -> bool:
        """離開（探索狀態不會被保留）。"""
        if self.session.steps:
            console.print(
                f"  [dim]{len(self.session.steps)} 個步驟不會被保留。"
                "想留下來的話用 save <檔名.yaml> 或 commit。[/]"
            )
        return True

    do_exit = do_quit
    do_EOF = do_quit

    @staticmethod
    def _kv(args: list[str]) -> dict[str, str]:
        """把 --key value 拆成 dict。"""
        opts: dict[str, str] = {}
        i = 0
        while i < len(args):
            if args[i].startswith("-"):
                key = args[i].lstrip("-")
                if i + 1 < len(args) and not args[i + 1].startswith("-"):
                    opts[key] = args[i + 1]
                    i += 1
                else:
                    opts[key] = "true"
            i += 1
        return opts


BANNER = """[bold cyan]cxr explore[/] —— 互動式資料集建構

  [bold]來源[/]    source aws_images@V1 --annotation   import DrLee@V1 list.txt
  [bold]縮限[/]    split --mod 4 --keep 0,1,2 --seed s1    filter 'x' in targets    balance 500 --seed b
            pick list.txt
  [bold]整理[/]    union   duplicates   dedup --keep 5,712   merge_identical   map aws_images@V1 A=a
  [bold]人工[/]    include / exclude <image|cls|det> <id> ["原因"]
  [bold]檢查[/]    preview   steps   categories   conflicts   images   batches
  [bold]試錯[/]    checkpoint <名稱>   rollback <名稱>   undo   checkout <step_id>
  [bold]產出[/]    spec   save x.yaml   commit -m 名字 -v V1 [--dry-run]

  探索狀態只在記憶體裡，離開就沒了——[dim]要留下來請 save 或 commit[/]
  [dim]Tab 補全 · help <指令> 看細節 · quit 離開[/]"""


def run(name: str) -> None:
    shell = ExploreShell(name)
    console.print(Panel(BANNER, border_style="cyan"))
    while True:
        try:
            shell.cmdloop(intro="")
            break
        except KeyboardInterrupt:
            # Ctrl-C 只取消這一行，不要把整個 session 丟掉
            console.print("\n  [dim]^C（要離開請打 quit）[/]")
