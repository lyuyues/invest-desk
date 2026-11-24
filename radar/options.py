"""Options for the /trade paper accounts (learning; no real orders).

Allowed, all without margin:
  - buy calls / puts (max loss = premium paid)
  - sell covered calls (every short call is backed by 100 held shares, which are then locked)
  - sell cash-secured puts (strike × 100 of cash is reserved per short put)
Quotes are yfinance option chains: buy at the ask, sell at the bid (mid when marking positions).
Resting limit orders fill during market hours against the live bid/ask, and after the close against the
contract's daily bar. At expiry the job settles at the underlying close: out of the money expires worthless;
in the money closes at intrinsic value, and shares are delivered (exercise / assignment) when the account can
take them, otherwise it is cash-settled. Contract multiplier 100, fee 0.65 per contract.
"""
from __future__ import annotations

import math
import re
import time as _time
from datetime import date, datetime, time, timedelta, timezone

from radar import trade
from radar.trade import NY, OrderError, _iso

MULT = 100
FEE = 0.65
RATE = 0.04          # risk-free rate for Black–Scholes greeks
OCC = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS opt_orders (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    created TEXT NOT NULL,
    contract TEXT NOT NULL,       -- OCC symbol, e.g. AAPL261218C00200000
    underlying TEXT NOT NULL,
    right TEXT NOT NULL CHECK (right IN ('C', 'P')),
    strike REAL NOT NULL,
    expiry TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    kind TEXT NOT NULL CHECK (kind IN ('market', 'limit')),
    qty INTEGER NOT NULL,         -- contracts
    px REAL,                      -- limit, per share
    status TEXT NOT NULL CHECK (status IN ('open', 'filled', 'cancelled', 'rejected')),
    filled_at TEXT,
    fill_px REAL,                 -- per share premium
    fee REAL,
    reason TEXT NOT NULL,
    how TEXT NOT NULL DEFAULT 'trade',   -- trade | expire | exercise | assign | cash
    review TEXT,
    msg TEXT
);
CREATE INDEX IF NOT EXISTS opt_orders_acct ON opt_orders (account_id, status);
"""


def init(conn) -> None:
    trade.init(conn)


def parse(sym: str) -> dict:
    m = OCC.match(sym.upper().strip())
    if not m:
        raise OrderError("Invalid option symbol")
    u, d, r, k = m.groups()
    return {"contract": m.group(0), "underlying": u, "right": r, "strike": int(k) / 1000,
            "expiry": date(2000 + int(d[:2]), int(d[2:4]), int(d[4:]))}


def label(info) -> str:
    return f"{info['underlying']} {str(info['expiry'])} {info['strike']:g} {'Call' if info['right'] == 'C' else 'Put'}"


def bs(spot: float, k: float, t: float, iv: float, right: str, r: float = RATE) -> tuple[float, float]:
    """Black–Scholes (price, delta) per share; t in years."""
    if t <= 0 or iv <= 0 or spot <= 0:
        intr = max(0.0, spot - k) if right == "C" else max(0.0, k - spot)
        return intr, (1.0 if spot > k else 0.0) if right == "C" else (-1.0 if spot < k else 0.0)
    n = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))  # noqa: E731
    d1 = (math.log(spot / k) + (r + iv * iv / 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if right == "C":
        return spot * n(d1) - k * math.exp(-r * t) * n(d2), n(d1)
    return k * math.exp(-r * t) * n(-d2) - spot * n(-d1), n(d1) - 1


def years_left(expiry: date, now: datetime) -> float:
    end = datetime.combine(expiry, time(16, 0), NY)
    return max(0.0, (end - now.astimezone(NY)).total_seconds() / (365 * 86400))


# ---------- quotes ----------

_ecache: dict[str, tuple[float, list[str]]] = {}
_ccache: dict[tuple[str, str], tuple[float, dict]] = {}


def expiries(u: str, ticker_factory=None) -> list[str]:
    hit = _ecache.get(u)
    if hit and _time.time() - hit[0] < 600:
        return hit[1]
    if ticker_factory is None:
        import yfinance as yf
        ticker_factory = yf.Ticker
    out = list(ticker_factory(u).options or [])
    _ecache[u] = (_time.time(), out)
    return out


def _f(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return v if v == v else 0.0


def chain(u: str, expiry: str, ticker_factory=None) -> dict:
    """{'spot', 'calls': [row], 'puts': [row], 'by': {contract: row}}; cached 60 s."""
    hit = _ccache.get((u, expiry))
    if hit and _time.time() - hit[0] < 60:
        return hit[1]
    if ticker_factory is None:
        import yfinance as yf
        ticker_factory = yf.Ticker
    oc = ticker_factory(u).option_chain(expiry)
    spot = _f((getattr(oc, "underlying", None) or {}).get("regularMarketPrice"))
    out = {"spot": spot or None, "calls": [], "puts": [], "by": {}}
    for key, df in (("calls", oc.calls), ("puts", oc.puts)):
        for r in df.to_dict("records"):
            bid, ask, last = _f(r.get("bid")), _f(r.get("ask")), _f(r.get("lastPrice"))
            row = {"contract": r["contractSymbol"], "strike": _f(r["strike"]), "bid": bid, "ask": ask, "last": last,
                   "iv": _f(r.get("impliedVolatility")), "oi": int(_f(r.get("openInterest"))),
                   "vol": int(_f(r.get("volume"))), "right": "C" if key == "calls" else "P",
                   "mark": (bid + ask) / 2 if bid > 0 and ask > 0 else last}
            out[key].append(row)
            out["by"][row["contract"]] = row
    _ccache[(u, expiry)] = (_time.time(), out)
    return out


def quote(contract: str) -> dict:
    info = parse(contract)
    row = chain(info["underlying"], info["expiry"].isoformat())["by"].get(info["contract"])
    if not row:
        raise ValueError(f"Not found in the option chain: {label(info)}")
    return row


# ---------- positions ----------

def book(conn, aid: int) -> tuple[dict, list[dict]]:
    """Open positions {contract: {qty (signed contracts), avg (premium/share), ...}} and closed trips."""
    pos: dict[str, dict] = {}
    trips: list[dict] = []
    for f in conn.execute("SELECT * FROM opt_orders WHERE account_id=? AND status='filled' ORDER BY filled_at, id",
                          (aid,)).fetchall():
        p = pos.get(f["contract"])
        if p is None:
            p = pos[f["contract"]] = {"qty": 0, "avg": 0.0, "realized": 0.0, "orders": [], "premium": 0.0,
                                      **parse(f["contract"])}
        signed = f["qty"] if f["side"] == "buy" else -f["qty"]
        if p["qty"] == 0 or (p["qty"] > 0) == (signed > 0):          # opening / adding
            if p["qty"] == 0:
                p.update(opened=f["filled_at"], short=signed < 0, orders=[], realized=0.0, premium=0.0)
            p["avg"] = (p["avg"] * abs(p["qty"]) + f["fill_px"] * f["qty"]) / (abs(p["qty"]) + f["qty"])
            p["premium"] += f["fill_px"] * f["qty"] * MULT
        else:                                                         # closing
            p["realized"] += (f["fill_px"] - p["avg"]) * f["qty"] * MULT * (1 if p["qty"] > 0 else -1)
        p["realized"] -= f["fee"] or 0
        p["qty"] += signed
        p["orders"].append(f)
        if p["qty"] == 0:
            first = p["orders"][0]
            trips.append({**{k: p[k] for k in ("contract", "underlying", "right", "strike", "expiry", "opened", "short")},
                          "closed": f["filled_at"], "pnl": p["realized"],
                          "ret": p["realized"] / p["premium"] if p["premium"] else 0.0, "how": f["how"],
                          "reason": first["reason"], "plan_stop": None, "orders": list(p["orders"]),
                          "last_id": f["id"], "review": f["review"], "label": label(p)})
            del pos[f["contract"]]
    return pos, trips


def constraints(conn, aid: int, pos: dict | None = None) -> tuple[float, dict[str, int]]:
    """(cash reserved for short puts, {ticker: shares locked by short calls})."""
    if pos is None:
        pos, _ = book(conn, aid)
    reserved, locked = 0.0, {}
    for p in pos.values():
        if p["qty"] < 0 and p["right"] == "P":
            reserved += p["strike"] * MULT * -p["qty"]
        if p["qty"] < 0 and p["right"] == "C":
            locked[p["underlying"]] = locked.get(p["underlying"], 0) + MULT * -p["qty"]
    return reserved, locked


def _check(conn, aid: int, cash: float, contract: str | None = None, dqty: int = 0, stock_delta: dict | None = None):
    """Error text if the account would break a no-margin rule after a change, else None."""
    pos, _ = book(conn, aid)
    if contract:
        info = parse(contract)
        p = pos.setdefault(contract, {"qty": 0, **info})
        if p["qty"] and (p["qty"] > 0) != (p["qty"] + dqty > 0) and p["qty"] + dqty != 0:
            return f"You can close at most the existing {abs(p['qty'])} contracts; you can’t reverse the position in one order"
        p["qty"] += dqty
    reserved, locked = constraints(conn, aid, pos)
    if cash < reserved - 1e-6:
        return (f"Not enough cash: selling the put requires reserving {reserved:,.0f} to buy the shares, but cash after the fill would be only {cash:,.0f}"
                if contract else f"Not enough cash: {reserved:,.0f} of it is already reserved for sold puts")
    stocks, _ = trade.book(conn, aid)
    for t, need in locked.items():
        have = stocks.get(t, {}).get("qty", 0) + (stock_delta or {}).get(t, 0)
        if have + 1e-9 < need:
            return (f"Not enough shares to cover: selling the call requires holding {need:g} shares of {t}, you have only {have:g}" if contract
                    else f"These shares are locked by a sold call ({need:g} shares of {t}); buy back the call before selling")
    return None


def orders(conn, aid: int, status: str | None = None):
    init(conn)
    q = "SELECT * FROM opt_orders WHERE account_id=?" + (" AND status=?" if status else "") + " ORDER BY id DESC"
    return conn.execute(q, (aid, status) if status else (aid,)).fetchall()


def value(conn, aid: int, now: datetime, q=None, spot=None) -> tuple[float, list[dict], list[str]]:
    """Market value of option positions (mid), display rows, errors."""
    q, spot = q or quote, spot or trade.live_quote
    pos, _ = book(conn, aid)
    total, rows, errs = 0.0, [], []
    for c, p in sorted(pos.items(), key=lambda x: (x[1]["underlying"], x[1]["expiry"], x[1]["strike"])):
        row, s = None, None
        try:
            s = spot(p["underlying"])
        except Exception as e:
            errs.append(f"{p['underlying']} quote failed: {e}")
        expired = p["expiry"] < now.astimezone(NY).date() or years_left(p["expiry"], now) == 0
        if not expired:
            try:
                row = q(c)
            except Exception as e:
                errs.append(f"{label(p)} quote failed: {e}")
        if row and row["mark"] > 0:
            mark, iv = row["mark"], row["iv"]
        elif s is not None:
            mark, iv = bs(s, p["strike"], years_left(p["expiry"], now), row["iv"] if row else 0, p["right"])[0], 0
        else:
            mark, iv = p["avg"], 0
        delta = bs(s, p["strike"], years_left(p["expiry"], now), iv, p["right"])[1] if s else None
        mv = mark * MULT * p["qty"]
        total += mv
        first = p["orders"][0]
        rows.append({"contract": c, "label": label(p), "qty": p["qty"], "avg": p["avg"], "mark": mark, "value": mv,
                     "pnl": (mark - p["avg"]) * MULT * p["qty"], "spot": s, "delta": delta, "iv": iv,
                     "dte": (p["expiry"] - now.astimezone(NY).date()).days, "underlying": p["underlying"],
                     "right": p["right"], "strike": p["strike"], "expiry": p["expiry"],
                     "itm": s is not None and (s > p["strike"] if p["right"] == "C" else s < p["strike"]),
                     "reason": first["reason"], "opened": first["filled_at"][:10]})
    return total, rows, errs


# ---------- orders ----------

def _fill(conn, o, px: float, when: datetime, note: str = "") -> bool:
    a = trade.account(conn, o["account_id"])
    fee = FEE * o["qty"]
    signed = o["qty"] if o["side"] == "buy" else -o["qty"]
    cash = a["cash"] - signed * px * MULT - fee
    err = _check(conn, o["account_id"], cash, o["contract"], signed)
    if err is None and cash < -1e-6:
        err = f"Insufficient cash: need {px * MULT * o['qty'] + fee:,.2f}, available {a['cash']:,.2f}"
    if err:
        conn.execute("UPDATE opt_orders SET status='rejected', msg=? WHERE id=?", (err, o["id"]))
        return False
    conn.execute("UPDATE opt_orders SET status='filled', filled_at=?, fill_px=?, fee=?, msg=? WHERE id=?",
                 (_iso(when), px, fee, note or None, o["id"]))
    conn.execute("UPDATE trade_accounts SET cash=? WHERE id=?", (cash, o["account_id"]))
    return True


def _live_px(o, row) -> tuple[float | None, str]:
    """Price an order would fill at now (None = not marketable)."""
    side_px = row["ask"] if o["side"] == "buy" else row["bid"]
    if o["kind"] == "market":
        if side_px > 0:
            return side_px, ""
        return (row["last"], "No bid/ask quote; filled at the last trade price") if row["last"] > 0 else (None, "")
    if side_px > 0 and (side_px <= o["px"] if o["side"] == "buy" else side_px >= o["px"]):
        return side_px, ""
    return None, ""


def place(conn, aid: int, f: dict, now: datetime, q=quote) -> int:
    """f: contract, side, kind, qty, px, reason. Returns the order id (filled, open or rejected)."""
    init(conn)
    a = trade.account(conn, aid)
    if not a or not a["active"]:
        raise OrderError("Account does not exist or is closed")
    info = parse(f.get("contract") or "")
    side, kind = f.get("side"), f.get("kind") or "market"
    if side not in ("buy", "sell") or kind not in ("market", "limit"):
        raise OrderError("Invalid order type")
    if years_left(info["expiry"], now) <= 0:
        raise OrderError("This contract has already expired")
    reason = (f.get("reason") or "").strip()
    if not reason:
        raise OrderError("Write one sentence on why you are making this trade. This is the key to learning")
    try:
        qty = float(f["qty"])
        px = float(f["px"]) if f.get("px") not in (None, "") else None
    except (TypeError, ValueError, KeyError):
        raise OrderError("Quantity / price must be numbers")
    if qty <= 0 or qty != int(qty) or qty > 500:
        raise OrderError("Contracts must be an integer from 1 to 500")
    if kind == "limit" and not (px and px > 0):
        raise OrderError("Limit orders need a price")
    row = q(info["contract"])
    cur = conn.execute(
        "INSERT INTO opt_orders (account_id, created, contract, underlying, right, strike, expiry, side, kind, qty, px, "
        "status, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, _iso(now), info["contract"], info["underlying"], info["right"], info["strike"], info["expiry"].isoformat(),
         side, kind, int(qty), px, "open", reason[:1000]))
    o = conn.execute("SELECT * FROM opt_orders WHERE id=?", (cur.lastrowid,)).fetchone()
    fill, note = _live_px(o, row)
    if fill is not None:
        if not trade.market_open(now):
            note = ("Outside market hours: filled at the closing quote; " + note).rstrip("; ")
        _fill(conn, o, fill, now, note)
    elif kind == "market":
        conn.execute("UPDATE opt_orders SET status='rejected', msg=? WHERE id=?", ("This contract has no quote right now", o["id"]))
    else:
        err = _check(conn, aid, a["cash"] - (px * MULT * qty if side == "buy" else 0) + (px * MULT * qty if side == "sell" else 0),
                     info["contract"], int(qty) if side == "buy" else -int(qty))
        if err:
            conn.execute("UPDATE opt_orders SET status='rejected', msg=? WHERE id=?", (err, o["id"]))
    conn.commit()
    return o["id"]


def cancel(conn, aid: int, oid: int) -> None:
    init(conn)
    conn.execute("UPDATE opt_orders SET status='cancelled' WHERE id=? AND account_id=? AND status='open'", (oid, aid))
    conn.commit()


def set_review(conn, aid: int, oid: int, text: str) -> None:
    init(conn)
    conn.execute("UPDATE opt_orders SET review=? WHERE id=? AND account_id=?", (text.strip()[:2000] or None, oid, aid))
    conn.commit()


def sweep_live(conn, aid: int, now: datetime, q=quote) -> int:
    """During market hours: fill resting limit orders the live bid/ask now reaches."""
    if not trade.market_open(now):
        return 0
    n = 0
    for o in conn.execute("SELECT * FROM opt_orders WHERE account_id=? AND status='open' ORDER BY id", (aid,)).fetchall():
        try:
            fill, _ = _live_px(o, q(o["contract"]))
        except Exception:
            continue
        if fill is not None:
            n += _fill(conn, o, fill, now, "Intraday quote fill")
    conn.commit()
    return n


def _deliver(conn, aid: int, ticker: str, side: str, shares: int, px: float, when: datetime, reason: str) -> None:
    conn.execute("INSERT INTO trade_orders (account_id, created, ticker, side, kind, qty, status, filled_at, fill_px, fee, "
                 "reason, msg) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (aid, _iso(when), ticker, side, "market", shares, "filled", _iso(when), px, 0.0, reason, "Option delivery"))
    conn.execute("UPDATE trade_accounts SET cash=cash+? WHERE id=?", ((px if side == "sell" else -px) * shares, aid))


def settle(conn, aid: int, done: date, bars=trade.daily_bars) -> int:
    """Settle positions that expired on or before `done` at the underlying's close."""
    pos, _ = book(conn, aid)
    n = 0
    closes: dict[str, list[dict]] = {}
    for c, p in sorted(pos.items(), key=lambda x: (x[1]["qty"] > 0, x[1]["expiry"])):   # shorts first: frees shares/cash
        if p["expiry"] > done:
            continue
        if p["underlying"] not in closes:
            closes[p["underlying"]] = bars(p["underlying"], p["expiry"] - timedelta(days=10))
        bar = [b for b in closes[p["underlying"]] if b["date"] <= p["expiry"]]
        if not bar:
            continue
        s = bar[-1]["close"]
        intr = max(0.0, s - p["strike"]) if p["right"] == "C" else max(0.0, p["strike"] - s)
        if intr < 0.01:
            intr = 0.0
        when = datetime.combine(p["expiry"], time(16, 0), NY)
        q = abs(p["qty"])
        shares = q * MULT
        long, call = p["qty"] > 0, p["right"] == "C"
        how, note = "expire", f"Expired out of the money, voided ({p['underlying']} close {s:.2f})"
        deliver = None
        if intr:
            a = trade.account(conn, aid)
            cash_after = a["cash"] + (intr if long else -intr) * shares
            stocks, _ = trade.book(conn, aid)
            if not long:
                how = "assign"
                deliver = ("sell" if call else "buy")
                note = (f"Assigned at strike {p['strike']:g}: sold {shares} shares" if call else f"Assigned at strike {p['strike']:g}: bought {shares} shares")
            elif call and cash_after >= s * shares:
                how, deliver, note = "exercise", "buy", f"Auto-exercised at strike {p['strike']:g}: bought {shares} shares"
            elif not call and stocks.get(p["underlying"], {}).get("qty", 0) >= shares:
                how, deliver, note = "exercise", "sell", f"Auto-exercised at strike {p['strike']:g}: sold {shares} shares"
            else:
                how, note = "cash", f"Expired in the money, cash-settled at intrinsic value {intr:.2f} (the account cannot take delivery of these shares)"
            note += f"({p['underlying']} close {s:.2f})"
        conn.execute(
            "INSERT INTO opt_orders (account_id, created, contract, underlying, right, strike, expiry, side, kind, qty, status, "
            "filled_at, fill_px, fee, reason, how, msg) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, _iso(when), c, p["underlying"], p["right"], p["strike"], p["expiry"].isoformat(),
             "sell" if long else "buy", "market", q, "filled", _iso(when), intr, 0.0, "Expiry settlement", how, note))
        conn.execute("UPDATE trade_accounts SET cash=cash+? WHERE id=?", ((intr if long else -intr) * shares, aid))
        if deliver:
            # option closed at intrinsic + shares at the close  ==  shares at the strike
            _deliver(conn, aid, p["underlying"], deliver, shares, s, when, f"{label(p)} {note.split('(')[0]}")
        n += 1
    conn.commit()
    return n


