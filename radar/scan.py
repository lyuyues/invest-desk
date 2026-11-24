"""Market-wide portfolio paper accounts (Strategy sim · Market-wide): scan every US-listed stock above a market-cap floor,
keep a DCF fair value for each, and let a valuation strategy hold up to N of them. Educational; no real orders.

Daily after the close (job `scan`):
  1. universe  — Yahoo screener: US exchanges (no OTC), market cap ≥ FLOOR → price, market cap
  2. accounts  — execute yesterday's signals at today's close, snapshot equity vs SPY, then decide new signals
                 (sells: exit conditions on held names; buys: entry conditions, best DCF upside first, into free slots)
  3. fair values — DCF engine for the stalest names within a time budget (fundamentals move quarterly, so a
                 rolling refresh every ~week is enough; the first full pass takes a few runs)
Same execution rule as single-stock paper accounts: a signal at day t's close fills at day t+1's close.
Fair value: your own DCF inputs for names on the Valuation panel, else the automatic engine value.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import time as _time
from datetime import date, datetime, time, timezone

from radar import backtest
from radar.trade import NY

log = logging.getLogger(__name__)
FLOOR = 1e9                       # universe market-cap floor (USD)
EXCHANGES = ["NMS", "NYQ", "NGM", "NCM", "ASE"]
# A free-cash-flow DCF doesn't fit banks / insurers / brokers, REITs or regulated utilities (their value isn't
# driven by FCF to the firm: it gives absurd fair values there), so those sectors are left out of the universe.
SECTORS = ["Technology", "Healthcare", "Consumer Cyclical", "Consumer Defensive", "Industrials",
           "Communication Services", "Energy", "Basic Materials"]
MAX_UPSIDE = 400.0                # upside above this (%) is almost always bad data → skipped
STALE_DAYS = 7
# price / momentum conditions need daily history: fetched only for names that already pass the valuation filter
PRICE_CONDS = {"new_high", "new_low", "ret_up", "ret_down", "above_ma", "below_ma", "ma_up", "ma_down", "dip"}
ALLOWED = set(backtest.VALUATION_CONDS) | {"stop_loss", "trailing", "take_profit", "max_hold"} | PRICE_CONDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_universe (
    ticker TEXT PRIMARY KEY, name TEXT, exchange TEXT, price REAL, mcap REAL, quote_date TEXT, seen TEXT
);
CREATE TABLE IF NOT EXISTS scan_momo (
    account_id INTEGER NOT NULL, ticker TEXT NOT NULL, date TEXT NOT NULL, ok INTEGER NOT NULL,
    PRIMARY KEY (account_id, ticker)
);
CREATE TABLE IF NOT EXISTS scan_fair (
    ticker TEXT PRIMARY KEY, updated TEXT NOT NULL, fair REAL, error TEXT, fcf_margin REAL, g1 REAL, rr REAL
);
CREATE TABLE IF NOT EXISTS port_accounts (
    id INTEGER PRIMARY KEY, strategy_id INTEGER, name TEXT NOT NULL, spec TEXT NOT NULL,
    capital REAL NOT NULL, cost REAL NOT NULL, max_pos INTEGER NOT NULL, created TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, cash REAL NOT NULL, last_date TEXT, bench_px REAL
);
CREATE TABLE IF NOT EXISTS port_positions (
    account_id INTEGER NOT NULL, ticker TEXT NOT NULL, shares REAL NOT NULL, entry_px REAL NOT NULL,
    entry_date TEXT NOT NULL, peak REAL NOT NULL, held INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (account_id, ticker)
);
CREATE TABLE IF NOT EXISTS port_pending (
    account_id INTEGER NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL, why TEXT, created TEXT NOT NULL,
    PRIMARY KEY (account_id, ticker)
);
CREATE TABLE IF NOT EXISTS port_fills (
    id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, date TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL,
    price REAL NOT NULL, shares REAL NOT NULL, fee REAL NOT NULL, reason TEXT
);
CREATE TABLE IF NOT EXISTS port_equity (
    account_id INTEGER NOT NULL, date TEXT NOT NULL, equity REAL NOT NULL, bench REAL, n_pos INTEGER,
    PRIMARY KEY (account_id, date)
);
"""


