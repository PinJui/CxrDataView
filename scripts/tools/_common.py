"""匯入腳本共用的小工具。

放在 scripts/tools 而不是套件裡：這些是一次性的資料搬遷工具，不是 cxr
執行期會用到的東西，而且它們依賴 pandas / pyarrow（核心刻意不依賴）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# 讓腳本可以直接 python scripts/tools/xxx.py 執行
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rich.console import Console
from rich.table import Table

console = Console()

DATE_PLACEHOLDERS = {None, "", "0000-00-00", "NaT", "nan"}


def die(message: str) -> None:
    console.print(f"[bold red]✗[/] {message}")
    raise SystemExit(1)


def read_parquet(directory: Path, name: str, required: bool = True):
    """讀一個 parquet；required=False 時檔案不存在就回 None。"""
    import pandas as pd

    path = directory / f"{name}.parquet"
    if not path.exists():
        if required:
            die(f"缺少 {path}")
        return None
    return pd.read_parquet(path)


def cell(row, column: str, default: Any = None) -> Any:
    """安全取值：欄位不存在、或值是 NaN/NaT，一律回 default。

    parquet 的缺值表現方式不只一種（NaN、NaT、None、空字串），
    每個呼叫點各寫一次判斷遲早會漏掉其中一種。
    """
    import pandas as pd

    if column not in row.index:
        return default
    value = row[column]
    if value is None or (not isinstance(value, (list, tuple)) and pd.isna(value)):
        return default
    return value


def clean_date(value: Any) -> str | None:
    """規格用 '0000-00-00' 當未知，資料庫用 NULL。"""
    if value is None:
        return None
    text = str(value).strip()
    if text in DATE_PLACEHOLDERS:
        return None
    return text[:10]


def clean_hash(value: Any) -> str | None:
    """blake3_hash 有 CHECK (^[0-9a-f]{64}$)，不合格就當作沒有。

    寧可留 NULL 也不要寫入一個會讓整批 INSERT 失敗的值——
    之後可以用 backfill 腳本補算。
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if len(text) == 64 and all(c in "0123456789abcdef" for c in text):
        return text
    return None


def summary_table(title: str, rows: list[tuple[str, Any]]) -> None:
    table = Table(title=title, header_style="bold cyan", title_justify="left")
    table.add_column("項目")
    table.add_column("數量", justify="right")
    for name, count in rows:
        table.add_row(name, f"{count:,}" if isinstance(count, int) else str(count))
    console.print(table)
