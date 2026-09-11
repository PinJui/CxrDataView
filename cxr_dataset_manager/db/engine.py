"""DB engine / session 建立，以及 schema 的套用。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from cxr_dataset_manager.settings import settings

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_FILE = REPO_ROOT / "db" / "schema.sql"

_engine: Engine | None = None


def get_engine(url: str | None = None, echo: bool = False) -> Engine:
    global _engine
    if url is not None:
        return create_engine(url, echo=echo, future=True)
    if _engine is None:
        _engine = create_engine(
            settings.database_url,
            echo=echo,
            future=True,
            pool_pre_ping=True,
            # 預設 5+10 條連線在並發查詢下會不夠用
            pool_size=20,
            max_overflow=20,
            pool_timeout=10,
        )
    return _engine


def session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)


def new_session(url: str | None = None) -> Session:
    return session_factory(url)()


def apply_schema(engine: Engine, drop_first: bool = False) -> None:
    """Apply db/schema.sql verbatim.

    The .sql file is executed directly rather than going through
    Base.metadata.create_all(): it is the single source of truth for the data
    model, and it contains things the ORM cannot express — notably the
    deferred constraint trigger that forbids dangling categories.
    """
    with engine.begin() as conn:
        if drop_first:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        conn.execute(text(SCHEMA_FILE.read_text()))


def get_db() -> Iterator[Session]:
    """用完一定把連線還回 pool 的 context helper。

    少了這個 finally，一批並發查詢就會把連線池抽乾，看起來像整個程式當掉。
    """
    db = new_session()
    try:
        yield db
    finally:
        db.close()