def init(conn) -> None:
    conn.executescript(SCHEMA)


def check_spec(spec: dict) -> None:
    bad = [c["type"] for side in ("entry", "exit") for c in spec[side]["conds"] if c["type"] not in ALLOWED]
    if bad:
        raise ValueError("Market-wide portfolios only support valuation / market cap, price momentum conditions and stop loss, take profit, trailing take profit, holding days; "
                         f"unsupported: {', '.join(backtest.CONDS[b][0].split('{')[0].strip() for b in bad)}")
    if not any(c["type"] == "val_ge" for c in spec["entry"]["conds"]):
        raise ValueError("A market-wide portfolio needs a buy condition “DCF upside ≥ X%”")
    if spec["entry"]["mode"] != "all" and any(c["type"] in PRICE_CONDS for c in spec["entry"]["conds"]):
        raise ValueError("When buy conditions include momentum / price conditions, choose “All met”")


def value_only(spec: dict) -> dict:
    """The spec with price conditions dropped from the entry side: the cheap first-pass filter."""
    return {**spec, "entry": {"mode": "all", "conds": [c for c in spec["entry"]["conds"] if c["type"] not in PRICE_CONDS]}}


def price_conds(spec: dict, side: str) -> list[dict]:
    return [c for c in spec[side]["conds"] if c["type"] in PRICE_CONDS]


def fetch_closes(tickers: list[str], bar_date: str, days: int = 420) -> dict[str, tuple[list, list]]:
    """Daily adjusted closes up to (not including) bar_date for many tickers in one yfinance call."""
    if not tickers:
        return {}
    import yfinance as yf
    from datetime import timedelta
    start = (date.fromisoformat(bar_date) - timedelta(days=days)).isoformat()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        df = yf.download(tickers, start=start, end=bar_date, interval="1d", auto_adjust=True, progress=False,
                         group_by="ticker", threads=True)
    out = {}
    for t in tickers:
        try:
            col = df[t]["Close"]
        except Exception:
            try:
                col = df["Close"] if len(tickers) == 1 else None
            except Exception:
                col = None
        if col is None:
            continue
        if hasattr(col, "columns"):
            col = col.iloc[:, 0]
        ds, cs = [], []
        for ts, v in col.items():
            if v == v and v > 0:
                ds.append(ts.date())
                cs.append(float(v))
        if ds:
            out[t] = (ds, cs)
    return out


def _series(hist, t: str, bar_date: str, px: float, fair, cap) -> backtest.Series:
    d0 = date.fromisoformat(bar_date)
    ds, cs = hist.get(t, ([], []))
    keep = [(d, c) for d, c in zip(ds, cs) if d < d0]
    return backtest.Series([d for d, _ in keep] + [d0], [c for _, c in keep] + [px], fair, cap)


# ---------- universe & fair values ----------

def fetch_universe(screener=None, floor: float = FLOOR) -> list[dict]:
    if screener is None:
        import yfinance as yf
        screener = yf.screen
    from yfinance import EquityQuery as Q
    q = Q("and", [Q("eq", ["region", "us"]), Q("gte", ["intradaymarketcap", floor]), Q("is-in", ["exchange"] + EXCHANGES),
                  Q("is-in", ["sector"] + SECTORS)])
    out, offset = [], 0
    while True:
        res = screener(q, size=250, offset=offset, sortField="intradaymarketcap", sortAsc=False)
        quotes = res.get("quotes", [])
        for x in quotes:
            px, cap = x.get("regularMarketPrice"), x.get("marketCap")
            if x.get("quoteType") != "EQUITY" or not px or not cap or x.get("financialCurrency", "USD") != "USD":
                continue    # non-USD financials (foreign filers): fair value would be in another currency
            ts = x.get("regularMarketTime")
            out.append({"ticker": x["symbol"], "name": x.get("shortName") or x.get("longName") or "",
                        "exchange": x.get("exchange"), "price": float(px), "mcap": float(cap),
                        "quote_date": datetime.fromtimestamp(ts, timezone.utc).astimezone(NY).date().isoformat()
                        if ts else None})
        offset += len(quotes)
        if not quotes or offset >= (res.get("total") or 0) or offset > 8000:
            break
    return out


