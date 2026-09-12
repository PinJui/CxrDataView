"""What both terminal interfaces share (Layer 4).

`cxr` and `cxr explore` print with the same table, fail with the same message,
and resolve the same refs. Keeping that here rather than in main.py is what
lets repl.py stay independent of it: the import used to run the other way,
inside a function, purely to dodge the import cycle.
"""

from __future__ import annotations

import sys
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from cxr_dataset_manager.core.engine import Author
from cxr_dataset_manager.core.types import SpecError
from cxr_dataset_manager.db import crud
from cxr_dataset_manager.settings import settings

console = Console()
# Errors go to stderr, apart from normal output, so piping into grep does not mix them.
err_console = Console(stderr=True)


def interactive() -> bool:
    """Whether there is anybody to ask; scripts and CI take the defaults."""
    return sys.stdin.isatty()


def table(title: str, columns: list[str], rows: list[list[Any]]) -> Table:
    rendered = Table(title=title, header_style="bold cyan", title_justify="left")
    for column in columns:
        rendered.add_column(column)
    for row in rows:
        rendered.add_row(*["" if c is None else str(c) for c in row])
    return rendered


def die(message: str) -> None:
    err_console.print(f"[bold red]✗[/] {message}")
    raise typer.Exit(1)


def parse_ref(ref: str) -> tuple[str, str]:
    try:
        return crud.parse_ref(ref)
    except SpecError as exc:
        die(str(exc))
        raise


def resolve_version_id(db, ref: str) -> int:
    """`name@version` → manual_set_version_id, or exit saying what is wrong.

    Every query command does the same thing; one function means none of them
    forgets the check.
    """
    name, version = parse_ref(ref)
    version_id = crud.resolve_version(db, name, version)
    if version_id is None:
        die(f"{ref} not found ([cyan]cxr ls manual-sets[/] lists what exists)")
    return version_id


def resolve_author(name: str | None, email: str | None) -> Author:
    """Who is building this dataset: command flags → .env → ask.

    When nothing can be asked (a script, CI) fail and say how to set it —
    better than a dataset nobody knows the builder of.
    """

    name = name or settings.author_name
    email = email or settings.author_email
    if not name or not email:
        if not interactive():
            die(
                "Who is building this dataset? Pass --author-name / --author-email, "
                "or set CXR_AUTHOR_NAME and CXR_AUTHOR_EMAIL in .env."
            )
        console.print(
            "[dim]The dataset records who built it "
            "(set CXR_AUTHOR_NAME / CXR_AUTHOR_EMAIL to skip this question)[/]"
        )
        name = name or typer.prompt("Your name")
        email = email or typer.prompt("Your email")
    try:
        return Author(name=name, email=email)
    except SpecError as exc:
        die(str(exc))
        raise
