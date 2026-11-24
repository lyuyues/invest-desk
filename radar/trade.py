"""Manual paper trading for learning (/trade): virtual cash, real quotes, a trade journal. No real orders.

Orders: market (filled now at the live quote), limit buy/sell and stop sell (good till cancelled,
matched after each close against that day's open/high/low — a gap through the price fills at the open).
Long only, no margin (options: see radar/options.py). Positions use average cost; a "round trip" runs from a flat position back to flat.
"""
from __future__ import annotations

import sqlite3
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
BENCH = "SPY"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    capital REAL NOT NULL,
    cash REAL NOT NULL,
    cost REAL NOT NULL,           -- fee per side, fraction of notional
    created TEXT NOT NULL,
    bench_px REAL,                -- SPY when the account opened
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS trade_orders (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    created TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    kind TEXT NOT NULL CHECK (kind IN ('market', 'limit', 'stop')),
    qty REAL NOT NULL,
    px REAL,                      -- limit / stop price
    status TEXT NOT NULL CHECK (status IN ('open', 'filled', 'cancelled', 'rejected')),
    filled_at TEXT,
    fill_px REAL,
    fee REAL,
    reason TEXT NOT NULL,         -- why (required)
    plan_stop REAL,
    plan_target REAL,
    review TEXT,                  -- post-trade review
    msg TEXT                      -- rejection reason / fill note
);
CREATE INDEX IF NOT EXISTS trade_orders_acct ON trade_orders (account_id, status);
CREATE TABLE IF NOT EXISTS trade_equity (
    account_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    equity REAL NOT NULL,
    bench REAL,
    PRIMARY KEY (account_id, date)
);
"""


def init(conn) -> None:
    """Create tables (incl. options) once; a plain check afterwards, so it never commits a caller's transaction."""
    from radar.options import SCHEMA as OPT_SCHEMA
    if conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
                    "('trade_accounts','trade_orders','trade_equity','opt_orders')").fetchone()[0] < 4:
        conn.executescript(SCHEMA + OPT_SCHEMA)


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


def market_open(now: datetime) -> bool:
    ny = now.astimezone(NY)
    return ny.weekday() < 5 and time(9, 30) <= ny.time() < time(16, 0)


# ---------- quotes ----------

_qcache: dict[str, tuple[float, float]] = {}


def live_quote(ticker: str, ticker_factory=None) -> float:
    """Latest trade price (≈15 min delayed at worst); cached 20 s."""
    hit = _qcache.get(ticker)
    if hit and _time.time() - hit[0] < 20:
        return hit[1]
    if ticker_factory is None:
        import yfinance as yf
        ticker_factory = yf.Ticker
    try:
        px = float(ticker_factory(ticker).fast_info["lastPrice"])
    except Exception:
        px = 0.0
    if not px or px != px or px <= 0:
        raise ValueError(f"Can’t find a quote for {ticker}. Is the ticker right?")
    _qcache[ticker] = (_time.time(), px)
    return px


def daily_bars(ticker: str, since: date, ticker_factory=None) -> list[dict]:
    if ticker_factory is None:
        import yfinance as yf
        ticker_factory = yf.Ticker
    hist = ticker_factory(ticker).history(start=since.isoformat(), interval="1d", auto_adjust=False)
    out = []
    for ts, r in hist.iterrows():
        if r["Close"] == r["Close"]:
            out.append({"date": ts.date(), "open": float(r["Open"]), "high": float(r["High"]),
                        "low": float(r["Low"]), "close": float(r["Close"])})
    return out


# ---------- accounts & positions ----------

def open_account(conn, name: str, capital: float, cost: float, now: datetime, quote=live_quote) -> int:
    init(conn)
    try:
        bench = quote(BENCH)
    except Exception:
        bench = None
    cur = conn.execute("INSERT INTO trade_accounts (name, capital, cash, cost, created, bench_px) VALUES (?,?,?,?,?,?)",
                       (name.strip()[:40] or "My paper account", capital, capital, cost, _iso(now), bench))
    conn.commit()
    return cur.lastrowid