def store_universe(conn, rows: list[dict], now: datetime) -> None:
    conn.executemany("INSERT OR REPLACE INTO scan_universe VALUES (?,?,?,?,?,?,?)",
                     [(r["ticker"], r["name"], r["exchange"], r["price"], r["mcap"], r["quote_date"],
                       now.isoformat(timespec="seconds")) for r in rows])
    conn.commit()


def refresh_fair(conn, now: datetime, budget_s: float = 2400, limit: int = 2000, compute=None) -> dict:
    """DCF for the stalest universe names (never valued first) until the time budget runs out."""
    from radar import valuation
    if compute is None:
        from radar.dcf import builder
        from radar import wacc as waccmod

        def compute(t, px, cap):
            with contextlib.redirect_stdout(io.StringIO()):
                data = valuation.compute(conn, t, builder, shares_of=lambda _t: cap / px / 1e6 if px else None,
                                         profile_of=waccmod.market_profile, price=px, quarterly=False)
            a = data["auto"]
            fair = valuation.project(valuation.merged(a, None))["fair"]
            g1 = (a["analyst"][0] / a["base_rev"] - 1) if a["analyst"] else a["path"][0]
            return fair, a["fcf_margin"], g1, a["rr"]
    cutoff = (now.timestamp() - STALE_DAYS * 86400)
    rows = conn.execute(
        "SELECT u.ticker, u.price, u.mcap, f.updated FROM scan_universe u LEFT JOIN scan_fair f ON f.ticker=u.ticker "
        "ORDER BY f.updated IS NOT NULL, f.updated, u.mcap DESC").fetchall()
    todo = [r for r in rows if not r["updated"] or datetime.fromisoformat(r["updated"]).timestamp() < cutoff][:limit]
    t0, ok, bad = _time.time(), 0, 0
    for r in todo:
        if _time.time() - t0 > budget_s:
            break
        try:
            fair, margin, g1, rr = compute(r["ticker"], r["price"], r["mcap"])
            conn.execute("INSERT OR REPLACE INTO scan_fair VALUES (?,?,?,?,?,?,?)",
                         (r["ticker"], now.isoformat(timespec="seconds"), fair if fair and fair > 0 else None,
                          None if fair and fair > 0 else "Negative DCF (negative free cash flow)", margin, g1, rr))
            ok += 1
        except Exception as e:
            conn.execute("INSERT OR REPLACE INTO scan_fair VALUES (?,?,?,?,?,?,?)",
                         (r["ticker"], now.isoformat(timespec="seconds"), None, f"{type(e).__name__}: {e}"[:200],
                          None, None, None))
            bad += 1
        conn.commit()
    return {"done": ok + bad, "ok": ok, "failed": bad, "left": len(todo) - ok - bad}


def coverage(conn) -> dict:
    init(conn)
    r = conn.execute("SELECT COUNT(*) n, MAX(seen) seen, MAX(quote_date) qd FROM scan_universe").fetchone()
    f = conn.execute("SELECT COUNT(*) n, SUM(fair IS NOT NULL) ok, MAX(updated) last FROM scan_fair "
                     "WHERE ticker IN (SELECT ticker FROM scan_universe)").fetchone()
    return {"universe": r["n"], "seen": r["seen"], "quote_date": r["qd"], "valued": f["n"] or 0,
            "with_fair": f["ok"] or 0, "last_fair": f["last"]}


def fair_map(conn) -> dict[str, float]:
    """Fair value per ticker: your own DCF (Valuation panel) wins over the scan's automatic one."""
    from radar import valuation
    out = {r["ticker"]: r["fair"] for r in conn.execute("SELECT ticker, fair FROM scan_fair WHERE fair IS NOT NULL")}
    mine = {r["ticker"] for r in conn.execute("SELECT ticker FROM valuation_inputs")}
    for r in conn.execute("SELECT ticker FROM valuations").fetchall():
        # the panel's model wins when you've set your own inputs (or the scan has none); otherwise the scan's
        # rolling refresh is the fresher automatic value
        if r["ticker"] in mine or r["ticker"] not in out:
            f = valuation.fair_value(conn, r["ticker"])
            if f:
                out[r["ticker"]] = f
    return out


