"""`cxr explore` — an exploration session in the terminal (Layer 4).

It drives the same `ManualSetSession` a notebook does: this module only turns
command lines into method calls and prints the results nicely, with not a line
of business logic (design_doc §1 principle 7).

Exploration state lives in this process's memory and is gone on exit, as the
whole design intends: what is worth keeping is a spec written with `save`, or
the version a `commit` produces.
"""

from __future__ import annotations

import cmd
import shlex
import sys
import tempfile
import traceback
from pathlib import Path

from rich.panel import Panel
from rich.syntax import Syntax

from cxr_dataset_manager.cli._common import console, resolve_author, table
from cxr_dataset_manager.cli.meta import on_commit
from cxr_dataset_manager.core.types import SpecError
from cxr_dataset_manager.db import crud
from cxr_dataset_manager.db.engine import new_session
from cxr_dataset_manager.session.builder import ManualSetSession


def _preview_panel(name: str, p: dict) -> Panel:
    counts, delta = p["counts"], p.get("delta")
    head = (
        f"Images [bold]{counts['images']}[/]  cls [bold]{counts['cls']}[/]  "
        f"det [bold]{counts['det']}[/]"
    )
    if delta:
        head += (
            f"\nLast step: images {delta['images']:+d}"
            f" (in {delta['images_added']} / out {delta['images_removed']})"
        )
    head += (
        f"\nSubjects {p['subjects']['distinct']}"
        f" ({p['subjects']['images_without_subject']} images without subject information)"
        f"   unannotated images {p['annotation_coverage']['images_without_annotation']}"
    )
    return Panel(head, title=f"[cyan]{name}[/]")


def _print_preview_tables(p: dict) -> None:
    """Where the images came from, and what they are labelled."""
    if p["by_source"]:
        console.print(
            table(
                "Sources",
                ["source", "images"],
                [[k, v] for k, v in p["by_source"].items()],
            )
        )
    dist = p["by_target_category"] or p["by_local_category"]
    if dist:
        console.print(
            table(
                "Categories",
                ["category", "annotations"],
                [[k, v] for k, v in dist.items()],
            )
        )


def _print_preview_distribution(p: dict) -> None:
    """Per-target POS / NEG / UNKNOWN, and the two things that table can hide."""
    counts = p["counts"]
    rows = [
        r
        for r in p["category_distribution"]
        if r["det_all"]
        or r["cls_pos"]
        or r["cls_neg"]
        or r["cls_unknown"] < counts["images"]
    ]
    if not rows:
        return
    console.print(
        table(
            f"Category distribution (cls counted in images, "
            f"{counts['images']} in all; det in boxes)",
            ["target category", "CLS POS", "CLS NEG", "CLS UNKNOWN", "DET POS"],
            [
                [
                    r["target"],
                    r["cls_pos"],
                    r["cls_neg"],
                    r["cls_unknown"],
                    r["det_pos"],
                ]
                for r in rows
            ],
        )
    )
    if any(
        r["cls_pos"] + r["cls_neg"] + r["cls_unknown"] != counts["images"] for r in rows
    ):
        console.print(
            "  [yellow]⚠[/] for some targets the three cls columns do not add "
            "up to the image count — different sources call one image positive "
            "and negative, so the conflicts have not converged yet "
            "(type conflicts; after resolve they agree)"
        )
    unscored = sum(r["det_no_score"] for r in rows)
    if unscored:
        console.print(
            f"  [yellow]⚠[/] {unscored} det boxes have no score and are not counted in DET POS"
        )


