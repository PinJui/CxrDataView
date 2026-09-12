"""cxr — the terminal interface (Layer 4).

Only a skin: every command calls a core/session function and prints the result
nicely, with no business logic of its own (design_doc §1 principle 7).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import typer
from rich.panel import Panel
from rich.syntax import Syntax

from cxr_dataset_manager.cli import meta as meta_prompts
from cxr_dataset_manager.cli._common import (
    console,
    die,
    interactive,
    parse_ref,
    resolve_author,
    resolve_version_id,
    table,
)
from cxr_dataset_manager.core import export as export_mod
from cxr_dataset_manager.core.engine import build as run_build
from cxr_dataset_manager.core.schema import BuildSpec
from cxr_dataset_manager.core.types import SpecError
from cxr_dataset_manager.db import crud
from cxr_dataset_manager.db.engine import apply_schema, get_engine, new_session
from cxr_dataset_manager.settings import settings

app = typer.Typer(
    help="A chest X-ray dataset manager to handle original sets, manual sets, and their versions.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="Database initialization and seeding.", no_args_is_help=True)
ls_app = typer.Typer(help="Browse datasets.", no_args_is_help=True)
lists_app = typer.Typer(help="External file-name lists.", no_args_is_help=True)
meta_app = typer.Typer(
    help="Write the __meta__.md that describes a batch or a manual-set version.",
    no_args_is_help=True,
)
app.add_typer(db_app, name="db")
app.add_typer(ls_app, name="ls")
app.add_typer(lists_app, name="lists")
app.add_typer(meta_app, name="meta")

EXPORT_FORMATS = ("zip", "coco", "csv", "parquet")


def _spec_line(summary: dict) -> str:
    """The line of the `cxr show` panel saying where the spec is and whether it is intact."""
    status = summary["spec_status"]
    if status == "none":
        return "— (an imported version, no spec)"
    where = f"[dim]{summary['spec_key']}[/]"
    if status == "ok":
        return f"{where}  sha256 {summary['spec_sha256'][:16]}…"
    if status == "missing":
        return f"{where}  [red]the file is missing[/]"
    return f"{where}  [yellow]content does not match its fingerprint[/]"


def _print_category_distribution(rows: list[dict], total_images: int) -> None:
    """Positive / negative / unknown per target category in this version.

    The three cls columns count images and exclude each other, so they always
    add up to the image count; det counts boxes — a film can have several — so
    it is a different unit and must not be added to them.
    """
    if not rows:
        return
    console.print(
        table(
            f"Category distribution (cls counted in images, {total_images} in all; det in boxes)",
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
    # A det box with a NULL score is neither POS nor negative and would vanish
    # from the table — when there are any, say so, or nobody sees what is missing.
    unscored = sum(r["det_no_score"] for r in rows)
    if unscored:
        console.print(
            f"  [yellow]⚠[/] {unscored} det boxes have no score and are not counted in DET POS"
        )


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------


@db_app.command("init")
def db_init(
    drop: bool = typer.Option(
        False, "--drop", help="Empty the public schema first, then create it"
    ),
):
    """Apply db/*.sql to create the schema."""
    apply_schema(get_engine(), drop_first=drop)
    console.print(f"[green]✓[/] schema applied to {settings.database_url}")


@db_app.command("seed")
def db_seed(
    with_images: bool = typer.Option(
        True, help="Also upload the synthetic images to MinIO"
    ),
):
    """Generate mock data (with deliberate duplicates, conflicts and inconsistent names)."""
    from cxr_dataset_manager.seed import seed_demo
    from cxr_dataset_manager.storage import get_store

    store = None
    if with_images:
        store = get_store()
        if not store.alive():
            die(
                f"cannot reach object storage at {settings.s3_endpoint_url} "
                "(--no-with-images seeds the metadata only)"
            )
        store.ensure_buckets()

    counts = seed_demo(new_session(), store)
    console.print(
        table("Mock data", ["table", "rows"], [[k, v] for k, v in counts.items()])
    )


@db_app.command("reset")
def db_reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask, just do it"),
):
    """Drop and recreate the schema, then seed the mock data again."""
    if not yes and not typer.confirm(
        f"This erases every row in {settings.database_url}. Continue?"
    ):
        raise typer.Abort()
    apply_schema(get_engine(), drop_first=True)
    db_seed(with_images=True)


@db_app.command("status")
def db_status():
    """Check that Postgres / MinIO respond, and print how much data there is."""
    from cxr_dataset_manager.storage import get_store

    rows = []
    try:
        db = new_session()
        sets = crud.list_original_sets(db)
        rows.append(["postgres", settings.database_url, "[green]ok[/]"])
        rows.append(
            [
                "  original-sets",
                str(len(sets)),
                str(sum(s["images"] for s in sets)) + " images",
            ]
        )
        rows.append(["  manual-sets", str(len(crud.list_manual_sets(db))), ""])
    except Exception as exc:
        rows.append(["postgres", settings.database_url, f"[red]{exc}[/]"])
    store = get_store()
    rows.append(
        [
            "minio",
            settings.s3_endpoint_url or "-",
            "[green]ok[/]" if store.alive() else "[red]unreachable[/]",
        ]
    )
    console.print(table("Services", ["component", "location", "status"], rows))


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


@ls_app.command("sets")
def ls_sets():
    """List every original-set."""
    rows = crud.list_original_sets(new_session())
    console.print(
        table(
            "Original sets",
            ["name", "image batches", "annotation batches", "images"],
            [
                [r["name"], r["image_batches"], r["annotation_batches"], r["images"]]
                for r in rows
            ],
        )
    )


@ls_app.command("batches")
def ls_batches(original_set: str | None = typer.Argument(None)):
    """List image/annotation batches (a spec's source names them as name@version)."""
    rows = crud.list_batches(new_session(), original_set)
    console.print(
        table(
            "Batches",
            ["original_set", "kind", "version", "in a spec", "items", "categories"],
            [
                [
                    r["original_set_name"],
                    r["batch_kind"],
                    r["version"],
                    f"{r['original_set_name']}@{r['version']}",
                    r["item_count"],
                    r["categories"] or "",
                ]
                for r in rows
            ],
        )
    )


@ls_app.command("categories")
def ls_categories():
    """List each annotation batch's local category namespace."""
    rows = crud.list_categories(new_session())
    console.print(
        table(
            "Local categories",
            ["scope", "name", "supercategory", "id"],
            [
                [
                    f"{r['original_set']}@{r['version']}",
                    r["name"],
                    r["supercategory"],
                    r["id"],
                ]
                for r in rows
            ],
        )
    )


@ls_app.command("annotators")
def ls_annotators():
    """List annotators and how much each of them labelled."""
    rows = crud.list_annotators(new_session())
    console.print(
        table(
            "Annotators",
            ["name", "cls", "det"],
            [[r["name"], r["cls"], r["det"]] for r in rows],
        )
    )


@ls_app.command("manual-sets")
def ls_manual_sets():
    """List every manual-set and its versions."""
    rows = crud.list_manual_sets(new_session())
    table_rows = []
    for entry in rows:
        for v in entry["versions"] or [{}]:
            table_rows.append(
                [
                    entry["name"],
                    v.get("version", "—"),
                    v.get("images", ""),
                    v.get("cls", ""),
                    v.get("det", ""),
                    v.get("targets", ""),
                    str(v.get("created_at", ""))[:19],
                ]
            )
    console.print(
        table(
            "Manual sets",
            ["name", "version", "images", "cls", "det", "target categories", "created"],
            table_rows,
        )
    )


@ls_app.command("history")
def ls_history(limit: int = 20):
    """List the build history: every version and the spec that made it.

    Exploration never reaches the database, so this is everything — one spec
    per version.
    """
    rows = crud.build_history(new_session(), limit)
    console.print(
        table(
            "Build history",
            ["manual-set", "version", "images", "spec", "built by", "created"],
            [
                [
                    r["manual_set"],
                    r["version"],
                    r["images"],
                    (r["spec_sha256"] or "—")[:12],
                    r["created_by_name"],
                    str(r["created_at"])[:19],
                ]
                for r in rows
            ],
        )
    )


# ---------------------------------------------------------------------------
# external file-name lists
# ---------------------------------------------------------------------------


@lists_app.command("add")
def lists_add(
    file: Path = typer.Argument(..., help="A text file with one file name per line"),
    note: str | None = typer.Option(
        None, "--note", "-n", help="A description for humans"
    ),
):
    """Store a file-name list in the database and print its sha256.

    A spec refers to it with file_names_ref instead of inlining ten thousand
    names. Content-addressed: storing the same list twice keeps one row.
    """
    if not file.exists():
        die(f"{file} not found")
    names = file.read_text().splitlines()
    digest, created = crud.register_import_list(new_session(), names, note or file.name)
    count = len(crud.normalize_file_names(names))
    console.print(
        f"[green]✓[/] {'stored' if created else 'already stored (same content)'} "
        f"{count:,} names\n  sha256 [bold]{digest}[/]"
    )
    console.print(
        "\n  refer to it in a spec like this:\n"
        f"[dim]    file_names_ref:\n"
        f"      sha256: {digest}\n"
        f"      source: {note or file.name}[/]"
    )


@lists_app.command("ls")
def lists_ls(limit: int = 30):
    """List the stored lists."""
    rows = crud.list_import_lists(new_session(), limit)
    console.print(
        table(
            "File-name lists",
            ["sha256", "names", "note", "created"],
            [
                [
                    r["sha256"][:16] + "…",
                    f"{r['n']:,}",
                    r["source_note"] or "",
                    str(r["created_at"])[:19],
                ]
                for r in rows
            ],
        )
    )


@lists_app.command("show")
def lists_show(
    sha256: str = typer.Argument(..., help="The full hash or a prefix of it"),
    limit: int = typer.Option(
        20, "--limit", "-n", help="How many names to print; 0 prints all"
    ),
):
    """Print the contents of a list."""
    db = new_session()
    names = crud.get_import_list(db, sha256)
    if names is None:
        matches = [
            r for r in crud.list_import_lists(db, 500) if r["sha256"].startswith(sha256)
        ]
        if len(matches) != 1:
            die(
                f"no list whose sha256 starts with {sha256}"
                if not matches
                else f"{sha256} matches {len(matches)} lists; give a longer prefix"
            )
        names = crud.get_import_list(db, matches[0]["sha256"])
    assert names is not None
    shown = names if limit == 0 else names[:limit]
    for name in shown:
        console.print(f"  {name}")
    if len(shown) < len(names):
        console.print(
            f"  [dim]… {len(names) - len(shown):,} more (-n 0 prints all)[/]"
        )


# ---------------------------------------------------------------------------
# validating and running a spec
# ---------------------------------------------------------------------------


def _spec_argument(db, token: str) -> BuildSpec:
    """A spec argument on the command line: a local file or manual-set@version."""
    try:
        return crud.load_spec_from(
            db, token, on_warning=lambda msg: console.print(f"[yellow]⚠[/] {msg}")
        )
    except SpecError as exc:
        die(str(exc))
        raise


def _load_spec(path: Path) -> BuildSpec:
    if not path.exists():
        die(f"spec file {path} not found")
    try:
        return BuildSpec.from_yaml(path.read_text())
    except Exception as exc:
        die(f"invalid spec:\n{exc}")
        raise


@app.command()
def validate(spec_file: Path = typer.Argument(..., help="A spec YAML file")):
    """Only checks syntax of spec and dependency of step. (Will not touch database)"""
    spec = _load_spec(spec_file)
    console.print(
        f"[green]✓[/] valid spec, {len(spec.steps)} steps, final = [bold]{spec.final}[/]"
    )
    console.print(f"  sha256 = {spec.sha256()}")
    console.print(
        table(
            "Steps",
            ["#", "step_id", "op", "inputs"],
            [
                [i, s.id, s.op, ", ".join(s.input_ids()) or "—"]
                for i, s in enumerate(spec.steps)
            ],
        )
    )


@app.command()
def build(
    spec_file: str = typer.Argument(
        ...,
        help="A spec YAML file, or manual-set@version (reuse the spec of that version)",
    ),
    manual_set: str = typer.Option(
        ..., "--manual-set", "-m", help="The manual-set to write into"
    ),
    version: str = typer.Option("V1", "--version", "-v"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Run everything but write nothing; just show the result"
    ),
    author_name: str | None = typer.Option(
        None, "--author-name", help="Name of the builder"
    ),
    author_email: str | None = typer.Option(
        None, "--author-email", help="Email of the builder"
    ),
):
    """Run a spec, generate a manual-set version in the database.

    The spec can be a local file or `manual-set@version` — the latter reuses the
    recipe of that version, to "make another one the way V1 was made". A real
    build asks for the version's __meta__.md once the result is known (with no
    terminal it writes the statistics and leaves the rest N/A).
    """
    db = new_session()
    spec = _spec_argument(db, spec_file)
    author = None if dry_run else resolve_author(author_name, author_email)
    try:
        result = run_build(
            db,
            spec,
            manual_set,
            version,
            dry_run=dry_run,
            author=author,
            meta=None if dry_run else meta_prompts.on_commit(db),
        )
    except SpecError as exc:
        die(str(exc))
        raise

    console.print(
        table(
            "Execution",
            ["#", "step_id", "op", "images", "cls", "det"],
            [
                [
                    r.step_index,
                    r.step_id,
                    r.op,
                    r.counts["images"],
                    r.counts["cls"],
                    r.counts["det"],
                ]
                for r in result.execution.reports
            ],
        )
    )
    for warning in result.execution.warnings:
        console.print(f"[yellow]⚠[/]  {warning}")

    if dry_run:
        console.print(
            Panel(
                f"images [bold]{result.counts['images']}[/]  "
                f"cls [bold]{result.counts['cls']}[/]  det [bold]{result.counts['det']}[/]\n"
                f"spec sha256 {spec.sha256()[:16]}…",
                title="[yellow]Dry run finished[/] (nothing was written)",
            )
        )
        return
    console.print(
        Panel(
            f"images [bold]{result.counts['images']}[/]  "
            f"cls [bold]{result.counts['cls']}[/]  det [bold]{result.counts['det']}[/]\n"
            f"target categories: {', '.join(result.target_categories) or '—'}\n"
            f"spec sha256 {spec.sha256()[:16]}…\n"
            f"__meta__.md → [dim]{result.meta_location}[/]",
            title=f"[green]✓[/] {manual_set}@{version}",
        )
    )


@app.command()
def show(
    ref: str = typer.Argument(..., help="manual-set@version, e.g. pneumonia_v4@V1"),
):
    """Show the composition of a manual-set version."""
    db = new_session()
    version_id = resolve_version_id(db, ref)
    summary = crud.version_summary(db, version_id)

    console.print(
        Panel(
            f"Images [bold]{summary['images']}[/]  cls [bold]{summary['cls']}[/]  "
            f"det [bold]{summary['det']}[/]\n"
            f"Subjects {summary['subjects']['distinct']}"
            f" ({summary['subjects']['images_without_subject']} images without subject information)\n"
            f"Built by {summary['created_by']}\n"
            f"spec {_spec_line(summary)}",
            title=f"[cyan]{ref}[/]",
        )
    )
    console.print(
        table(
            "Composition",
            ["source", "images"],
            [[k, v] for k, v in summary["by_source"].items()],
        )
    )
    console.print(
        table(
            "Target categories",
            ["target", "annotations", "from local categories"],
            [
                [k, v, ", ".join(summary["category_mappings"].get(k, []))]
                for k, v in summary["by_target_category"].items()
            ],
        )
    )
    _print_category_distribution(summary["category_distribution"], summary["images"])


@app.command()
def spec(
    ref: str = typer.Argument(..., help="manual-set@version"),
    out: Path | None = typer.Option(
        None, "--out", "-o", help="Save it as a YAML file (without this it is only shown)"
    ),
):
    """Show the spec of a certain version, or save it to a YAML file.

    A spec has three operations: save (a YAML file), view, and load (`cxr build`
    or `load` in the REPL). Saving always uses -o, which writes the file
    directly without going through the terminal.

    The terminal is for people: `cxr spec x@V1` is laid out and coloured, and
    redirecting it into a file does not give a usable spec — it is a display
    channel, not a data channel.
    """
    db = new_session()
    version_id = resolve_version_id(db, ref)
    loaded = crud.load_spec(db, version_id)
    if loaded["yaml"] is None:
        die(f"{ref}: {crud._spec_unavailable(loaded)}")
    if loaded["status"] == "modified":
        console.print(f"[yellow]⚠[/] {crud._spec_unavailable(loaded)}")

    if out is not None:
        # Written straight to the file, never through the console: what is saved
        # must be byte-identical to the copy in object storage.
        out.expanduser().write_text(loaded["yaml"])
        console.print(f"[green]✓[/] saved to [bold]{out}[/]", soft_wrap=True)
        return
    # word_wrap=True: long lines wrap instead of being cropped, so no text is lost
    console.print(Syntax(loaded["yaml"], "yaml", theme="ansi_dark", word_wrap=True))


def _print_image_annotations(d: dict, version: str | None) -> None:
    rows = [
        [f"#{a['id']}", "cls", a["category"], a["source"], a["annotator"], a["score"]]
        for a in d["cls_annotations"]
    ] + [
        [f"#{a['id']}", "det", a["category"], a["source"], a["annotator"], a["score"]]
        for a in d["det_annotations"]
    ]
    console.print(
        table(
            "Annotations" + (f" (only {version})" if version else ""),
            ["id", "kind", "category", "source", "annotator", "score"],
            rows,
        )
        if rows
        else table("Annotations", ["id"], [])
    )


def _print_image_lineage(d: dict) -> None:
    if not d["lineage"]:
        return
    console.print(
        table(
            "Lineage",
            ["direction", "image", "id"],
            [
                [
                    "◀ from" if l["direction"] == "parent" else "▶ derived into",
                    f"{l['original_set']}/{l['version']}/{l['file_name']}",
                    f"#{l['id']}",
                ]
                for l in d["lineage"]
            ],
        )
    )


def _print_image_duplicates(d: dict) -> None:
    if not d["duplicates"]:
        return
    console.print(
        table(
            "Other images with identical content (same blake3)",
            ["image", "id"],
            [
                [
                    f"{x['original_set']}/{x['batch_version']}/{x['file_name']}",
                    f"#{x['id']}",
                ]
                for x in d["duplicates"]
            ],
        )
    )


def _print_image_usage(d: dict) -> None:
    if not d["used_by"]:
        return
    console.print(
        table(
            "Used by these datasets",
            ["manual-set", "version", "annotations selected"],
            [[u["name"], u["version"], u["annotations"]] for u in d["used_by"]],
        )
    )
    console.print(
        f"  [dim]cxr why <manual-set@version> --image "
        f"{d['original_set']}/{d['batch_version']}/{d['file_name']} "
        "shows how it got in[/]"
    )


@app.command()
def image(
    ref: str = typer.Argument(..., help="An image_id, or original_set/version/file_name"),
    version: str | None = typer.Option(
        None,
        "--version",
        "-v",
        help="Only show the annotations a manual-set version selected",
    ),
):
    """Show a image's annotation, lineage, content hash, referenced by which manual-sets.

    Ids come from the images / duplicates / conflicts output of `cxr explore`;
    original_set/version/file_name works too.
    """
    db = new_session()
    image_id = crud.resolve_image(db, ref)
    if image_id is None:
        die(
            f"image {ref} not found"
            + (
                " (a bare file name may match several images; "
                "give the full path or the image_id)"
                if not ref.isdigit()
                else ""
            )
        )

    version_id = resolve_version_id(db, version) if version else None
    d = crud.image_detail(db, image_id, version_id)

    console.print(
        Panel(
            f"[bold]{d['original_set']}/{d['batch_version']}/{d['file_name']}[/]\n"
            f"{d['width']} × {d['height']}   subject {d['subject_id'] or 'unknown'}   "
            f"captured {d['date_captured'] or 'unknown'}   "
            f"license {d['license'] or 'unknown'}\n"
            f"blake3 {d['blake3_hash'] or '—'}",
            title=f"[cyan]image #{image_id}[/]",
        )
    )
    _print_image_annotations(d, version)
    _print_image_lineage(d)
    _print_image_duplicates(d)
    _print_image_usage(d)


@app.command()
def why(
    ref: str = typer.Argument(..., help="manual-set@version"),
    image: str = typer.Option(
        ..., "--image", "-i", help="A file name, or original_set/version/file_name"
    ),
):
    """Answer the question that "At which step, or by which criteria was this image selected or excluded?"""
    db = new_session()
    version_id = resolve_version_id(db, ref)

    result = crud.explain(db, version_id, file_name=image)
    if not result["found"]:
        die(result["reason"])

    status = (
        "[green]in the final set[/]"
        if result["in_final_set"]
        else "[red]not in the final set[/]"
    )
    console.print(Panel(f"{image}\n{status}", title=f"cxr why · {ref}"))

    if not result["trail"]:
        console.print(
            "[yellow]No step of this build mentions the image — "
            "it never entered the candidate set.[/]"
        )
        return

    colours = {
        "added": "green",
        "dropped": "red",
        "remapped": "yellow",
        "overridden": "magenta",
        "missing": "red",
    }
    for entry in result["trail"]:
        kind = entry["entity_kind"].replace("_annotation", " annotation")
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
    left: str = typer.Argument(..., help="manual-set@version"),
    right: str = typer.Argument(..., help="manual-set@version"),
):
    """Compare two manual-set versions."""
    db = new_session()
    ids = []
    for ref in (left, right):
        ids.append(resolve_version_id(db, ref))

    result = crud.diff_versions(db, ids[0], ids[1])
    console.print(
        table(
            "Images",
            ["", "count"],
            [
                [f"in {left}", result["left"]["images"]],
                [f"in {right}", result["right"]["images"]],
                ["added", f"[green]+{result['images']['added']}[/]"],
                ["removed", f"[red]-{result['images']['removed']}[/]"],
                ["in both", result["images"]["unchanged"]],
            ],
        )
    )
    console.print(
        table(
            "cls annotations",
            ["", "count"],
            [
                ["added", f"[green]+{result['cls_annotations']['added']}[/]"],
                ["removed", f"[red]-{result['cls_annotations']['removed']}[/]"],
                [
                    "images whose annotations changed",
                    result["cls_annotations"]["images_with_changed_annotations"],
                ],
            ],
        )
    )
    if result["category_mapping_changes"]:
        console.print(
            table(
                "Category mapping changes",
                ["local category", left, right],
                [
                    [k, v["from"] or "—", v["to"] or "—"]
                    for k, v in result["category_mapping_changes"].items()
                ],
            )
        )
    if result["images"]["added_sample"]:
        console.print(
            "[green]added, e.g.:[/] " + ", ".join(result["images"]["added_sample"][:5])
        )
    if result["images"]["removed_sample"]:
        console.print(
            "[red]removed, e.g.:[/] "
            + ", ".join(result["images"]["removed_sample"][:5])
        )


@app.command("check-leakage")
def check_leakage(
    refs: list[str] = typer.Argument(..., help="Two or more manual-set@version"),
):
    """Check if there are any duplicate content or shared subjects between multiple versions."""
    if len(refs) < 2:
        die("give at least two versions to compare")
    db = new_session()
    ids = []
    for ref in refs:
        ids.append(resolve_version_id(db, ref))

    report = crud.duplicate_report(db, ids)
    content = report["identical_content"]["groups"]
    subjects = report["shared_subjects"]["count"]
    style = "red" if (content or subjects) else "green"
    console.print(
        Panel(
            f"Identical image content (same blake3): [bold]{content}[/] groups\n"
            f"Subjects shared across versions: [bold]{subjects}[/]"
            f" ({report['shared_subjects']['images_involved']} images involved)",
            title=f"[{style}]Leakage check[/] " + " ↔ ".join(refs),
        )
    )
    for group in report["identical_content"]["sample"][:5]:
        console.print(f"  · {group['blake3_hash'][:12]}… → {', '.join(group['refs'])}")
    for group in report["shared_subjects"]["sample"][:5]:
        console.print(
            f"  · subject {group['subject_id']} appears in versions {group['versions']}"
        )


@app.command()
def rm(
    ref: str = typer.Argument(
        ..., help="manual-set@version, or only the name to delete all of it"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask, just delete"),
):
    """Delete a manual-set version, or the entire manual-set.

    Versions are normally immutable records, but this is an exception to that rule—when a build is incorrect and you want to reuse a version number,
    or to clean up experimental artifacts. The original images and annotations are completely unaffected, as manual-sets are merely records of "which ones were selected".

    Corresponding spec.yaml and __meta__.md files stored in the object storage will also be deleted, and once deleted, the dataset cannot be reproduced.
    """
    db = new_session()
    manual_set, _, version = ref.partition("@")
    plan = crud.describe_deletion(db, manual_set, version or None)
    if not plan["found"]:
        die(f"{ref} not found ([cyan]cxr ls manual-sets[/] lists what exists)")

    console.print(
        table(
            f"About to delete [bold]{manual_set}[/]",
            [
                "version",
                "images",
                "cls",
                "det",
                "targets",
                "steps",
                "built by",
                "created",
            ],
            [
                [
                    v["version"],
                    v["images"],
                    v["cls"],
                    v["det"],
                    v["targets"],
                    v["steps"] if v["steps"] is not None else "—",
                    v["created_by"],
                    str(v["created_at"])[:19],
                ]
                for v in plan["versions"]
            ],
        )
    )
    with_spec = [v for v in plan["versions"] if v["spec_key"]]
    if with_spec:
        console.print(
            f"  [yellow]⚠[/] {len(with_spec)} spec.yaml files will be deleted from "
            "object storage too — after that these datasets can never be reproduced. "
            "To keep them, run first:"
        )
        for v in with_spec:
            console.print(
                f"      [dim]cxr spec {manual_set}@{v['version']} "
                f"-o {manual_set}_{v['version']}.yaml[/]"
            )
    if plan["removes_manual_set"]:
        console.print(
            f"  [dim]this deletes every version of {manual_set}, and the name itself[/]"
        )

    if not yes:
        if not sys.stdin.isatty():
            die("Refusing to delete without a terminal to confirm. Add --yes if you mean it.")
        if not typer.confirm("Delete?"):
            console.print("  [dim]cancelled[/]")
            raise typer.Abort()

    from cxr_dataset_manager.storage import get_store, meta_location

    # The database goes first. Deleting the objects first would, if the database
    # then failed, leave a version pointing at a spec that cannot be read.
    crud.delete_manual_set(db, manual_set, version or None)
    leftovers = []
    for v in plan["versions"]:
        try:
            if v["spec_key"]:
                get_store().delete_spec(manual_set, v["version"])
            get_store().delete_meta("manual-set", manual_set, v["version"])
        except Exception as exc:  # objects failing to go must not make the deletion look failed
            bucket, key = meta_location("manual-set", manual_set, v["version"])
            leftovers.append(f"{v['spec_key'] or ''} {bucket}/{key} ({exc})")
    for leftover in leftovers:
        console.print(f"  [yellow]⚠[/] could not delete {leftover}; remove it by hand")
    console.print(
        f"[green]✓[/] deleted {len(plan['versions'])} versions"
        + (" (and the manual-set itself)" if plan["removes_manual_set"] else "")
    )
    console.print("  [dim]the original images and annotations are untouched[/]")


@app.command()
def export(
    ref: str = typer.Argument(..., help="manual-set@version"),
    out: Path = typer.Option(Path("."), "--out", "-o", help="Output directory"),
    fmt: str = typer.Option(
        "zip", "--format", "-f", help="zip | coco | csv | parquet"
    ),
):
    """Export to training formats (COCO json / manifest csv / packed zip / manual-set parquet).

    parquet writes the standard manual-set layout — images, cls_annotations,
    det_annotations, categories and annotators .parquet plus the version's
    __meta__.md — into <out>/manual-sets/<name>/annotations/<version>/, so
    `-o ChestDatasetsRoot` puts it where the dataset format expects it.
    """
    if fmt not in EXPORT_FORMATS:
        die(f"unknown format {fmt!r} (use {', '.join(EXPORT_FORMATS)})")
    db = new_session()
    version_id = resolve_version_id(db, ref)

    if fmt == "parquet":
        written = export_mod.to_parquet(db, version_id, out)
        console.print(f"[green]✓[/] exported to {written['dir']}", soft_wrap=True)
        for name, n in written["rows"].items():
            console.print(f"  {name}.parquet  {n:,} rows")
        if not written["meta"]:
            console.print(
                f"  [yellow]⚠[/] {ref} has no __meta__.md in object storage, so none "
                f"was exported; write one with [cyan]cxr meta manual-set {ref}[/]"
            )
        return

    out.mkdir(parents=True, exist_ok=True)
    stem = ref.replace("@", "_")
    if fmt == "coco":
        path = out / f"{stem}_coco.json"
        path.write_text(
            json.dumps(export_mod.to_coco(db, version_id), indent=2, default=str)
        )
    elif fmt == "csv":
        path = out / f"{stem}_manifest.csv"
        path.write_text(export_mod.to_manifest_csv(db, version_id))
    else:
        path = out / f"{stem}.zip"
        path.write_bytes(export_mod.to_zip(db, version_id))
    console.print(f"[green]✓[/] exported {path}  ({path.stat().st_size:,} bytes)")


@app.command()
def explore(
    name: str = typer.Argument(
        "untitled",
        help="Name of this exploration; also the default manual-set name at commit",
    ),
):
    """Start a interactive exploration session.

    Exploration state only lives in the memory of this process, gone at leaving.
    To keep it, please use `save` to save it as spec file or `commit` to generate official version.
    """
    from cxr_dataset_manager.cli import repl

    repl.run(name)


# ---------------------------------------------------------------------------
# __meta__.md
# ---------------------------------------------------------------------------


def _write_meta(
    kind: str,
    name: str,
    version: str,
    view: bool,
    yes: bool,
    render: Callable[[], str],
) -> None:
    """Show, or (re)write, one __meta__.md. `render` asks and returns the text."""
    from cxr_dataset_manager.storage import get_store

    store = get_store()
    ref = f"{name}@{version}"
    existing = store.get_meta(kind, name, version)  # type: ignore[arg-type]
    if view:
        if existing is None:
            die(f"{ref} has no __meta__.md yet (write one with `cxr meta {kind} {ref}`)")
        typer.echo(existing, nl=False)
        return
    if existing is not None and not yes:
        if not interactive():
            die(
                f"{ref} already has a __meta__.md; add --yes to replace it "
                "(--view shows it)"
            )
        if not typer.confirm(
            f"{ref} already has a __meta__.md. Replace it?", default=False
        ):
            raise typer.Abort()
    location = store.put_meta(kind, name, version, render())  # type: ignore[arg-type]
    console.print(f"[green]✓[/] wrote {location}", soft_wrap=True)


VIEW_OPTION = typer.Option(
    False, "--view", help="Show the current __meta__.md instead of writing one"
)
YES_OPTION = typer.Option(
    False, "--yes", "-y", help="Replace an existing __meta__.md without asking"
)


@meta_app.command("images")
def meta_images(
    ref: str = typer.Argument(..., help="original-set@version of an image batch"),
    sample: int = typer.Option(
        50,
        "--sample",
        help="How many images to read for the data type, spread across the batch; 0 reads all",
    ),
    view: bool = VIEW_OPTION,
    yes: bool = YES_OPTION,
):
    """Write the __meta__.md of an image batch (run after import_image_batch.py).

    The number of images, file extension, data type and resolution are read
    from the batch; the rest is asked. Stored at
    original-sets/<set>/images/<version>/__meta__.md.
    """
    db = new_session()
    name, version = parse_ref(ref)
    batch = crud.batch_id(db, "image", name, version)
    if batch is None:
        die(f"image batch {ref} not found ([cyan]cxr ls batches[/] lists them)")

    def render() -> str:
        stats = crud.image_batch_stats(db, batch, sample=sample)
        if stats["unreadable"]:
            console.print(
                f"  [yellow]⚠[/] {len(stats['unreadable'])} images could not be read "
                f"from object storage (e.g. {stats['unreadable'][0]}); "
                "the data type comes from the rest"
            )
        if set(stats["extensions"]) != {".png"} or set(stats["dtypes"]) - {"uint16"}:
            console.print(
                "  [yellow]⚠[/] the dataset format stores images as uint16 .png; found "
                f"{', '.join(stats['extensions'])} / {', '.join(stats['dtypes']) or '?'}"
            )
        return meta_prompts.images_markdown(stats)

    _write_meta("images", name, version, view, yes, render)


@meta_app.command("annotations")
def meta_annotations(
    ref: str = typer.Argument(..., help="original-set@version of an annotation batch"),
    view: bool = VIEW_OPTION,
    yes: bool = YES_OPTION,
):
    """Write the __meta__.md of an annotation batch (run after import_annotation_batch.py).

    Statistics and the class distribution are counted from the batch; the rest
    is asked. Stored at original-sets/<set>/annotations/<version>/__meta__.md.
    """
    db = new_session()
    name, version = parse_ref(ref)
    batch = crud.batch_id(db, "annotation", name, version)
    if batch is None:
        die(f"annotation batch {ref} not found ([cyan]cxr ls batches[/] lists them)")
    _write_meta(
        "annotations",
        name,
        version,
        view,
        yes,
        lambda: meta_prompts.annotations_markdown(crud.annotation_batch_stats(db, batch)),
    )


@meta_app.command("manual-set")
def meta_manual_set(
    ref: str = typer.Argument(..., help="manual-set@version"),
    view: bool = VIEW_OPTION,
    yes: bool = YES_OPTION,
):
    """Write the __meta__.md of a manual-set version.

    Committing a version already writes one; use this to fill it in when the
    commit had no terminal to ask on, or to correct it. Stored beside the
    version's spec.yaml.
    """
    db = new_session()
    version_id = resolve_version_id(db, ref)
    name, version = parse_ref(ref)
    _write_meta(
        "manual-set",
        name,
        version,
        view,
        yes,
        lambda: meta_prompts.manual_set_markdown(crud.version_summary(db, version_id)),
    )


def main() -> None:
    """Entrypoint for the CLI.

    Collapse the exceptions into a single line message——users want to know "where they went wrong",
    not SQLAlchemy's traceback. Set CXR_DEBUG=1 during debugging to see the full stack trace.
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
        console.print("[dim](set CXR_DEBUG=1 for the full traceback)[/]")
        sys.exit(1)


if __name__ == "__main__":
    main()