def accounts(conn):
    init(conn)
    return conn.execute("SELECT * FROM trade_accounts ORDER BY active DESC, id DESC").fetchall()


def account(conn, aid: int):
    init(conn)
    return conn.execute("SELECT * FROM trade_accounts WHERE id=?", (aid,)).fetchone()


def orders(conn, aid: int, status: str | None = None):
    q = "SELECT * FROM trade_orders WHERE account_id=?" + (" AND status=?" if status else "") + " ORDER BY id DESC"
    return conn.execute(q, (aid, status) if status else (aid,)).fetchall()


def book(conn, aid: int) -> tuple[dict, list[dict]]:
    """Positions {ticker: {qty, avg, realized, opened, buys:[orders]}} and closed round trips (oldest first)."""
    pos: dict[str, dict] = {}
    trips: list[dict] = []
    fills = conn.execute("SELECT * FROM trade_orders WHERE account_id=? AND status='filled' ORDER BY filled_at, id",
                         (aid,)).fetchall()
    for f in fills:
        p = pos.setdefault(f["ticker"], {"qty": 0.0, "avg": 0.0, "realized": 0.0, "opened": None, "orders": [],
                                         "invested": 0.0, "fees": 0.0})
        if f["side"] == "buy":
            if p["qty"] == 0:
                p.update(opened=f["filled_at"], orders=[], realized=0.0, invested=0.0, fees=0.0)
            p["avg"] = (p["avg"] * p["qty"] + f["fill_px"] * f["qty"]) / (p["qty"] + f["qty"])
            p["qty"] += f["qty"]
            p["invested"] += f["fill_px"] * f["qty"]
            p["realized"] -= f["fee"]
        else:
            p["realized"] += (f["fill_px"] - p["avg"]) * f["qty"] - f["fee"]
            p["qty"] -= f["qty"]
            if p["qty"] < 1e-9:
                p["qty"] = 0.0
        p["fees"] += f["fee"]
        p["orders"].append(f)
        if p["qty"] == 0:
            first = p["orders"][0]
            trips.append({"ticker": f["ticker"], "opened": p["opened"], "closed": f["filled_at"],
                          "pnl": p["realized"], "ret": p["realized"] / p["invested"] if p["invested"] else 0.0,
                          "reason": first["reason"], "plan_stop": first["plan_stop"], "plan_target": first["plan_target"],
                          "orders": list(p["orders"]), "last_id": f["id"], "review": f["review"]})
            del pos[f["ticker"]]
    return pos, trips


def stats(trips: list[dict]) -> dict:
    if not trips:
        return {}
    wins = [t["pnl"] for t in trips if t["pnl"] > 0]
    losses = [t["pnl"] for t in trips if t["pnl"] <= 0]
    return {"n": len(trips), "win": len(wins) / len(trips),
            "avg_win": sum(wins) / len(wins) if wins else None,
            "avg_loss": sum(losses) / len(losses) if losses else None,
            "pf": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None,
            "expectancy": sum(t["pnl"] for t in trips) / len(trips),
            "planned": sum(t["plan_stop"] is not None for t in trips) / len(trips)}


def max_drawdown(values: list[float]) -> float:
    peak, mdd = (values[0] if values else 0), 0.0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1) if peak else mdd
    return mdd


# ---------- orders ----------

class OrderError(ValueError):
    pass