def run_job(conn, now: datetime, bars=trade.daily_bars) -> str:
    """After the close: match resting limit orders on the contract's daily bar, then settle expiries."""
    init(conn)
    ny = now.astimezone(NY)
    done = ny.date() if ny.time() >= time(16, 15) else ny.date() - timedelta(days=1)
    filled = 0
    for o in conn.execute("SELECT o.* FROM opt_orders o JOIN trade_accounts a ON a.id=o.account_id "
                          "WHERE o.status='open' AND a.active=1 ORDER BY o.id").fetchall():
        start = trade._first_bar_day(o["created"])
        exp = date.fromisoformat(o["expiry"])
        try:
            hist = bars(o["contract"], start - timedelta(days=7))
        except Exception:
            hist = []
        for bar in hist:
            if start <= bar["date"] <= min(done, exp):
                px = trade.match_bar(o, bar)
                if px is not None:
                    filled += _fill(conn, o, px, datetime.combine(bar["date"], time(16, 0), NY), f"{bar['date']} daily-bar fill")
                    break
        else:
            if exp <= done:
                conn.execute("UPDATE opt_orders SET status='cancelled', msg='Contract expired, open order cancelled' WHERE id=?", (o["id"],))
    conn.commit()
    settled = 0
    for a in conn.execute("SELECT id FROM trade_accounts WHERE active=1").fetchall():
        try:
            settled += settle(conn, a["id"], done, bars)
        except Exception:
            continue
    return f"filled={filled} settled={settled}"