def candidates(conn, spec: dict, limit: int = 30, aid: int | None = None) -> list[dict]:
    """Names meeting the valuation part of the entry conditions at the latest universe prices, best upside first.
    momo: price/momentum part at the last close (1 yes, 0 no, -1 no price history, None not checked yet)."""
    fairs = fair_map(conn)
    momo = {r["ticker"]: r["ok"] for r in conn.execute("SELECT * FROM scan_momo WHERE account_id=?", (aid,))} \
        if aid is not None and price_conds(spec, "entry") else {}
    spec = value_only(spec)
    out = []
    for u in conn.execute("SELECT * FROM scan_universe").fetchall():
        fair = fairs.get(u["ticker"])
        if not fair:
            continue
        up = (fair / u["price"] - 1) * 100
        if up > MAX_UPSIDE:
            continue
        s = backtest.Series([date.today()], [u["price"]], fair, u["mcap"])
        sig, _ = backtest.decide(spec, s, 0, backtest.Pos())
        if sig == "buy":
            out.append({"ticker": u["ticker"], "name": u["name"], "price": u["price"], "mcap": u["mcap"],
                        "fair": fair, "upside": up, "momo": momo.get(u["ticker"])})
    out.sort(key=lambda x: -x["upside"])
    return out[:limit]


# ---------- portfolio accounts ----------

def open_account(conn, strategy: dict, capital: float, cost: float, max_pos: int, now: datetime, bench_px=None) -> int:
    init(conn)
    spec = strategy["spec"]
    check_spec(spec)
    cur = conn.execute("INSERT INTO port_accounts (strategy_id, name, spec, capital, cost, max_pos, created, cash, bench_px) "
                       "VALUES (?,?,?,?,?,?,?,?,?)",
                       (int(strategy["id"]) if str(strategy["id"]).isdigit() else None, spec["name"],
                        json.dumps(spec, ensure_ascii=False), capital, cost, max_pos, now.isoformat(timespec="seconds"),
                        capital, bench_px))
    conn.commit()
    return cur.lastrowid


def accounts(conn):
    init(conn)
    return conn.execute("SELECT * FROM port_accounts ORDER BY active DESC, id DESC").fetchall()


def account(conn, aid: int):
    init(conn)
    return conn.execute("SELECT * FROM port_accounts WHERE id=?", (aid,)).fetchone()


def positions(conn, aid: int):
    return conn.execute("SELECT * FROM port_positions WHERE account_id=? ORDER BY ticker", (aid,)).fetchall()