class ExploreShell(cmd.Cmd):
    intro = ""
    doc_header = "Commands (type help <command> for details)"
    ruler = "─"

    def __init__(self, name: str) -> None:
        super().__init__()
        # readline treats @ and - as word delimiters by default, so
        # `source aws@V<Tab>` would complete only "V" and `TB-portal` breaks at
        # the hyphen. Batch names contain both, so the delimiters are narrowed
        # to whitespace.
        try:
            import readline

            readline.set_completer_delims(" \t\n")
        except ImportError:  # pragma: no cover - platforms without readline
            pass
        self.db = new_session()
        self.session = ManualSetSession(
            self.db,
            name=name,
            on_warning=lambda msg: console.print(f"  [yellow]⚠[/]  {msg}"),
        )
        self.batches = crud.list_batches(self.db)
        self._batch_index: dict[str, list[str]] = {}
        for b in self.batches:
            key = f"{b['original_set_name']}@{b['version']}"
            self._batch_index.setdefault(key, []).append(b["batch_kind"])
        self._update_prompt()

    # -- plumbing ---------------------------------------------------------

    def preloop(self) -> None:
        """Bind Tab to completion.

        readline has two incompatible configuration syntaxes and which one
        Python talks to depends on the platform: the deployment target (Ubuntu)
        uses GNU readline (`tab: complete`), the development Mac uses libedit
        (`bind ^I rl_complete`). The wrong one raises no error; it silently
        does nothing.

        cmd.Cmd.cmdloop hardcodes the GNU line, so on macOS Tab did nothing at
        all. preloop runs before cmdloop binds, and sends the line the actual
        backend understands; binding both explicitly means the GNU path does not
        depend on cmd.Cmd's internals staying the same.
        """
        try:
            import readline
        except ImportError:  # pragma: no cover - platforms without readline
            return
        # readline.backend is the official way to tell from 3.13 on; 3.12 only has __doc__.
        backend = getattr(readline, "backend", None)
        if backend is None:
            backend = (
                "editline" if "libedit" in (readline.__doc__ or "") else "readline"
            )
        if backend == "editline":
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")

    def _update_prompt(self) -> None:
        counts = self.session.current.counts()
        state = f"{counts['images']}img" if self.session.head else "[NO IMGS NOW]"
        plain = f"cxr({self.session.name} {state})> "
        # \001/\002 tell readline "this takes no width"; without a tty readline
        # is not involved and would print them, so colour only when interactive.
        if self.use_rawinput and sys.stdin.isatty():
            self.prompt = f"\001\033[36m\002{plain.rstrip()}\001\033[0m\002 "
        else:
            self.prompt = plain

    def _report(self) -> None:
        """Printed after every command that changes state — the line you most want while exploring."""
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
        except Exception as exc:  # a typo while exploring should not throw you out
            console.print(f"  [red]✗[/] {type(exc).__name__}: {exc}")
            console.print("  [dim]full traceback: debug on[/]")
            if getattr(self, "_debug", False):
                console.print(traceback.format_exc())
        return False

    def emptyline(self) -> bool:
        return False

    def default(self, line: str) -> None:
        console.print(
            f"  [red]✗[/] unknown command [bold]{line.split()[0]}[/]; "
            "type [cyan]help[/] for the list"
        )

    def _args(self, arg: str) -> list[str]:
        return shlex.split(arg)

    def _resolve_batch(self, token: str, kind_flag: str | None) -> tuple[str, str, str]:
        """`aws_images@V1` → (original_set, version, kind).

        One name@version may have both an image and an annotation batch; then
        which one must be said explicitly, never guessed.
        """
        if "@" not in token:
            raise SpecError(
                f"a source is written name@version (e.g. aws_images@V1), got {token!r}"
            )
        name, version = token.rsplit("@", 1)
        kinds = self._batch_index.get(f"{name}@{version}")
        if not kinds:
            raise SpecError(f"{token} not found; [cyan]batches[/] lists them")
        if kind_flag:
            if kind_flag not in kinds:
                raise SpecError(
                    f"{token} has no {kind_flag} batch (it has: {', '.join(kinds)})"
                )
            return name, version, kind_flag
        if len(kinds) > 1:
            raise SpecError(
                f"{token} has both {' and '.join(kinds)} batches; "
                "add --image or --annotation to choose"
            )
        return name, version, kinds[0]

    # -- sources ----------------------------------------------------------

    def do_source(self, arg: str) -> None:
        """Add a whole batch as a source.

        source <name@version> [--image | --annotation]

        An image batch brings its images and the annotations they already have;
        an annotation batch brings its annotations and the images they are on.
        When a name@version has both kinds you must say which.
        """
        args = self._args(arg)
        if not args:
            return console.print(
                "  usage: source <name@version> [--image|--annotation]"
            )
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
        """Import from a file-name list (one name per line).

        import <name@version> <list file> [--strict]

        By default names that match nothing only warn (while exploring you want
        to see how well the list matches); --strict fails the step on any miss.
        """
        args = self._args(arg)
        if len(args) < 2:
            return console.print(
                "  usage: import <name@version> <list file> [--strict]"
            )
        strict = "--strict" in args
        args = [a for a in args if a != "--strict"]
        name, version, _ = self._resolve_batch(args[0], "image")
        path = Path(args[1]).expanduser()
        if not path.exists():
            raise SpecError(f"list file {path} not found")
        names = path.read_text().splitlines()
        self.session.import_list(
            original_set=name,
            image_batch=version,
            file_names=names,
            on_missing="error" if strict else "warn",
            source_note=path.name,
        )
        report = self.session.last_import_report()
        if report:
            console.print(
                f"  matched [bold]{report.matched_count}[/]/{report.requested}"
                + (
                    f", [red]{report.missing_count:,} did not match[/]"
                    if report.missing_count
                    else ""
                )
            )
            for missing in report.missing[:10]:
                console.print(f"    [dim]· {missing}[/]")
            if report.missing_count > 10:
                console.print(f"    [dim]… {report.missing_count - 10:,} more[/]")
        self._report()

    complete_import = complete_source

    # -- narrowing --------------------------------------------------------

    def do_split(self, arg: str) -> None:
        """Deterministic split (the same seed always cuts the same images).

        split --mod 4 --keep 0,1,2 --seed my-seed [--key subject_id|image_id|file_name]

        The key defaults to subject_id, so one patient's images always stay together.
        """
        args = self._args(arg)
        opts = self._kv(args)
        if not {"mod", "keep", "seed"} <= opts.keys():
            return console.print(
                "  usage: split --mod 4 --keep 0,1,2 --seed <seed> [--key subject_id]"
            )
        self.session.split(
            mod=int(opts["mod"]),
            keep_remainder=[int(x) for x in opts["keep"].split(",")],
            seed=opts["seed"],
            key_field=opts.get("key", "subject_id"),
        )
        self._report()

    def do_balance(self, arg: str) -> None:
        """Cap the number of images of every class.

        balance 500 --seed b1              # at most 500 images per target category
        balance 500 --seed b1 --by local   # by local category, before mapping

        Multi-label makes "exactly N per class" impossible — an image that is
        both pneumonia and effusion fills two quotas. Quotas fill rarest class
        first: rare classes take their share, and the images they share count
        towards the common classes too. Picks use hash(seed + image_id), so the
        same seed always picks the same images.
        """
        args = self._args(arg)
        opts = self._kv(args)
        positional = [a for a in args if not a.startswith("-") and a.isdigit()]
        if not positional:
            return console.print(
                "  usage: balance <cap per class> --seed <seed> [--by target|local]"
            )
        if "seed" not in opts:
            return console.print(
                "  [red]✗[/] --seed is required: which images are picked must be deterministic"
            )
        self.session.balance(
            max_per_class=int(positional[0]),
            seed=opts["seed"],
            by=opts.get("by", "target"),
        )
        self._report()
        report = self.session.reports[self.session.head].stats
        console.print(
            table(
                "Class distribution",
                ["class", "before", "after"],
                [
                    [name, before, report["class_counts_after"].get(name, 0)]
                    for name, before in report["class_counts_before"].items()
                ],
            )
        )

    def do_filter(self, arg: str) -> None:
        """Filter by a condition on the metadata or the annotations.

        filter date_captured >= '2022-01-01'
        filter width >= 512 and original_set in ['aws_images', 'DrLee']
        filter regex(file_name, '^DL_2023')
        filter --annotated                    # keep only annotated images
        filter 'Pneumonia' in labels          # filter by annotation
        filter 'pneumonia' in targets and not ('normal' in targets)
        filter n_annotations >= 2             # annotated at least twice

        Image fields: file_name, original_set, batch_version, width, height,
        area, blake3_hash, date_captured, subject_id

        Annotation fields (they follow the earlier steps): labels, targets,
        annotators, n_annotations
        """
        if arg.strip() in ("--annotated", "annotated"):
            self.session.keep_annotated_only()
            return self._report()
        if not arg.strip():
            return console.print(
                "  usage: filter <expression>, e.g. filter width >= 512"
                "\n         filter --annotated   # keep only annotated images"
            )
        self.session.filter(criterion="predicate", expression=arg.strip())
        self._report()

    def do_pick(self, arg: str) -> None:
        """Narrow the current result with a file-name list (the names must already be in the set).

        pick <list file> [--strict]
        """
        args = self._args(arg)
        if not args:
            return console.print("  usage: pick <list file> [--strict]")
        strict = "--strict" in args
        path = Path(next(a for a in args if a != "--strict")).expanduser()
        if not path.exists():
            raise SpecError(f"list file {path} not found")
        self.session.filter(
            criterion="explicit_list",
            file_names=path.read_text().splitlines(),
            on_missing="error" if strict else "warn",
            source_note=path.name,
        )
        self._report()

    def do_duplicates(self, arg: str) -> None:
        """List the images in the current set that share a blake3, with each copy's annotations.

        duplicates [groups]

        dedup discards duplicate images together with their annotations, so look
        before deciding which copy to keep; then pin the winners with
        `dedup --keep <image_id>,<image_id>`.
        """
        limit = int(arg.strip()) if arg.strip().isdigit() else 10
        groups = self.session.find_duplicates()
        if not groups:
            return console.print("  [green]✓[/] no images with duplicate content")

        cross = sum(1 for g in groups if g["cross_source"])
        console.print(
            f"  [bold]{len(groups)}[/] groups of duplicate content (same blake3), "
            f"[bold]{cross}[/] of them across sources"
        )
        for group in groups[:limit]:
            console.print(f"  [dim]{group['blake3'][:16]}…[/]")
            for c in group["candidates"]:
                labels = ", ".join(c["labels"]) or "[red]no annotations[/]"
                ann = len(c["cls_annotation_ids"]) + len(c["det_annotation_ids"])
                console.print(
                    f"    [bold]#{c['image_id']}[/] {c['ref']}"
                    f"  [dim]{ann} annotations · {labels}"
                    f" · subject {c['subject_id'] or 'unknown'}[/]"
                )
        if len(groups) > limit:
            console.print(
                f"  [dim]… {len(groups) - limit} more groups (duplicates <n> shows more)[/]"
            )
        console.print(
            "  [dim]once you have chosen: dedup --keep <image_id>,<image_id>,…[/]"
        )

    def do_dedup(self, arg: str) -> None:
        """Deduplicate by blake3 — discards duplicate images together with their annotations.

        dedup                                  # annotated copy first, then the lowest image_id
        dedup TB-portal,DrLee,aws_images       # add a source priority
        dedup --keep 5,712,918                 # choose each group's survivor by hand
        dedup TB-portal,aws_images --keep 5    # both together

        Look at `duplicates` first. Groups not pinned with --keep keep the
        annotated copy (identical blake3 is the same picture; keeping the bare
        one throws labels away for nothing).
        """
        args = self._args(arg)
        keep: list[int] = []
        if "--keep" in args:
            idx = args.index("--keep")
            if idx + 1 >= len(args):
                return console.print(
                    "  usage: dedup [source priority] --keep <image_id>,<image_id>"
                )
            keep = [int(x) for x in args[idx + 1].split(",") if x.strip()]
            args = args[:idx] + args[idx + 2 :]
        priority = [p.strip() for p in " ".join(args).split(",") if p.strip()]
        self.session.dedup(source_priority=priority, keep=keep)
        self._report()

    def complete_dedup(self, text, line, begidx, endidx):
        names = {b["original_set_name"] for b in self.batches}
        return sorted(n for n in names if n.startswith(text))

    # -- set operations ---------------------------------------------------

    def do_union(self, arg: str) -> None:
        """Join the branches not merged yet (every source/import opens a new branch)."""
        self.session.union()
        self._report()

    def do_intersect(self, arg: str) -> None:
        """Intersect every branch not merged yet."""
        self.session.intersect()
        self._report()

    def do_except(self, arg: str) -> None:
        """The first branch minus the others."""
        self.session.exclude()
        self._report()

    # -- categories -------------------------------------------------------

    def do_categories(self, arg: str) -> None:
        """Show the category mapping so far: what is mapped and what is not."""
        report = self.session.preview_categories()
        if report["targets"]:
            console.print(
                table(
                    "Mapped",
                    ["target", "from local categories"],
                    [
                        [t["name"], ", ".join(t["local_categories"])]
                        for t in report["targets"]
                    ],
                )
            )
        if report["unmapped"]:
            console.print(
                table(
                    "[red]Not mapped yet[/]",
                    ["scope", "local category", "annotations"],
                    [
                        [c["scope"], c["local_name"], c["annotations"]]
                        for c in report["unmapped"]
                    ],
                )
            )
            console.print(
                "  [dim]a real build refuses unmapped categories; "
                "handle them with map or merge_identical.[/]"
            )
        elif report["targets"]:
            console.print("  [green]✓[/] every local category is mapped")
        else:
            console.print("  [dim]no annotations yet[/]")

    def do_map(self, arg: str) -> None:
        """Map the categories of one annotation batch to targets.

        map aws_images@V1 Pneumonia=pneumonia Normal=normal

        The scope must name the annotation batch: category namespaces belong to
        a batch, and the same name meaning different things is normal.
        """
        args = self._args(arg)
        if len(args) < 2:
            return console.print(
                "  usage: map <name@version> Local=target [Local2=target2 ...]"
            )
        scope = args[0]
        mapping = {}
        for pair in args[1:]:
            if "=" not in pair:
                raise SpecError(f"a mapping is written Local=target, got {pair!r}")
            local, target = pair.split("=", 1)
            mapping[local.strip()] = target.strip()
        self.session.map_category(scope, mapping)
        self._report()

    def complete_map(self, text, line, begidx, endidx):
        scopes = {c["scope"] for c in self.session.preview_categories()["unmapped"]}
        return sorted(s for s in scopes if s.startswith(text))

    def do_merge_identical(self, arg: str) -> None:
        """Merge identically named local categories into a target of the same name.

        Only exact matches — Pneumonia and pneumonia are not merged automatically;
        map those explicitly.
        """
        self.session.merge_identical_category()
        self._report()

    # -- conflicts --------------------------------------------------------

    def do_conflicts(self, arg: str) -> None:
        """Show the images labelled by more than one source.

        conflicts [examples]
        """
        limit = int(arg.strip()) if arg.strip().isdigit() else 5
        summary = self.session.conflict_summary()
        if not summary["total"]:
            return console.print(
                "  [green]✓[/] no image is labelled by more than one source"
            )
        console.print(
            f"  [bold]{summary['total']}[/] images are labelled by several sources: "
            f"[red]{summary['contradictions']}[/] truly contradict "
            f"(the sources give different categories), {summary['duplicates']} "
            "merely repeat the same categories"
        )
        console.print(
            table(
                "Source pairs",
                ["pair", "images"],
                [[k, v] for k, v in summary["by_source_pair"].items()],
            )
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
            '  [dim]to choose yourself: resolve manual <annotation_id> ["reason"][/]'
        )

    def do_resolve(self, arg: str) -> None:
        """Settle conflicts. What a rule cannot decide is left alone, never settled silently.

        resolve annotator radiologist_senior,radiologist_junior
        resolve version V3,V2,V1
        resolve score
        resolve manual 1887 "the attending's reading is right"

        manual picks by hand: `conflicts` prints every annotation's id; the one
        you name stays, and the other sources' annotations on that image go.
        """
        args = self._args(arg)
        if not args:
            return console.print(
                "  usage: resolve annotator <a,b,c> | version <V3,V1> | score"
                ' | manual <annotation_id> ["reason"]'
            )
        mode = args[0]

        if mode == "manual":
            if len(args) < 2 or not args[1].isdigit():
                return console.print(
                    '  usage: resolve manual <annotation_id> ["reason"]'
                    "\n  [dim]conflicts prints every id; pick one[/]"
                )
            self.session.resolve_conflicts_by_manual_setting(
                designated_annotation_id=int(args[1]),
                reason=args[2] if len(args) > 2 else "",
            )
            self._report()
            remaining = len(self.session.find_conflicts())
            console.print(
                f"  [yellow]{remaining} images still have unsettled conflicts[/]"
                if remaining
                else "  [green]✓[/] every conflict is settled"
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
                f"unknown resolution {mode!r} (use annotator / version / score / manual)"
            )
        remaining = len(self.session.find_conflicts())
        self._report()
        if remaining:
            console.print(
                f"  [yellow]{remaining} images have conflicts this rule could not settle[/]"
            )
        else:
            console.print("  [green]✓[/] every conflict is settled")

    def complete_resolve(self, text, line, begidx, endidx):
        return [
            m for m in ("annotator", "version", "score", "manual") if m.startswith(text)
        ]

    def _override(self, include: bool, arg: str) -> None:
        verb = "include" if include else "exclude"
        args = self._args(arg)
        if len(args) < 2 or args[0] not in ("image", "cls", "det"):
            return console.print(
                f'  usage: {verb} <image|cls|det> <id> ["reason"]\n'
                f'         {verb} image 315 "poor image quality"\n'
                f'         {verb} cls 1887 "this label is wrong"\n'
                "  [dim]ids come from the output of images / duplicates / conflicts[/]"
            )
        if not args[1].isdigit():
            return console.print(
                f"  [red]✗[/] the id must be a number, got {args[1]!r}"
            )
        self.session.override_one(
            include=include,
            kind=args[0],
            target_id=int(args[1]),
            reason=args[2] if len(args) > 2 else "",
        )
        self._report()

    def complete_exclude(self, text, line, begidx, endidx):
        return [k for k in ("image", "cls", "det") if k.startswith(text)]

    complete_include = complete_exclude

    def do_exclude(self, arg: str) -> None:
        """Exclude one image or one annotation.

        exclude image 315 "poor image quality"
        exclude cls 1887 "this label is wrong"
        exclude det 42 "box in the wrong place"

        Always by id: file names are not globally unique, so a path string will
        point at the wrong row sooner or later. Ids come from the output of
        `images`, `duplicates` and `conflicts`. The reason goes into the spec,
        where `cxr why` finds it later.
        """
        self._override(False, arg)

    def do_include(self, arg: str) -> None:
        """Include one image or one annotation — bring back what an earlier step excluded.

        include image 1 "rare presentation, the training set needs it"
        include cls 291 "the attending's reading"

        Including an image brings its annotations too (as source --image does).
        """
        self._override(True, arg)

    # -- inspecting -------------------------------------------------------

    def do_preview(self, arg: str) -> None:
        """A full statistical summary of the current state."""
        if not self.session.head:
            return console.print("  [dim]no steps yet[/]")
        p = self.session.preview()
        console.print(_preview_panel(self.session.name, p))
        _print_preview_tables(p)
        _print_preview_distribution(p)
        if p["categories"]["unmapped_local"]:
            console.print(
                f"  [yellow]⚠[/] {len(p['categories']['unmapped_local'])} local "
                "categories are not mapped yet (type categories for details)"
            )

    def do_steps(self, arg: str) -> None:
        """List the steps so far — the list that compiles into the spec."""
        rows = self.session.describe()
        if not rows:
            return console.print("  [dim]No steps yet[/]")
        console.print(
            table(
                "Pipeline",
                ["#", "step_id", "op", "inputs", "images", "cls", "det", "branch", ""],
                [
                    [
                        i,
                        r["step_id"],
                        r["op"],
                        ", ".join(r["inputs"]) or "—",
                        r["counts"]["images"],
                        r["counts"]["cls"],
                        r["counts"]["det"],
                        "[yellow]BRANCH END[/]" if r["open"] else "",
                        "[cyan]← head[/]" if r["head"] else "",
                    ]
                    for i, r in enumerate(rows)
                ],
            )
        )
        open_ends = [r["step_id"] for r in rows if r["open"]]
        if len(open_ends) > 1:
            console.print(
                f"  [dim]{len(open_ends)} branches not merged yet ({', '.join(open_ends)}) — "
                "join them with union, or commit adds a union step for you[/]"
            )
        console.print("  [dim]checkout <step_id> can move head to other branch.[/]")

    def do_images(self, arg: str) -> None:
        """List a sample of the images in the current set.

        images [count]
        """
        limit = int(arg.strip()) if arg.strip().isdigit() else 10
        cand, catalog = self.session.current, self.session.catalog
        ids = sorted(cand.images)[:limit]
        if not ids:
            return console.print("  [dim]no images yet[/]")
        rows = []
        for image_id in ids:
            meta = catalog.image(image_id)
            targets = sorted(
                {
                    cand.category_targets[catalog.cls(a).category_id]
                    for a in cand.cls
                    if catalog.cls(a).image_id == image_id
                    and catalog.cls(a).category_id in cand.category_targets
                }
            )
            rows.append(
                [
                    f"#{image_id}",
                    meta.ref,
                    meta.subject_id or "—",
                    ", ".join(targets) or "—",
                ]
            )
        console.print(
            table(
                f"Images (first {len(ids)} of {len(cand.images)})",
                ["id", "image", "subject", "categories"],
                rows,
            )
        )
        console.print('  [dim]exclude image <id> ["reason"] excludes one of them[/]')

    def do_batches(self, arg: str) -> None:
        """List every batch that can be a source."""
        console.print(
            table(
                "Batches",
                ["in a spec", "kind", "items", "categories"],
                [
                    [
                        f"{b['original_set_name']}@{b['version']}",
                        b["batch_kind"],
                        b["item_count"],
                        b["categories"] or "",
                    ]
                    for b in self.batches
                ],
            )
        )

    # -- trial and error --------------------------------------------------

    def do_checkpoint(self, arg: str) -> None:
        """Remember where you are, to rollback to it later.

        checkpoint after_dedup
        """
        label = arg.strip()
        if not label:
            return console.print("  usage: checkpoint <label>")
        self.session.checkpoint(label)
        console.print(
            f"  [green]✓[/] remembered [bold]{label}[/] (step {len(self.session.steps)})"
        )

    def do_rollback(self, arg: str) -> None:
        """Go back to a checkpoint, discarding every step after it.

        rollback after_dedup
        """
        label = arg.strip()
        if not label:
            return console.print(
                f"  usage: rollback <label> (existing: {', '.join(self.session.checkpoints) or 'none'})"
            )
        self.session.rollback(label)
        console.print(f"  [green]✓[/] back at [bold]{label}[/]")
        self._report()

    def complete_rollback(self, text, line, begidx, endidx):
        return [c for c in self.session.checkpoints if c.startswith(text)]

    def do_checkout(self, arg: str) -> None:
        """Move to a step; the next commands apply on top of it.

        checkout source_2

        Every source / import opens a new branch and moves there; this command
        goes back to an earlier branch to keep working on it. `steps` shows
        where you are.
        """
        step_id = arg.strip()
        if not step_id:
            return console.print(
                f"  usage: checkout <step_id> (existing: {', '.join(s.id for s in self.session.steps) or 'none'})"
            )
        self.session.checkout(step_id)
        console.print(f"  [green]✓[/] now at [bold]{step_id}[/]")
        self._report()

    def complete_checkout(self, text, line, begidx, endidx):
        return [s.id for s in self.session.steps if s.id.startswith(text)]

    def do_undo(self, arg: str) -> None:
        """Undo the step you are on; you move back to its input.

        Unlike rollback: undo goes back one step and needs no label; rollback
        returns to a checkpoint, discarding every step in between.
        """
        if not self.session.steps or self.session.head is None:
            return console.print("  [dim]nothing to undo[/]")
        dropped = self.session.head
        self.session.undo()
        console.print(f"  [green]✓[/] undid [bold]{dropped}[/]")
        self._report()

    # -- output -----------------------------------------------------------

    def do_spec(self, arg: str) -> None:
        """Show the spec the current steps compile to (display only; save writes a file)."""
        if not self.session.steps:
            return console.print("  [dim]no steps yet[/]")
        spec = self.session.compile(strict_conflicts=False)
        # Display only. Saving is `save`, which writes the file without the terminal.
        console.print(Syntax(spec.to_yaml(), "yaml", theme="ansi_dark", word_wrap=True))

    def do_save(self, arg: str) -> None:
        """Save the spec as a YAML file (run it with cxr build later, or load it back).

        save                    saves to a temp file and prints the path
        save pneumonia_v5.yaml  saves where you say

        For keeping a snapshot mid-exploration, or for diffing. A commit puts
        the spec into object storage by itself; no separate save is needed.
        """
        spec = self.session.compile()
        raw = arg.strip()
        if raw:
            path = Path(raw).expanduser()
        else:
            # No name means the temp directory. Keeping a snapshot mid-exploration
            # is a casual act; it should not demand a file name and a place.
            tmp = Path(tempfile.gettempdir()) / "cxr-specs"
            tmp.mkdir(parents=True, exist_ok=True)
            path = tmp / f"{self.session.name}-{spec.sha256()[:12]}.yaml"
        path.write_text(spec.to_yaml())
        # soft_wrap: temp paths are long, and one Rich folds in two cannot be copied
        console.print(
            f"  [green]✓[/] saved to [bold]{path}[/]  sha256 {spec.sha256()[:16]}…",
            soft_wrap=True,
        )

    def do_load(self, arg: str) -> None:
        """Load an existing spec and keep exploring from it.

        load pneumonia_v5.yaml     from a local file
        load pneumonia@V1          that version's spec, from object storage
        """
        token = arg.strip()
        if not token:
            return console.print("  usage: load <file.yaml> or <manual-set>@<version>")
        spec = crud.load_spec_from(
            self.db,
            token,
            on_warning=lambda msg: console.print(f"  [yellow]⚠[/] {msg}"),
        )
        self.session.replay(spec)
        console.print(f"  [green]✓[/] loaded {len(self.session.steps)} steps")
        self._report()

    def do_commit(self, arg: str) -> None:
        """Produce an official manual-set version.

        commit -m pneumonia -v V5 [--dry-run]

        --dry-run runs everything and writes nothing at all. A real commit
        records who built it — asked on the spot unless CXR_AUTHOR_NAME /
        CXR_AUTHOR_EMAIL are set — and then asks for the version's __meta__.md:
        a description, what changed since the previous version, and how each
        source was selected. It is stored beside the spec, and
        `cxr meta manual-set` rewrites it later.
        """
        args = self._args(arg)
        opts = self._kv(args)
        dry = "--dry-run" in args
        name = opts.get("m") or opts.get("name") or self.session.name
        version = opts.get("v") or opts.get("version") or "V1"
        if not self.session.steps:
            return console.print("  [red]✗[/] no steps yet")

        author = (
            None
            if dry
            else resolve_author(opts.get("author-name"), opts.get("author-email"))
        )
        result = self.session.commit(
            version=version,
            manual_set_name=name,
            dry_run=dry,
            author=author,
            meta=None if dry else on_commit(self.db),
        )
        counts = result.counts
        body = (
            f"images [bold]{counts['images']}[/]  cls [bold]{counts['cls']}[/]  "
            f"det [bold]{counts['det']}[/]"
        )
        if dry:
            console.print(
                Panel(body, title="[yellow]Dry run finished[/] (nothing was written)")
            )
        else:
            body += f"\ntarget categories: {', '.join(result.target_categories) or '—'}"
            body += f"\nspec → [dim]{result.spec_key}[/]"
            body += f"\n__meta__.md → [dim]{result.meta_location}[/]"
            console.print(Panel(body, title=f"[green]✓[/] {name}@{version}"))
            console.print(f"  [dim]built by {author}[/]")
            console.print(
                f"  [dim]cxr show {name}@{version}   "
                f"cxr export {name}@{version} -f parquet[/]"
            )

    # -- misc -------------------------------------------------------------

    def do_debug(self, arg: str) -> None:
        """Toggle full tracebacks: debug on / debug off"""
        self._debug = arg.strip() == "on"
        console.print(f"  debug = {'on' if self._debug else 'off'}")

    def do_quit(self, arg: str) -> bool:
        """Leave (the exploration state is not kept)."""
        if self.session.steps:
            console.print(
                f"  [dim]{len(self.session.steps)} steps will not be kept. "
                "To keep them use save <file.yaml> or commit.[/]"
            )
        return True

    do_exit = do_quit
    do_EOF = do_quit

    @staticmethod
    def _kv(args: list[str]) -> dict[str, str]:
        """Split --key value pairs into a dict."""
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