def _fill(conn, o, px: float, when: datetime, note: str = "") -> bool:
    """Fill order `o` at `px` if cash / shares allow, else reject. Returns True if filled."""
    a = account(conn, o["account_id"])
    pos, _ = book(conn, o["account_id"])
    held = pos.get(o["ticker"], {}).get("qty", 0.0)
    fee = px * o["qty"] * a["cost"]
    if o["side"] == "buy":
        need = px * o["qty"] + fee
        if need > a["cash"] + 1e-6:
            conn.execute("UPDATE trade_orders SET status='rejected', msg=? WHERE id=?",
                         (f"Insufficient cash: need {need:,.2f}, available {a['cash']:,.2f}", o["id"]))
            return False
        cash = a["cash"] - need
    else:
        if o["qty"] > held + 1e-9:
            conn.execute("UPDATE trade_orders SET status='rejected', msg=? WHERE id=?",
                         (f"Not enough shares: trying to sell {o['qty']:g}, only have {held:g}", o["id"]))
            return False
        cash = a["cash"] + px * o["qty"] - fee
    from radar import options   # short puts reserve cash; short calls lock shares
    err = options._check(conn, o["account_id"], cash,
                         stock_delta={o["ticker"]: o["qty"] if o["side"] == "buy" else -o["qty"]})
    if err:
        conn.execute("UPDATE trade_orders SET status='rejected', msg=? WHERE id=?", (err, o["id"]))
        return False
    conn.execute("UPDATE trade_orders SET status='filled', filled_at=?, fill_px=?, fee=?, msg=? WHERE id=?",
                 (_iso(when), px, fee, note or None, o["id"]))
    conn.execute("UPDATE trade_accounts SET cash=? WHERE id=?", (cash, o["account_id"]))
    return True


def place(conn, aid: int, f: dict, now: datetime, quote=live_quote) -> int:
    """f: ticker, side, kind, qty, px, reason, plan_stop, plan_target. Returns order id (filled, open or rejected)."""
    a = account(conn, aid)
    if not a or not a["active"]:
        raise OrderError("Account does not exist or is closed")
    ticker, side, kind = f["ticker"].upper().strip(), f["side"], f["kind"]
    if side not in ("buy", "sell") or kind not in ("market", "limit", "stop"):
        raise OrderError("Invalid order type")
    if kind == "stop" and side == "buy":
        raise OrderError("Stop orders can only be used to sell")
    reason = (f.get("reason") or "").strip()
    if not reason:
        raise OrderError("Write one sentence on why you are making this trade. This is the key to learning")
    try:
        qty = float(f["qty"])
        px = float(f["px"]) if f.get("px") not in (None, "") else None
        stop = float(f["plan_stop"]) if f.get("plan_stop") not in (None, "") else None
        target = float(f["plan_target"]) if f.get("plan_target") not in (None, "") else None
    except (TypeError, ValueError):
        raise OrderError("Quantity / price must be numbers")
    if qty <= 0 or qty != int(qty):
        raise OrderError("Quantity must be a positive integer")
    if kind != "market" and not (px and px > 0):
        raise OrderError("Limit / stop orders need a price")
    if side == "sell":
        held = book(conn, aid)[0].get(ticker, {}).get("qty", 0)
        if held <= 0:
            raise OrderError(f"You don’t hold {ticker}, so you can’t sell it (long only, no short selling). To sell a put, click “Options →”")
    try:
        last = quote(ticker)   # also validates the ticker
    except Exception as e:
        raise OrderError(str(e))
    cur = conn.execute(
        "INSERT INTO trade_orders (account_id, created, ticker, side, kind, qty, px, status, reason, plan_stop, plan_target) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (aid, _iso(now), ticker, side, kind, qty, px, "open", reason[:1000], stop, target))
    o = conn.execute("SELECT * FROM trade_orders WHERE id=?", (cur.lastrowid,)).fetchone()
    note = "" if market_open(now) else "Outside market hours: filled at the last trade price"
    marketable = (kind == "market" or (kind == "limit" and (last <= px if side == "buy" else last >= px))
                  or (kind == "stop" and last <= px))
    if marketable:
        _fill(conn, o, last, now, note)
    conn.commit()
    return o["id"]


def transfer_in(conn, aid: int, ticker: str, qty: float, cost: float | None, now: datetime, quote=live_quote) -> int:
    """Bring shares you already own into the paper account (e.g. to practise covered calls).

    Capital grows by the shares' current value, so the account's return starts at 0 % for them; the SPY
    benchmark gets the same amount added at today's SPY price. `cost` (your real cost basis) only affects the
    position's P&L display and journal; default = current price."""
    a = account(conn, aid)
    if not a or not a["active"]:
        raise OrderError("Account does not exist or is closed")
    ticker = ticker.upper().strip()
    if qty <= 0 or qty != int(qty):
        raise OrderError("Shares must be a positive integer")
    try:
        px = quote(ticker)
    except Exception as e:
        raise OrderError(str(e))
    cost = cost if cost and cost > 0 else px
    add = px * qty
    bench_px = a["bench_px"]
    try:
        spy = quote(BENCH)
        if bench_px:   # keep the benchmark's current value continuous: old SPY value + `add`
            bench_px = (a["capital"] + add) * spy / (a["capital"] * spy / bench_px + add)
    except Exception:
        pass
    # cash unchanged; the shares are recorded at `cost` so the position shows your real gain, while the account's
    # return (equity vs capital) starts at 0 % for them
    conn.execute("UPDATE trade_accounts SET capital=capital+?, bench_px=? WHERE id=?", (add, bench_px, aid))
    cur = conn.execute(
        "INSERT INTO trade_orders (account_id, created, ticker, side, kind, qty, px, status, filled_at, fill_px, fee, reason, msg) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (aid, _iso(now), ticker, "buy", "market", qty, px, "filled", _iso(now), cost, 0.0,
                                             TRANSFER, f"Transfer-in Price {px:.2f}"))
    conn.commit()
    return cur.lastrowid