def step(conn, a, bar_date: str, prices: dict[str, tuple[float, float]], fairs: dict[str, float],
         spy: float | None, now: datetime, history=None) -> dict:
    """One trading day for one account. prices: ticker → (close, market cap)."""
    spec = json.loads(a["spec"])
    aid, cost = a["id"], a["cost"]
    cash = a["cash"]
    held = {p["ticker"]: dict(p) for p in positions(conn, aid)}
    eq_prev = cash + sum(p["shares"] * prices.get(t, (p["entry_px"],))[0] for t, p in held.items())
    pend = conn.execute("SELECT * FROM port_pending WHERE account_id=?", (aid,)).fetchall()
    fills = []
    for p in sorted(pend, key=lambda r: r["side"] != "sell"):          # sells first: they free cash
        t = p["ticker"]
        if t not in prices:
            continue                                                    # no price today: keep it pending
        px = prices[t][0]
        if p["side"] == "sell" and t in held:
            gross = held[t]["shares"] * px
            fee = gross * cost
            cash += gross - fee
            fills.append((t, "sell", px, held[t]["shares"], fee, p["why"]))
            conn.execute("DELETE FROM port_positions WHERE account_id=? AND ticker=?", (aid, t))
            del held[t]
        elif p["side"] == "buy" and t not in held and len(held) < a["max_pos"]:
            budget = min(cash, eq_prev / a["max_pos"])
            if budget >= eq_prev / a["max_pos"] * 0.25:
                fee = budget * cost
                sh = (budget - fee) / px
                cash -= budget
                held[t] = {"shares": sh, "entry_px": px, "entry_date": bar_date, "peak": px, "held": 0}
                conn.execute("INSERT INTO port_positions VALUES (?,?,?,?,?,?,0)", (aid, t, sh, px, bar_date, px))
                fills.append((t, "buy", px, sh, fee, p["why"]))
        conn.execute("DELETE FROM port_pending WHERE account_id=? AND ticker=?", (aid, t))
    for f in fills:
        conn.execute("INSERT INTO port_fills (account_id, date, ticker, side, price, shares, fee, reason) VALUES (?,?,?,?,?,?,?,?)",
                     (aid, bar_date, *f))
    bought_today = {f[0] for f in fills if f[1] == "buy"}
    for t, p in held.items():
        if t in prices and t not in bought_today:
            px = prices[t][0]
            conn.execute("UPDATE port_positions SET peak=MAX(peak, ?), held=held+1 WHERE account_id=? AND ticker=?",
                         (px, aid, t))
            p["peak"], p["held"] = max(p["peak"], px), p["held"] + 1
    equity = cash + sum(p["shares"] * prices.get(t, (p["entry_px"],))[0] for t, p in held.items())
    bench_px = a["bench_px"] or spy
    bench = a["capital"] * spy / bench_px if spy and bench_px else None
    conn.execute("INSERT OR REPLACE INTO port_equity VALUES (?,?,?,?,?)", (aid, bar_date, equity, bench, len(held)))
    # new signals at today's close → fill at the next close
    sells, buys = signals(conn, a, spec, bar_date, prices, fairs, held, history)
    conn.execute("UPDATE port_accounts SET cash=?, last_date=?, bench_px=? WHERE id=?", (cash, bar_date, bench_px, aid))
    conn.commit()
    return {"fills": fills, "sells": [t for t, _ in sells], "buys": [t for t, _ in buys]}


def signals(conn, a, spec: dict, bar_date: str, prices, fairs, held: dict, history=None):
    """Decide sells (exit conditions on held names) and buys (entry conditions, best DCF upside first, into free
    slots) at bar_date's close and store them as pending → they fill at the next close."""
    aid = a["id"]
    sells, buys = [], []
    history = history or fetch_closes
    hist = {}
    if price_conds(spec, "exit"):
        hist.update(history([t for t in held if t in prices], bar_date))
    for t, p in held.items():
        if t not in prices:
            continue
        px, cap = prices[t]
        s = _series(hist, t, bar_date, px, fairs.get(t), cap)
        sig, why = backtest.decide(spec, s, len(s) - 1, backtest.Pos(True, p["entry_px"], p["peak"], p["held"]))
        if sig == "sell":
            sells.append((t, why))
    free = a["max_pos"] - len(held) + len(sells)
    if free > 0:
        pre, momo = value_only(spec), price_conds(spec, "entry")
        passed = []
        for t, (px, cap) in prices.items():
            fair = fairs.get(t)
            if t in held or not fair or (fair / px - 1) * 100 > MAX_UPSIDE:
                continue
            s = backtest.Series([date.fromisoformat(bar_date)], [px], fair, cap)
            sig, why = backtest.decide(pre, s, 0, backtest.Pos())
            if sig == "buy":
                passed.append(t)
        ranked = []
        if momo:
            got = history(passed, bar_date)
            hist.update(got)
            conn.execute("DELETE FROM scan_momo WHERE account_id=?", (aid,))
        for t in passed:
            px, cap = prices[t]
            fair = fairs[t]
            if momo:
                s = _series(hist, t, bar_date, px, fair, cap)
                sig, why = backtest.decide(spec, s, len(s) - 1, backtest.Pos())
                conn.execute("INSERT OR REPLACE INTO scan_momo VALUES (?,?,?,?)",
                             (aid, t, bar_date, int(sig == "buy") if t in got else -1))
            else:
                s = backtest.Series([date.fromisoformat(bar_date)], [px], fair, cap)
                sig, why = backtest.decide(spec, s, 0, backtest.Pos())
            if sig == "buy":
                ranked.append((fair / px, t, why))
        ranked.sort(reverse=True)
        buys = [(t, why) for _, t, why in ranked[:free]]
    for t, why in sells:
        conn.execute("INSERT OR REPLACE INTO port_pending VALUES (?,?,?,?,?)", (aid, t, "sell", why, bar_date))
    for t, why in buys:
        conn.execute("INSERT OR REPLACE INTO port_pending VALUES (?,?,?,?,?)", (aid, t, "buy", why, bar_date))
    return sells, buys


