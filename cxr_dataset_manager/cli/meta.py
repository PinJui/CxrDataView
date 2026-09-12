"""Asking for the parts of a __meta__.md that only a person knows (Layer 4).

The templates live in core/meta.py and everything countable comes from
db/crud.py; this module only asks. Without a terminal nothing can be asked,
so every answer takes its default (N/A for most) and the statistics are
written anyway — `cxr meta ... --yes` replaces the file later.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import typer
from rich.markup import escape
from sqlalchemy.orm import Session

from cxr_dataset_manager.cli._common import console, interactive
from cxr_dataset_manager.core import meta
from cxr_dataset_manager.db import crud
from cxr_dataset_manager.settings import settings

HOW_TO_ANSWER = (
    "[dim]An answer may span several lines; an empty line ends it. "
    "Pressing Enter straight away keeps the default.[/]"
)


def ask(question: meta.Question) -> str:
    """One answer, possibly several lines long."""
    if not interactive():
        return question.default
    console.print(f"\n[bold]{escape(question.label)}[/]")
    default = escape(question.default).replace("\n", "\n           ")
    console.print(f"  [dim]default: {default}[/]", soft_wrap=True)
    lines: list[str] = []
    while True:
        try:
            line = input("  > ")
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines) if lines else question.default


def ask_all(questions: Iterable[meta.Question]) -> dict[str, str]:
    if interactive():
        console.print(HOW_TO_ANSWER)
    return {q.key: ask(q) for q in questions}


def _builder() -> str | None:
    if settings.author_name and settings.author_email:
        return f"{settings.author_name} <{settings.author_email}>"
    return None


def _today() -> str:
    return dt.date.today().strftime("%Y/%m/%d")


def images_markdown(stats: Mapping[str, Any]) -> str:
    answers = ask_all(meta.original_set_questions("these images", _builder(), _today()))
    processed = interactive() and typer.confirm(
        "\nWere these images processed (resized, normalized, ...) rather than RAW?",
        default=False,
    )
    if processed:
        answers["process_procedure"] = ask(
            meta.Question(
                "process_procedure",
                "Process Procedure (describe how the images were processed)",
            )
        )
    answers["additional"] = ask(
        meta.Question(
            "additional",
            "Additional Information (extra notes)",
            meta.resolution_note(stats["sizes"]),
        )
    )
    return meta.render_images(stats, answers)


def annotations_markdown(stats: Mapping[str, Any]) -> str:
    questions = [
        *meta.original_set_questions("this annotation version", _builder(), _today()),
        *meta.ANNOTATION_DIFF_QUESTIONS,
    ]
    return meta.render_annotations(stats, ask_all(questions))


def manual_set_markdown(summary: Mapping[str, Any]) -> str:
    questions = [
        *meta.MANUAL_SET_QUESTIONS,
        *(meta.selection_rule_question(c) for c in summary["composition"]),
        meta.MANUAL_SET_ADDITIONAL,
    ]
    return meta.render_manual_set(summary, ask_all(questions))


def on_commit(db: Session) -> Callable[[int], str]:
    """What `build()` calls once the selection is written and before it commits.

    The statistics are final by then, and a build that fails earlier — a
    mapping gap, an unresolved conflict — has asked nothing.
    """

    def write(version_id: int) -> str:
        summary = crud.version_summary(db, version_id)
        if interactive():
            console.print(
                f"\n[bold cyan]__meta__.md for {summary['manual_set']}@{summary['version']}[/]"
                f"  [dim]{summary['images']} images · {summary['cls']} cls · "
                f"{summary['det']} det[/]"
            )
        return manual_set_markdown(summary)

    return write
