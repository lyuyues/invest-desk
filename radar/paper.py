"""Saved strategies + live paper accounts (forward test, no real money).

A paper account freezes a copy of the strategy spec when it starts, so later edits to the strategy
cannot rewrite its record. The `paper` job walks every active account forward bar by bar over
*completed* daily bars: execute yesterday's pending signal at today's close, then evaluate today's
close for the next signal — exactly what `backtest.simulate` does. Fills are an append-only log.
Prices: the latest bar's close (not dividend-credited); a stock split mid-run would need manual fixing.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from radar import backtest
from radar.models import Fire

NY = ZoneInfo("America/New_York")
CLOSE_DONE = time(16, 30)   # a bar is final after this (New York)

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    spec TEXT NOT NULL,           -- JSON (backtest.validate output)
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    deleted TEXT
);
CREATE TABLE IF NOT EXISTS paper_accounts (
    id INTEGER PRIMARY KEY,
    strategy_id INTEGER,
    name TEXT NOT NULL,
    spec TEXT NOT NULL,           -- frozen copy
    ticker TEXT NOT NULL,
    capital REAL NOT NULL,
    cost REAL NOT NULL,
    start TEXT NOT NULL,          -- first bar processed (YYYY-MM-DD)
    created TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    stopped TEXT,
    cash REAL NOT NULL,
    shares REAL NOT NULL DEFAULT 0,
    entry_px REAL, peak REAL, held INTEGER NOT NULL DEFAULT 0,
    pending TEXT, pending_why TEXT,
    hold_px REAL,                 -- buy-and-hold benchmark entry (first bar's close)
    last_date TEXT                -- last bar processed
);
CREATE TABLE IF NOT EXISTS paper_fills (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    price REAL NOT NULL,
    shares REAL NOT NULL,
    fee REAL NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS paper_fills_acct ON paper_fills (account_id, date);
CREATE TABLE IF NOT EXISTS paper_equity (
    account_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    price REAL NOT NULL,
    equity REAL NOT NULL,
    hold REAL NOT NULL,
    PRIMARY KEY (account_id, date)
);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _now_iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


# ---------- strategies ----------

def list_strategies(conn) -> list[dict]:
    init(conn)
    out = [{"id": k, "preset": True, "spec": backtest.validate(v)} for k, v in backtest.PRESETS.items()]
    for r in conn.execute("SELECT * FROM strategies WHERE deleted IS NULL ORDER BY updated DESC"):
        out.append({"id": str(r["id"]), "preset": False, "spec": json.loads(r["spec"]), "updated": r["updated"]})
    return out


def get_strategy(conn, sid: str) -> dict | None:
    if sid in backtest.PRESETS:
        return {"id": sid, "preset": True, "spec": backtest.validate(backtest.PRESETS[sid])}
    init(conn)
    if not sid.isdigit():
        return None
    r = conn.execute("SELECT * FROM strategies WHERE id=? AND deleted IS NULL", (int(sid),)).fetchone()
    return {"id": sid, "preset": False, "spec": json.loads(r["spec"])} if r else None


def save_strategy(conn, spec: dict, now: datetime, sid: str | None = None) -> str:
    init(conn)
    spec = backtest.validate(spec)
    ts = _now_iso(now)
    if sid and sid.isdigit() and conn.execute("SELECT 1 FROM strategies WHERE id=?", (int(sid),)).fetchone():
        conn.execute("UPDATE strategies SET name=?, spec=?, updated=? WHERE id=?",
                     (spec["name"], json.dumps(spec, ensure_ascii=False), ts, int(sid)))
    else:
        sid = str(conn.execute("INSERT INTO strategies (name, spec, created, updated) VALUES (?,?,?,?)",
                               (spec["name"], json.dumps(spec, ensure_ascii=False), ts, ts)).lastrowid)
    conn.commit()
    return sid


def delete_strategy(conn, sid: str, now: datetime) -> None:
    init(conn)
    conn.execute("UPDATE strategies SET deleted=? WHERE id=?", (_now_iso(now), int(sid)))
    conn.commit()


# ---------- accounts ----------

def first_bar_date(now: datetime) -> date:
    """Accounts start with the next bar that is not yet final: today's if before the close, else the next day."""
    ny = now.astimezone(NY)
    return ny.date() if ny.time() < CLOSE_DONE else ny.date() + timedelta(days=1)


