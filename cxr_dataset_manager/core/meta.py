"""`__meta__.md` — the human-readable description beside every batch and version.

One template per kind of folder in the dataset layout:

    original-sets/{name}/images/{version}/__meta__.md        render_images()
    original-sets/{name}/annotations/{version}/__meta__.md   render_annotations()
    manual-sets/{name}/annotations/{version}/__meta__.md     render_manual_set()

Everything countable comes from the database (`stats`); only what a person
knows — who built it, why, what changed — is asked (`answers`). Rendering is
pure: the CLI decides how to ask, and tests can check the text.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

NA = "N/A"


@dataclass(frozen=True)
class Question:
    key: str
    label: str
    default: str = NA


def original_set_questions(
    subject: str, builder: str | None, today: str
) -> list[Question]:
    """General Information and Conversion Material, shared by the images and the
    annotations template. `subject` says what was converted ("these images")."""
    return [
        Question("builder", "Builder (Name <email>)", builder or NA),
        Question("build_date", "Build Date (yyyy/mm/dd)", today),
        Question(
            "dataset_description",
            "Dataset description (overall purpose/source of this dataset)",
        ),
        Question(
            "version_description",
            "Version description (what this specific version represents)",
        ),
        Question(
            "file_naming_convention",
            "File naming convention (how image files are named in this version)",
        ),
        Question(
            "conversion_data",
            f"Conversion Material — Data (source data used to build {subject})",
        ),
        Question(
            "conversion_code",
            "Conversion Material — Code (repository/script used for the conversion)",
        ),
    ]


ANNOTATION_DIFF_QUESTIONS = [
    Question("diff_cls", "Major Difference with Parent Version — cls", "No changes."),
    Question("diff_det", "Major Difference with Parent Version — det", "No changes."),
    Question(
        "diff_images", "Major Difference with Parent Version — images", "No changes."
    ),
]

MANUAL_SET_QUESTIONS = [
    Question("description", "Description (purpose/source of this manual-set version)"),
    Question("diff_cls", "Major Difference with Previous Version — cls", "No changes."),
    Question("diff_det", "Major Difference with Previous Version — det", "No changes."),
]

MANUAL_SET_ADDITIONAL = Question(
    "additional",
    "Additional Information (extra notes/warnings for using this manual-set)",
)


def selection_rule_key(original_set: str, version: str) -> str:
    return f"rule:{original_set}@{version}"


def selection_rule_question(source: Mapping[str, Any]) -> Question:
    """One per row of the Composition of Datasets table."""
    return Question(
        selection_rule_key(source["original_set"], source["version"]),
        f"Selection rule for {source['original_set']}@{source['version']} "
        f"({source['images']} images) — how were these images chosen?",
        "See spec.yaml",
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def sanitize(text: str | None) -> str:
    """Tabs become four spaces, trailing spaces and blank edge lines go; empty is N/A."""
    if not text:
        return NA
    lines = text.replace("\t", "    ").splitlines()
    return "\n".join(line.rstrip() for line in lines).strip() or NA


def field(label: str, text: str | None) -> str:
    """`- **label:**` with the answer indented beneath it, so an answer of
    several lines stays inside its list item."""
    text = sanitize(text)
    if text == NA:
        return f"- **{label}:** {NA}"
    body = [f"    {line}" if line else "" for line in text.splitlines()]
    return "\n".join([f"- **{label}:**", *body])


def table_cell(text: str | None) -> str:
    """A raw newline would end the table row, so lines are joined with <br>."""
    lines = [line.strip() for line in sanitize(text).splitlines() if line.strip()]
    return "<br>".join(lines) or NA


def _table(header: list[str], rows: list[list[Any]], empty: list[Any]) -> str:
    def row(cells: list[Any]) -> str:
        return "| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |"

    lines = [row(header), "| " + " | ".join([":---"] * len(header)) + " |"]
    lines += [row(r) for r in rows or [empty]]
    return "\n".join(lines)


def _describe(counts: Mapping[str, int]) -> str:
    """`.png`, or `mixed: .png ×900, .jpg ×12` — never silently the majority."""
    if not counts:
        return NA
    if len(counts) == 1:
        return f"`{next(iter(counts))}`"
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return "mixed: " + ", ".join(f"`{value}` ×{n}" for value, n in ranked)


def category_ids(names: Iterable[str]) -> dict[str, int]:
    """1-based ids in name order. Every export of a manual-set version and its
    __meta__.md number target categories this way, so their ID columns agree."""
    return {name: i for i, name in enumerate(sorted(set(names)), start=1)}


def resolution_note(sizes: Mapping[tuple[int, int], int]) -> str:
    """Default text for an image batch's Additional Information."""
    if not sizes:
        return "Could not determine image resolution."
    if len(sizes) == 1:
        ((w, h),) = sizes
        return f"All the images here have a resolution of {w} by {h}."
    top = sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    lines = "\n".join(f"- {w}x{h}: {n} images" for (w, h), n in top)
    more = f"\n- ... and {len(sizes) - 5} more distinct sizes" if len(sizes) > 5 else ""
    return "Resolution varies across images. Most common sizes:\n" + lines + more


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_HEADER_BYTES = 25