TRANSFER = "Transferred existing position"


def fix_transfer(conn, aid: int, oid: int, qty: float, now: datetime, quote=live_quote) -> None:
    """Correct the share count of a transfer-in (0 removes it). Capital moves by Δshares × the transfer price."""
    o = conn.execute("SELECT * FROM trade_orders WHERE id=? AND account_id=? AND reason=? AND status='filled'",
                     (oid, aid, TRANSFER)).fetchone()
    if not o:
        raise OrderError("Can’t find this transfer")
    if qty < 0 or qty != int(qty):
        raise OrderError("Shares must be 0 or a positive integer")
    px = o["px"]
    if px is None:
        import re
        m = re.search(r"Price ([0-9.]+)", o["msg"] or "")
        px = float(m.group(1)) if m else o["fill_px"]
    a = account(conn, aid)
    delta = (qty - o["qty"]) * px
    if a["capital"] + delta <= 0:
        raise OrderError("After this change the principal would be negative")
    if qty == 0:
        conn.execute("DELETE FROM trade_orders WHERE id=?", (oid,))
    else:
        conn.execute("UPDATE trade_orders SET qty=? WHERE id=?", (qty, oid))
    run, oversold = 0.0, False
    for f in conn.execute("SELECT side, qty FROM trade_orders WHERE account_id=? AND ticker=? AND status='filled' "
                          "ORDER BY filled_at, id", (aid, o["ticker"])):
        run += f["qty"] if f["side"] == "buy" else -f["qty"]
        oversold = oversold or run < -1e-9
    from radar import options
    err = "More shares have already been sold since then than the new amount" if oversold else options._check(conn, aid, a["cash"])
    if err:
        conn.rollback()
        raise OrderError(err)
    bench_px = a["bench_px"]
    try:
        spy = quote(BENCH)
        if bench_px:
            bench_px = (a["capital"] + delta) * spy / (a["capital"] * spy / bench_px + delta)
    except Exception:
        pass
    conn.execute("UPDATE trade_accounts SET capital=capital+?, bench_px=? WHERE id=?", (delta, bench_px, aid))
    conn.commit()