def open_account(conn, strategy: dict, ticker: str, capital: float, cost: float, now: datetime) -> int:
    init(conn)
    spec = strategy["spec"]
    if any(c["type"] in ("val_ge", "val_le") for side in ("entry", "exit") for c in spec[side]["conds"]) \
            and fair_for(conn, ticker, spec) is None:
        raise ValueError(f"{ticker} has no DCF valuation, so valuation conditions can’t be used. First add it to Watchlist or Holdings on the Overview page and wait for the Valuation panel to compute a fair value"
                         " (ETFs and companies with negative free cash flow have no DCF)")
    cur = conn.execute(
        "INSERT INTO paper_accounts (strategy_id, name, spec, ticker, capital, cost, start, created, cash) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (int(strategy["id"]) if str(strategy["id"]).isdigit() else None, spec["name"],
         json.dumps(spec, ensure_ascii=False), ticker, capital, cost, first_bar_date(now).isoformat(),
         _now_iso(now), capital))
    conn.commit()
    return cur.lastrowid


def stop_account(conn, aid: int, now: datetime) -> None:
    conn.execute("UPDATE paper_accounts SET active=0, stopped=? WHERE id=?", (_now_iso(now), aid))
    conn.commit()


def accounts(conn) -> list[sqlite3.Row]:
    init(conn)
    return conn.execute("SELECT * FROM paper_accounts ORDER BY active DESC, id DESC").fetchall()


def account(conn, aid: int):
    init(conn)
    a = conn.execute("SELECT * FROM paper_accounts WHERE id=?", (aid,)).fetchone()
    if not a:
        return None
    fills = conn.execute("SELECT * FROM paper_fills WHERE account_id=? ORDER BY date, id", (aid,)).fetchall()
    eq = conn.execute("SELECT * FROM paper_equity WHERE account_id=? ORDER BY date", (aid,)).fetchall()
    return a, fills, eq


def final_bars(dates: list[date], now: datetime) -> int:
    """How many of `dates` are completed bars (drop today's bar before the close)."""
    ny = now.astimezone(NY)
    n = len(dates)
    while n and (dates[n - 1] > ny.date() or (dates[n - 1] == ny.date() and ny.time() < CLOSE_DONE)):
        n -= 1
    return n


def fair_for(conn, ticker: str, spec: dict) -> float | None:
    """Today's DCF fair value when the spec has DCF conditions (else None)."""
    if not any(c["type"] in ("val_ge", "val_le") for side in ("entry", "exit") for c in spec[side]["conds"]):
        return None
    from radar import valuation
    return valuation.fair_value(conn, ticker)


_mcap: dict[str, tuple[float, float]] = {}


def mcap_for(ticker: str, spec: dict, ticker_factory=None) -> float | None:
    """Current market cap (USD) when the spec has a market-cap condition; cached 6 h."""
    import time as _t
    if not any(c["type"] == "mcap_ge" for side in ("entry", "exit") for c in spec[side]["conds"]):
        return None
    hit = _mcap.get(ticker)
    if hit and _t.time() - hit[0] < 6 * 3600:
        return hit[1]
    try:
        if ticker_factory is None:
            import yfinance as yf
            ticker_factory = yf.Ticker
        v = float(ticker_factory(ticker).fast_info["marketCap"])
    except Exception:
        return None
    if not v or v != v:
        return None
    _mcap[ticker] = (_t.time(), v)
    return v


