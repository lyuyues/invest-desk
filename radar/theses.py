"""Investment theses, editable on the dashboard (/theses). Seeded with starter text marked as default."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from radar.db import _ts

GENERAL = "_GENERAL"

_T = "Buy thesis: {why}\nKey metrics: {watch}\nThesis breakers (consider trimming): {breaks}"

DEFAULTS: dict[str, str] = {
    GENERAL: "Hold quality companies for the long term.\n"
             "- No single stock above 15% of the total portfolio; cap tech/semiconductors combined.\n"
             "- After earnings, first check whether guidance and the thesis changed; don’t trade on a single day’s move.\n"
             "- Before adding, write down the reason and the exit conditions.",
}

TEMPLATE = _T.format(why="(why buy / watch)", watch="(which data to watch)", breaks="(when the thesis no longer holds)")


def seed(conn: sqlite3.Connection, tickers: list[str], now: datetime) -> None:
    """Fill in starter text for tickers without a thesis. Never overwrites."""
    for t in [GENERAL, *tickers]:
        conn.execute("INSERT OR IGNORE INTO theses VALUES (?,?,?,1)", (t, DEFAULTS.get(t, TEMPLATE), _ts(now)))
    conn.commit()


def save(conn: sqlite3.Connection, ticker: str, body: str, now: datetime) -> None:
    conn.execute("INSERT OR REPLACE INTO theses VALUES (?,?,?,0)", (ticker, body.strip()[:5000], _ts(now)))
    conn.commit()


def all_theses(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["ticker"]: r for r in conn.execute("SELECT * FROM theses")}


def for_prompt(conn: sqlite3.Connection, tickers: list[str]) -> list[str]:
    th = all_theses(conn)
    out = []
    for t in [GENERAL, *tickers]:
        r = th.get(t)
        if r is None or r["body"] == TEMPLATE:
            continue
        name = "General principles" if t == GENERAL else t
        out.append(f"### {name}{' (default, not yet edited by the user)' if r['is_default'] else ''}\n{r['body']}")
    return out
