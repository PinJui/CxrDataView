"""測試直接跑在開發用的 Postgres 上（唯讀為主，寫入的部分自己清乾淨）。

不用 SQLite：schema 裡的 deferred constraint trigger、複合外鍵、ARRAY／JSONB
都是 Postgres 專屬的，換成 SQLite 測到的就不是真正會上線的那套約束。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from cxr_dataset_manager.core.types import Catalog
from cxr_dataset_manager.db.engine import new_session


@pytest.fixture(scope="session")
def db():
    session = new_session()
    try:
        session.execute(text("SELECT 1 FROM original_sets LIMIT 1")).all()
    except Exception:  # pragma: no cover
        pytest.skip("開發資料庫沒跑或還沒 seed，先執行 cxr db reset")
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _clean_transaction(db):
    """每個測試結束後 rollback。

    db 是 session 級的，少了這個，任何一個測試把 transaction 弄成 aborted，
    後面所有測試都會跟著失敗——一個根因變成幾十個紅字，很難查。
    """
    yield
    db.rollback()


@pytest.fixture
def catalog(db):
    return Catalog(db)
