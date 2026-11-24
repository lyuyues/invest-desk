"""SQLite helper for the ledger (vendored from ai-bookkeeping; separate, private DB file).

The ledger lives in its own file (default data/ledger.db, mode 0600) — never in invest.db,
which is world-readable on the server."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from radar.config import ROOT

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def db_path() -> Path:
    return Path(os.environ.get("LEDGER_DB") or ROOT / "data" / "ledger.db")


def init_db() -> None:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    os.chmod(p, 0o600)


@contextmanager
def connect():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query_all(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple = ()) -> dict | None:
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def execute(sql: str, params: tuple = ()) -> int:
    with connect() as conn:
        return conn.execute(sql, params).lastrowid or 0
