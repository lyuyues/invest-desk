"""After discovery: run the DCF engine on the top candidates and have Claude write a one-line
"why look / main risk" for each."""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime

from radar import db, valuation
from radar.weekly import Runner, _fmt

log = logging.getLogger(__name__)

PROMPT = """You are the user’s investment research assistant. Below are this week’s candidate stocks found by Invest Radar (not yet tracked by the user), each with the channels it hit and a DCF valuation.
Write a one-sentence comment for each. You may use web search to quickly check the latest news (only what is necessary; don’t go deep). Do not make up numbers; do not give buy/sell instructions.

Follow this format strictly, one line per stock, nothing else:
Ticker | Why it’s worth a look (≤40 words) | Main risk (≤30 words)

User’s investment principles:
{principles}

# Candidates
{cands}"""

LINE = re.compile(r"^\s*([A-Z][A-Z0-9.\-]{0,9})\s*[||]\s*(.+?)\s*[||]\s*(.+?)\s*$", re.M)


def pick(rows: list[dict], tracked: set[str], n: int) -> list[dict]:
    """Highest-scoring untracked candidates (rows are already sorted by score)."""
    return [r for r in rows if r["ticker"] not in tracked][:n]


def build_prompt(conn: sqlite3.Connection, cands: list[dict], val: dict[str, dict]) -> str:
    th = conn.execute("SELECT body FROM theses WHERE ticker='_GENERAL'").fetchone()
    L = []
    for c in cands:
        v = val.get(c["ticker"]) or {}
        notes = "; ".join(n for ns in c["detail"].values() for n in ns[:3])
        fair = v.get("auto")
        L.append(f"- {c['ticker']} (channels: {'+'.join(c['channels'])}): {notes[:400]}."
                 f"Price {_fmt(v.get('price'))}, P/E {_fmt(v.get('pe'), '{:.1f}')}, "
                 + (f"DCF fair value {fair:.2f} (upside {_fmt(v.get('upside'), '{:+.0f}%')}, FCF margin {v['fcf_margin']:.0%})"
                    if fair and fair > 0 else f"DCF: {v.get('error') or 'N/A'}"))
    return PROMPT.format(principles=th["body"] if th else "(not filled in)", cands="\n".join(L))


def parse(text: str) -> dict[str, tuple[str, str]]:
    return {m.group(1): (m.group(2), m.group(3)) for m in LINE.finditer(text)}


def run(conn: sqlite3.Connection, run_date: str, rows: list[dict], tracked: set[str], n: int,
        now: datetime, runner: Runner | None, fetch_quotes=None, builder=None) -> dict:
    cands = pick(rows, tracked, n)
    if not cands:
        return {"valued": 0, "reviewed": 0}
    tickers = [c["ticker"] for c in cands]
    if fetch_quotes is not None:  # price + P/E for upside (untracked tickers have no quotes yet)
        for q in fetch_quotes(tickers, now):
            db.insert_quote(conn, q)
        conn.commit()
    res = valuation.refresh(conn, tickers, now, **({"builder": builder} if builder else {}))
    val = {r["ticker"]: r for r in valuation.summary(conn, tickers)}
    reviewed = 0
    if runner is not None:
        try:
            notes = parse(runner(build_prompt(conn, cands, val)))
            for t, (why, risk) in notes.items():
                if t in tickers:
                    conn.execute("INSERT OR REPLACE INTO candidate_notes VALUES (?,?,?,?)", (run_date, t, why, risk))
                    reviewed += 1
            conn.commit()
        except Exception as e:
            log.warning("candidate review failed: %s", e)
    return {"valued": sum(v == "ok" for v in res.values()), "reviewed": reviewed}