BANNER = """[bold cyan]cxr explore[/] —— Interactive manual-set exploration and construction

  [bold]Source         [/]    source aws_images@V1 --annotation | import DrLee@V1 list.txt | load
  [bold]Limit          [/]    split --mod 4 --keep 0,1,2 --seed s1 | filter 'x' in targets | balance 500 --seed b | pick list.txt
  [bold]Reorganize     [/]    union | duplicates | dedup --keep 5,712 | merge_identical | map aws_images@V1 A=a
  [bold]Manual decision[/]    include / exclude <image|cls|det> <id> ["reason"]
  [bold]Inspect        [/]    preview | steps | categories | conflicts | images | batches
  [bold]Debug          [/]    checkpoint <checkpoint_name> | rollback <checkpoint_name> | undo | checkout <step_id>
  [bold]Output         [/]    spec | save x.yaml | commit -m <manual-set-name> -v <version> [--dry-run]

  Exploration state only stays in the memory, gone at exit.
  [dim]To keep the explored state please use `save` or `commit` [/]
  [dim]Tab completion · `help <command>` for details · `quit` to leave[/]"""


def run(name: str) -> None:
    shell = ExploreShell(name)
    console.print(Panel(BANNER, border_style="cyan"))
    while True:
        try:
            shell.cmdloop(intro="")
            break
        except KeyboardInterrupt:
            # Ctrl-C cancels only this line; it must not throw the session away
            console.print("\n  [dim]^C (type `quit` to leave)[/]")