def update_spec(conn, aid: int, spec: dict, history=None) -> list[str]:
    """Switch an account to a new version of its strategy. Signals from the last close that haven't filled yet are
    re-decided with the new rules (filled trades are kept). Returns the new buy list."""
    check_spec(spec)
    a = account(conn, aid)
    conn.execute("UPDATE port_accounts SET spec=?, name=? WHERE id=?", (json.dumps(spec, ensure_ascii=False), spec["name"], aid))
    bar = a["last_date"]
    if not bar:
        conn.commit()
        return []
    conn.execute("DELETE FROM port_pending WHERE account_id=? AND created=?", (aid, bar))
    prices = {r["ticker"]: (r["price"], r["mcap"]) for r in
              conn.execute("SELECT * FROM scan_universe WHERE quote_date=?", (bar,))}
    held = {p["ticker"]: dict(p) for p in positions(conn, aid)}
    _, buys = signals(conn, account(conn, aid), spec, bar, prices, fair_map(conn), held, history)
    conn.commit()
    return [t for t, _ in buys]


def run_job(conn, now: datetime, push=None, screener=None, spy_quote=None, compute=None, budget_s: float = 2400,
            history=None) -> str:
    init(conn)
    ny = now.astimezone(NY)
    msgs = []
    try:
        rows = fetch_universe(screener)
        store_universe(conn, rows, now)
        msgs.append(f"universe={len(rows)}")
    except Exception as e:
        log.exception("universe fetch failed")
        rows = []
        msgs.append(f"universe failed: {e}")
    after_close = ny.time() >= time(16, 30)
    bar_dates = sorted({r["quote_date"] for r in rows if r["quote_date"]})
    bar = bar_dates[-1] if bar_dates else None
    if rows and after_close and bar == ny.date().isoformat():
        prices = {r["ticker"]: (r["price"], r["mcap"]) for r in rows if r["quote_date"] == bar}
        fairs = fair_map(conn)
        if spy_quote is None:
            from radar.trade import live_quote as spy_quote
        try:
            spy = spy_quote("SPY")
        except Exception:
            spy = None
        n = 0
        for a in conn.execute("SELECT * FROM port_accounts WHERE active=1").fetchall():
            if a["last_date"] and a["last_date"] >= bar:
                continue
            res = step(conn, a, bar, prices, fairs, spy, now, history=history)
            n += 1
            if push and (res["fills"] or res["buys"] or res["sells"]):
                from radar.models import Fire
                parts = []
                if res["fills"]:
                    parts.append("Fills: " + ", ".join(f"{'Buy' if f[1] == 'buy' else 'Sell'} {f[0]} @ {f[2]:.2f}" for f in res["fills"]))
                if res["buys"] or res["sells"]:
                    parts.append("New signals (fill at next trading day close): " + ", ".join(
                        [f"Buy {t}" for t in res["buys"]] + [f"Sell {t}" for t in res["sells"]]))
                try:
                    push(Fire(f"port:{a['id']}:{bar}", "*", "notice", f"Market-wide sim #{a['id']} {a['name']}", "; ".join(parts), True))
                except Exception:
                    pass
        msgs.append(f"accounts={n}")
    r = refresh_fair(conn, now, budget_s=budget_s, compute=compute)
    msgs.append(f"fair ok={r['ok']} failed={r['failed']} left={r['left']}")
    return " ".join(msgs)