def png_dtype(head: bytes) -> str | None:
    """The data type a PNG decodes to, read from its IHDR chunk alone.

    Byte 24 is the bit depth: 16 decodes to uint16, anything lower to uint8.
    Reading 25 bytes instead of the whole film is what makes checking every
    image of a batch affordable.
    """
    if len(head) < PNG_HEADER_BYTES or not head.startswith(PNG_SIGNATURE):
        return None
    return "uint16" if head[24] == 16 else "uint8"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def _general(answers: Mapping[str, str]) -> list[str]:
    return [
        field("Builder", answers.get("builder")),
        field("Build Date", answers.get("build_date")),
        field("Dataset description", answers.get("dataset_description")),
        field("Version description", answers.get("version_description")),
        field("File naming convention", answers.get("file_naming_convention")),
    ]


def _conversion(answers: Mapping[str, str]) -> list[str]:
    return [
        "## Conversion Material",
        field("Data", answers.get("conversion_data")),
        field("Code", answers.get("conversion_code")),
    ]


def _class_distribution(rows: list[Mapping[str, Any]]) -> list[str]:
    """rows: id, name, cls_pos, cls_neg, cls_unknown, det_boxes — sorted by name."""
    rows = sorted(rows, key=lambda r: (r["name"], r["id"]))
    return [
        "## Class Distribution",
        "### Classification (`cls`)",
        _table(
            ["Class Name", "ID", "Positive Cases", "Negative Cases", "Unknown Cases"],
            [
                [r["name"], r["id"], r["cls_pos"], r["cls_neg"], r["cls_unknown"]]
                for r in rows
            ],
            [NA, NA, 0, 0, 0],
        ),
        "",
        "### Detection (`det`)",
        _table(
            ["Class Name", "ID", "Positive Boxes"],
            [[r["name"], r["id"], r["det_boxes"]] for r in rows],
            [NA, NA, 0],
        ),
    ]


def render_images(stats: Mapping[str, Any], answers: Mapping[str, str]) -> str:
    """stats: images, extensions {ext: n}, dtypes {dtype: n}, dtype_checked."""
    dtype = _describe(stats["dtypes"])
    if stats["dtypes"] and stats["dtype_checked"] < stats["images"]:
        dtype += f" (read from {stats['dtype_checked']} of {stats['images']} images)"
    procedure = sanitize(answers.get("process_procedure"))
    processed = procedure != NA

    parts = [
        "## General Information",
        *_general(answers),
        "",
        "## Image Specifications",
        _table(
            ["Property", "Value"],
            [
                ["**Type**", "Processed" if processed else "RAW"],
                ["**File Extension**", _describe(stats["extensions"])],
                ["**Data Type**", dtype],
                ["**Number of Images**", stats["images"]],
            ],
            [],
        ),
        "",
        *_conversion(answers),
        "",
    ]
    if processed:
        parts += ["## Process Procedure (Optional)", procedure, ""]
    parts += ["## Additional Information", sanitize(answers.get("additional"))]
    return "\n".join(parts) + "\n"


def render_annotations(stats: Mapping[str, Any], answers: Mapping[str, str]) -> str:
    """stats: images, categories, annotators, licenses, cls, det, distribution."""
    parts = [
        "## General Information",
        *_general(answers),
        "",
        *_conversion(answers),
        "",
        "## Major Difference with Parent Version",
        field("Classification (`cls`)", answers.get("diff_cls")),
        field("Detection (`det`)", answers.get("diff_det")),
        field("Images", answers.get("diff_images")),
        "",
        "## Statistics",
        _table(
            ["Metric", "Count"],
            [
                ["**Images**", stats["images"]],
                ["**Categories**", stats["categories"]],
                ["**Annotators**", stats["annotators"]],
                ["**Licenses**", stats["licenses"]],
                ["**Annotations (cls)**", stats["cls"]],
                ["**Annotations (det)**", stats["det"]],
            ],
            [],
        ),
        "",
        *_class_distribution(stats["distribution"]),
    ]
    return "\n".join(parts) + "\n"


def render_manual_set(summary: Mapping[str, Any], answers: Mapping[str, str]) -> str:
    """summary: what `crud.version_summary` returns for the version."""
    distribution = summary["category_distribution"]
    ids = category_ids(r["target"] for r in distribution)
    rows = [
        {
            "id": ids[r["target"]],
            "name": r["target"],
            "cls_pos": r["cls_pos"],
            "cls_neg": r["cls_neg"],
            "cls_unknown": r["cls_unknown"],
            "det_boxes": r["det_all"],
        }
        for r in distribution
    ]
    composition = [
        [
            c["original_set"],
            c["version"],
            table_cell(
                answers.get(selection_rule_key(c["original_set"], c["version"]))
            ),
        ]
        for c in summary["composition"]
    ]

    parts = [
        "## General Information",
        field("Creator", summary["created_by"]),
        field("Description", answers.get("description")),
        "",
        "## Major Difference with Previous Version",
        field("Classification (`cls`)", answers.get("diff_cls")),
        field("Detection (`det`)", answers.get("diff_det")),
        "",
        "## Statistics",
        _table(
            ["Metric", "Count"],
            [
                ["**Images**", summary["images"]],
                ["**Annotations (cls)**", summary["cls"]],
                ["**Annotations (det)**", summary["det"]],
            ],
            [],
        ),
        "",
        "## Composition of Datasets",
        _table(["Source Dataset", "Version", "Selection Rule"], composition, [NA] * 3),
        "",
        *_class_distribution(rows),
        "",
        "## Additional Information",
        sanitize(answers.get("additional")),
    ]
    return "\n".join(parts) + "\n"
