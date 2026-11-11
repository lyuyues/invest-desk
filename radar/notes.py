"""Personal notes (/notes): an accumulating log of things learned and thoughts on earnings reports / news.

Unlike theses (one rewritable text per ticker) these pile up. A note may point at an earnings report
(ticker + earnings_date → earnings_notes). Deletes are soft (``deleted`` timestamp), so they can be undone.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta

from radar.db import _ts

KINDS = ("学习", "财报", "新闻", "其他")
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,14}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_BODY, MAX_TITLE, MAX_URL = 20000, 200, 1000


def clean(kind: str, body: str, ticker: str | None = None, title: str | None = None,
          source_url: str | None = None, earnings_date: str | None = None) -> dict:
    """Validate form input → column values. Raises ValueError with a Chinese message."""
    kind = (kind or "").strip()
    if kind not in KINDS:
        raise ValueError("类型不对")
    body = (body or "").strip()
    if not body:
        raise ValueError("内容不能为空")
    if len(body) > MAX_BODY:
        raise ValueError(f"内容太长（上限 {MAX_BODY} 字）")
    ticker = (ticker or "").strip().upper() or None
    if ticker and not TICKER_RE.match(ticker):
        raise ValueError("标的代码格式不对（如 MSFT、BRK-B）")
    title = (title or "").strip()[:MAX_TITLE] or None
    source_url = (source_url or "").strip() or None
    if source_url and (not re.match(r"^https?://\S+$", source_url) or len(source_url) > MAX_URL):
        raise ValueError("链接要以 http:// 或 https:// 开头")
    earnings_date = (earnings_date or "").strip() or None
    if earnings_date and (not DATE_RE.match(earnings_date) or not ticker):
        raise ValueError("财报引用需要标的和日期（YYYY-MM-DD）")
    return {"kind": kind, "body": body, "ticker": ticker, "title": title, "source_url": source_url,
            "earnings_date": earnings_date}


def add(conn: sqlite3.Connection, now: datetime, **fields) -> int:
    v = clean(**fields)
    cur = conn.execute(
        "INSERT INTO notes (created, updated, kind, ticker, title, body, source_url, earnings_date) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (_ts(now), _ts(now), v["kind"], v["ticker"], v["title"], v["body"], v["source_url"], v["earnings_date"]))
    conn.commit()
    return cur.lastrowid


def update(conn: sqlite3.Connection, note_id: int, now: datetime, **fields) -> bool:
    v = clean(**fields)
    cur = conn.execute(
        "UPDATE notes SET updated=?, kind=?, ticker=?, title=?, body=?, source_url=?, earnings_date=? "
        "WHERE id=? AND deleted IS NULL",
        (_ts(now), v["kind"], v["ticker"], v["title"], v["body"], v["source_url"], v["earnings_date"], note_id))
    conn.commit()
    return cur.rowcount > 0


def delete(conn: sqlite3.Connection, note_id: int, now: datetime) -> bool:
    cur = conn.execute("UPDATE notes SET deleted=? WHERE id=? AND deleted IS NULL", (_ts(now), note_id))
    conn.commit()
    return cur.rowcount > 0


def restore(conn: sqlite3.Connection, note_id: int) -> bool:
    cur = conn.execute("UPDATE notes SET deleted=NULL WHERE id=? AND deleted IS NOT NULL", (note_id,))
    conn.commit()
    return cur.rowcount > 0


def get(conn: sqlite3.Connection, note_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM notes WHERE id=? AND deleted IS NULL", (note_id,)).fetchone()


def list_notes(conn: sqlite3.Connection, ticker: str | None = None, kind: str | None = None,
               limit: int = 200) -> list[sqlite3.Row]:
    """Newest first, live notes only."""
    where, args = ["deleted IS NULL"], []
    if ticker:
        where.append("ticker=?")
        args.append(ticker.upper())
    if kind:
        where.append("kind=?")
        args.append(kind)
    return conn.execute(f"SELECT * FROM notes WHERE {' AND '.join(where)} ORDER BY created DESC, id DESC LIMIT ?",
                        (*args, limit)).fetchall()


def counts_by_ticker(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["ticker"]: r["n"] for r in conn.execute(
        "SELECT ticker, COUNT(*) n FROM notes WHERE deleted IS NULL AND ticker IS NOT NULL GROUP BY ticker")}


def recent_by_ticker(conn: sqlite3.Connection, tickers: list[str], per: int = 3) -> dict[str, list[sqlite3.Row]]:
    """Newest few notes per ticker (for the compact lists on 逻辑 / 财报)."""
    out: dict[str, list[sqlite3.Row]] = {}
    for t in tickers:
        rows = list_notes(conn, ticker=t, limit=per)
        if rows:
            out[t] = rows
    return out


def by_report(conn: sqlite3.Connection) -> dict[tuple[str, str], list[sqlite3.Row]]:
    """Notes attached to an earnings report, keyed (ticker, date), oldest first."""
    out: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in conn.execute("SELECT * FROM notes WHERE deleted IS NULL AND earnings_date IS NOT NULL "
                          "ORDER BY created, id"):
        out.setdefault((r["ticker"], r["earnings_date"]), []).append(r)
    return out


def snippet(body: str, n: int = 80) -> str:
    """One-line plain preview of a markdown body."""
    s = re.sub(r"[#>*`_\[\]]+", "", body)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n] + "…"


def _line(r: sqlite3.Row, chars: int) -> str:
    head = f"- {r['created'][:10]} [{r['kind']}]"
    if r["ticker"]:
        head += f" {r['ticker']}"
    if r["earnings_date"]:
        head += f"（{r['earnings_date']} 财报）"
    if r["title"]:
        head += f" {r['title']}"
    body = re.sub(r"\s+", " ", r["body"]).strip()
    body = body if len(body) <= chars else body[:chars] + " …"
    return f"{head}：{body}" + (f" {r['source_url']}" if r["source_url"] else "")


def for_prompt(conn: sqlite3.Connection, now: datetime, days: int = 90, per_ticker: int = 5,
               general: int = 10, chars: int = 400, total: int = 60, ticker: str | None = None) -> list[str]:
    """Bounded recent notes for Claude: last `days`, at most `per_ticker` per ticker, `general` without a
    ticker and `total` overall, newest first; each body cut to `chars`."""
    rows = conn.execute("SELECT * FROM notes WHERE deleted IS NULL AND created>=? "
                        + ("AND ticker=? " if ticker else "") + "ORDER BY created DESC, id DESC LIMIT 2000",
                        (_ts(now - timedelta(days=days)), *([ticker.upper()] if ticker else []))).fetchall()
    seen: dict[str | None, int] = {}
    keep = []
    for r in rows:
        k = r["ticker"]
        cap = per_ticker if k else general
        if len(keep) >= total:
            break
        if seen.get(k, 0) < cap:
            seen[k] = seen.get(k, 0) + 1
            keep.append(r)
    return [_line(r, chars) for r in keep]