def step_account(conn, a, dates: list[date], closes: list[float], now: datetime) -> list[Fire]:
    """Process every completed bar after a.last_date (from a.start). Returns pushes for new signals."""
    spec = json.loads(a["spec"])
    s = backtest.Series(dates, closes, fair_for(conn, a["ticker"], spec), mcap_for(a["ticker"], spec))
    n = final_bars(dates, now)
    start, last = date.fromisoformat(a["start"]), date.fromisoformat(a["last_date"]) if a["last_date"] else None
    cash, shares, cost = a["cash"], a["shares"], a["cost"]
    pos = backtest.Pos(shares > 0, a["entry_px"] or 0.0, a["peak"] or 0.0, a["held"])
    pending, why, hold_px = a["pending"], a["pending_why"], a["hold_px"]
    fires: list[Fire] = []
    for t in range(n):
        d = dates[t]
        if d < start or (last and d <= last):
            continue
        px = closes[t]
        if hold_px is None:
            hold_px = px
        if pending == "buy" and not pos.holding:
            fee = cash * cost
            shares, cash = (cash - fee) / px, 0.0
            pos = backtest.Pos(True, px, px, 0)
            conn.execute("INSERT INTO paper_fills (account_id, date, side, price, shares, fee, reason) VALUES (?,?,?,?,?,?,?)",
                         (a["id"], d.isoformat(), "buy", px, shares, fee, why))
        elif pending == "sell" and pos.holding:
            gross = shares * px
            fee = gross * cost
            conn.execute("INSERT INTO paper_fills (account_id, date, side, price, shares, fee, reason) VALUES (?,?,?,?,?,?,?)",
                         (a["id"], d.isoformat(), "sell", px, shares, fee, why))
            cash, shares, pos = gross - fee, 0.0, backtest.Pos()
        elif pos.holding:
            pos.held += 1
            pos.peak = max(pos.peak, px)
        conn.execute("INSERT OR REPLACE INTO paper_equity (account_id, date, price, equity, hold) VALUES (?,?,?,?,?)",
                     (a["id"], d.isoformat(), px, cash + shares * px, a["capital"] * px / hold_px))
        pending, why = backtest.decide(spec, s, t, pos)
        if pending:
            verb = "Buy" if pending == "buy" else "Sell"
            fires.append(Fire(f"paper:{a['id']}:{d.isoformat()}", a["ticker"], "notice",
                              f"Paper account #{a['id']} {a['ticker']} triggered a {verb} signal",
                              f"{spec['name']}: {why}. {d.isoformat()} close {px:.2f}; per the rules, {verb} at the next trading day’s close.", True))
        last = d
    conn.execute("UPDATE paper_accounts SET cash=?, shares=?, entry_px=?, peak=?, held=?, pending=?, pending_why=?, "
                 "hold_px=?, last_date=? WHERE id=?",
                 (cash, shares, pos.entry_px if pos.holding else None, pos.peak if pos.holding else None, pos.held,
                  pending, why or None, hold_px, last.isoformat() if last else None, a["id"]))
    conn.commit()
    return fires


def history_years(spec: dict, start: date) -> float:
    """Enough history for the spec's warm-up before `start` (trading days → calendar years, padded)."""
    span = (date.today() - start).days / 365.25
    return round(span + backtest.warmup(spec) / 240 + 0.1, 2)


def run_job(conn, now: datetime, push, fetch=backtest.fetch_history, only: int | None = None) -> str:
    init(conn)
    q = "SELECT * FROM paper_accounts WHERE active=1" + (" AND id=?" if only else "")
    rows = conn.execute(q, (only,) if only else ()).fetchall()
    signals = errors = 0
    for a in rows:
        try:
            spec = json.loads(a["spec"])
            dates, closes = fetch(a["ticker"], history_years(spec, date.fromisoformat(a["start"])), max_age=0)
            for f in step_account(conn, a, dates, closes, now):
                signals += 1
                try:
                    push(f)
                except Exception:
                    pass
        except Exception:
            errors += 1
            import logging
            logging.getLogger(__name__).exception("paper account %s failed", a["id"])
    if errors and errors == len(rows):
        raise RuntimeError(f"all {errors} accounts failed")
    return f"accounts={len(rows)} signals={signals} errors={errors}"


def preview(spec: dict, a, dates: list[date], closes: list[float], now: datetime, fair: float | None = None,
            mcap: float | None = None) -> dict | None:
    """During market hours: what would today's close trigger if the price stayed here?"""
    n = final_bars(dates, now)
    if n == len(dates) or n == 0:
        return None  # market closed: nothing provisional to show
    if a["last_date"] and dates[n - 1].isoformat() != a["last_date"]:
        return None  # the account hasn't caught up to the last completed bar yet
    s = backtest.Series(dates, closes, fair, mcap)
    held = a["held"] + (1 if a["shares"] > 0 and a["pending"] != "sell" else 0)
    holding = (a["shares"] > 0 or a["pending"] == "buy") and a["pending"] != "sell"
    entry = a["entry_px"] or (closes[-1] if a["pending"] == "buy" else 0.0)
    pos = backtest.Pos(holding, entry, max(a["peak"] or 0.0, closes[-1]) if holding else 0.0, held)
    sig, why = backtest.decide(spec, s, len(dates) - 1, pos)
    return {"price": closes[-1], "signal": sig, "why": why, "holding": holding}