def set_plan(conn, aid: int, ticker: str, stop: float | None, target: float | None) -> None:
    """Set / change the planned stop and target of an open position (stored on its opening order)."""
    pos, _ = book(conn, aid)
    p = pos.get(ticker.upper().strip())
    if not p:
        raise OrderError(f"No position in {ticker}")
    if stop is not None and stop <= 0 or target is not None and target <= 0:
        raise OrderError("Price must be greater than 0")
    if stop is not None and target is not None and stop >= target:
        raise OrderError("Stop price must be below the target price")
    conn.execute("UPDATE trade_orders SET plan_stop=?, plan_target=? WHERE id=?", (stop, target, p["orders"][0]["id"]))
    conn.commit()


def cancel(conn, aid: int, oid: int) -> None:
    conn.execute("UPDATE trade_orders SET status='cancelled' WHERE id=? AND account_id=? AND status='open'", (oid, aid))
    conn.commit()


def set_review(conn, aid: int, oid: int, text: str) -> None:
    conn.execute("UPDATE trade_orders SET review=? WHERE id=? AND account_id=?", (text.strip()[:2000] or None, oid, aid))
    conn.commit()


def match_bar(o, bar: dict) -> float | None:
    """Fill price for an open limit/stop order on a daily bar, or None."""
    if o["kind"] == "limit" and o["side"] == "buy" and bar["low"] <= o["px"]:
        return min(bar["open"], o["px"])
    if o["kind"] == "limit" and o["side"] == "sell" and bar["high"] >= o["px"]:
        return max(bar["open"], o["px"])
    if o["kind"] == "stop" and bar["low"] <= o["px"]:
        return min(bar["open"], o["px"])
    return None


def _first_bar_day(created_iso: str) -> date:
    ny = datetime.fromisoformat(created_iso).astimezone(NY)
    return ny.date() if ny.time() < time(9, 30) else ny.date() + timedelta(days=1)


def run_job(conn, now: datetime, bars=daily_bars, quote=live_quote) -> str:
    """After the close: match open limit/stop orders on completed daily bars, then snapshot equity."""
    init(conn)
    ny = now.astimezone(NY)
    done = ny.date() if ny.time() >= time(16, 15) else ny.date() - timedelta(days=1)
    filled = 0
    cache: dict[str, list[dict]] = {}
    for o in conn.execute("SELECT o.* FROM trade_orders o JOIN trade_accounts a ON a.id=o.account_id "
                          "WHERE o.status='open' AND a.active=1 ORDER BY o.id").fetchall():
        start = _first_bar_day(o["created"])
        if o["ticker"] not in cache:
            cache[o["ticker"]] = bars(o["ticker"], start - timedelta(days=7))
        for bar in cache[o["ticker"]]:
            if start <= bar["date"] <= done:
                px = match_bar(o, bar)
                if px is not None:
                    when = datetime.combine(bar["date"], time(16, 0), NY)
                    filled += _fill(conn, o, px, when, f"{bar['date']} daily-bar fill")
                    break
    conn.commit()
    snaps = 0
    for a in conn.execute("SELECT * FROM trade_accounts WHERE active=1").fetchall():
        pos, _ = book(conn, a["id"])
        try:
            from radar import options
            ov, _, oerr = options.value(conn, a["id"], now, spot=quote)
            if oerr:
                raise ValueError(oerr[0])
            eq = a["cash"] + sum(p["qty"] * quote(t) for t, p in pos.items()) + ov
        except Exception:
            continue   # a quote failed: skip today's snapshot rather than record a wrong value
        try:
            b = quote(BENCH)
            bench_px = a["bench_px"] or b
            if not a["bench_px"]:
                conn.execute("UPDATE trade_accounts SET bench_px=? WHERE id=?", (b, a["id"]))
            bench = a["capital"] * b / bench_px
        except Exception:
            bench = None
        conn.execute("INSERT OR REPLACE INTO trade_equity (account_id, date, equity, bench) VALUES (?,?,?,?)",
                     (a["id"], done.isoformat(), eq, bench))
        snaps += 1
    conn.commit()
    return f"filled={filled} snapshots={snaps}"
