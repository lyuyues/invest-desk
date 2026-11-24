"""Strategy lab: rule-based strategies, backtest vs buy-and-hold, robustness checks (educational).

A strategy is a JSON spec of entry and exit conditions:
    {"entry": {"mode": "all", "conds": [{"type": "new_high", "n": 55}]},
     "exit":  {"mode": "any", "conds": [{"type": "new_low", "n": 20}, {"type": "stop_loss", "pct": 10}]}}
Long-only, all-in / all-out. A signal seen at day t's close is executed at day t+1's close (no look-ahead).
Each buy and each sell pays `cost` (fraction: commission + slippage). The same `decide()` drives
backtests and the live paper accounts (radar/paper.py).
"""
from __future__ import annotations

import json
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta

TRADING_DAYS = 252

# type → (label template, params, needs a position). Params: (name, label, default, kind) with kind int | pct.
CONDS: dict[str, tuple[str, list[tuple[str, str, float, str]], bool]] = {
    "new_high":   ("Close at {n}-day high", [("n", "Days", 55, "int")], False),
    "new_low":    ("Close at {n}-day low", [("n", "Days", 20, "int")], False),
    "ret_up":     ("{n}-day gain ≥ {pct}%", [("n", "Days", 3, "int"), ("pct", "%", 5, "pct")], False),
    "ret_down":   ("{n}-day drop ≥ {pct}%", [("n", "Days", 3, "int"), ("pct", "%", 5, "pct")], False),
    "above_ma":   ("Close above {n}-day MA", [("n", "Days", 200, "int")], False),
    "below_ma":   ("Close below {n}-day MA", [("n", "Days", 200, "int")], False),
    "ma_up":      ("{fast}-day MA above {slow}-day MA", [("fast", "Fast MA", 50, "int"), ("slow", "Slow MA", 200, "int")], False),
    "ma_down":    ("{fast}-day MA below {slow}-day MA", [("fast", "Fast MA", 50, "int"), ("slow", "Slow MA", 200, "int")], False),
    "dip":        ("Drawdown from {n}-day high ≥ {pct}%", [("n", "Days", 252, "int"), ("pct", "%", 20, "pct")], False),
    "stop_loss":  ("Loss ≥ {pct}% (stop loss)", [("pct", "%", 10, "pct")], True),
    "trailing":   ("Drawdown from peak since entry ≥ {pct}% (trailing take-profit)", [("pct", "%", 15, "pct")], True),
    "take_profit": ("Gain ≥ {pct}% (take profit)", [("pct", "%", 30, "pct")], True),
    "max_hold":   ("Held for {n} trading days", [("n", "Days", 60, "int")], True),
    # valuation: DCF upside = fair value / price − 1 (fair value from the Valuation panel; your own inputs if set).
    # No point-in-time history of fair values exists → forward paper accounts only, not backtests.
    "val_ge":     ("DCF upside ≥ {pct}%", [("pct", "%", 100, "upct")], False),
    "val_le":     ("DCF upside ≤ {pct}%", [("pct", "%", 5, "upct")], False),
    "mcap_ge":    ("Market cap ≥ {b} x $100M", [("b", "x $100M", 10, "bil")], False),
}
VALUATION_CONDS = ("val_ge", "val_le", "mcap_ge")   # need today's fundamentals → forward paper accounts only


def uses_valuation(spec: dict) -> bool:
    return any(c["type"] in VALUATION_CONDS for side in ("entry", "exit") for c in spec[side]["conds"])

PRESETS: dict[str, dict] = {
    "p:trend": {"name": "Trend following (breakout)", "note": "A systematic version of buying strength and cutting weakness: buy only on a long-period high, exit on a short-period low (Turtle Trading idea)",
                "entry": {"mode": "all", "conds": [{"type": "new_high", "n": 55}]},
                "exit": {"mode": "any", "conds": [{"type": "new_low", "n": 20}]}},
    "p:chase": {"name": "Chase and dump (retail style)", "note": "Buy after short-term gains, sell after short-term drops: a cautionary example of how fees and noise eat returns",
                "entry": {"mode": "all", "conds": [{"type": "ret_up", "n": 3, "pct": 5}]},
                "exit": {"mode": "any", "conds": [{"type": "ret_down", "n": 3, "pct": 5}]}},
    "p:ma200": {"name": "200-day MA timing", "note": "Hold while above the 200-day MA, exit when it breaks below",
                "entry": {"mode": "all", "conds": [{"type": "above_ma", "n": 200}]},
                "exit": {"mode": "any", "conds": [{"type": "below_ma", "n": 200}]}},
    "p:cross": {"name": "MA golden cross / death cross", "note": "Hold when the 50-day MA crosses above the 200-day MA, exit when it crosses below",
                "entry": {"mode": "all", "conds": [{"type": "ma_up", "fast": 50, "slow": 200}]},
                "exit": {"mode": "any", "conds": [{"type": "ma_down", "fast": 50, "slow": 200}]}},
    "p:dip": {"name": "Buy the dip (buy low, sell high)", "note": "Buy after a 20% drop from the 1-year high; sell at +25% or -15%",
              "entry": {"mode": "all", "conds": [{"type": "dip", "n": 252, "pct": 20}]},
              "exit": {"mode": "any", "conds": [{"type": "take_profit", "pct": 25}, {"type": "stop_loss", "pct": 15}]}},
    "p:value": {"name": "Valuation buy-low/sell-high (DCF)", "note": "Buy when the price is half the DCF fair value (upside ≥ 100%), sell when it nears fair value (upside ≤ 5%). Fair value comes from the Valuation panel; there is no historical valuation, so it can only be run forward in strategy simulation",
                "entry": {"mode": "all", "conds": [{"type": "val_ge", "pct": 100}]},
                "exit": {"mode": "any", "conds": [{"type": "val_le", "pct": 5}]}},
}


# ---------- spec handling ----------

def validate(spec: dict) -> dict:
    """Return a clean spec or raise ValueError (message is shown to the user)."""
    out = {"name": str(spec.get("name") or "").strip()[:60] or "Untitled strategy",
           "note": str(spec.get("note") or "").strip()[:500]}
    for side in ("entry", "exit"):
        block = spec.get(side) or {}
        mode = block.get("mode", "all" if side == "entry" else "any")
        if mode not in ("all", "any"):
            raise ValueError("Condition mode must be all / any")
        conds = []
        for c in (block.get("conds") or [])[:8]:
            t = c.get("type")
            if t not in CONDS:
                raise ValueError(f"Unknown condition {t}")
            _, params, needs_pos = CONDS[t]
            if side == "entry" and needs_pos:
                raise ValueError(f"“{describe_cond({'type': t, **{p[0]: p[2] for p in params}})}” can only be used as an exit condition")
            clean = {"type": t}
            for name, label, _, kind in params:
                try:
                    v = float(c[name])
                except (KeyError, TypeError, ValueError):
                    raise ValueError(f"{CONDS[t][0]}: {label} must be a number")
                if kind == "int":
                    v = int(round(v))
                    if not 1 <= v <= 500:
                        raise ValueError(f"{label} must be between 1 and 500")
                elif kind == "bil":
                    if not 0.1 <= v <= 1e6:
                        raise ValueError("Market cap must be between 0.1 and 1,000,000 x $100M")
                elif kind == "upct":
                    if not -90 <= v <= 1000:
                        raise ValueError("Upside must be between −90% and 1000%")
                elif not 0 < v <= 100:
                    raise ValueError(f"{label} must be between 0 and 100%")
                clean[name] = v
            if t in ("ma_up", "ma_down") and clean["fast"] >= clean["slow"]:
                raise ValueError("Fast MA days must be less than slow MA days")
            conds.append(clean)
        out[side] = {"mode": mode, "conds": conds}
    if not out["entry"]["conds"]:
        raise ValueError("At least one entry condition is required")
    return out


def _fmt(v) -> str:
    return f"{v:g}"


def describe_cond(c: dict) -> str:
    return CONDS[c["type"]][0].format(**{k: _fmt(v) for k, v in c.items() if k != "type"})


def describe(spec: dict) -> dict:
    def side(b, verb):
        if not b["conds"]:
            return "No active exit"
        joiner = " AND " if b["mode"] == "all" else " OR "
        return joiner.join(describe_cond(c) for c in b["conds"]) + f" → next close: {verb}"
    return {"entry": side(spec["entry"], "Buy"), "exit": side(spec["exit"], "Sell")}


def warmup(spec: dict) -> int:
    """Bars of history a spec needs before its first signal can fire."""
    need = 1
    for side in ("entry", "exit"):
        for c in spec[side]["conds"]:
            if not CONDS[c["type"]][2]:
                need = max(need, int(c.get("n", 0)), int(c.get("slow", 0)))
    return need + 1


# ---------- indicator series (cached per window, O(n) each) ----------

class Series:
    def __init__(self, dates: list[date], closes: list[float], fair: float | None = None, mcap: float | None = None):
        self.dates, self.c = dates, closes
        self.mcap = mcap            # market cap in USD now (scaled by price for earlier bars)
        self.fair = fair            # DCF fair value (valuation conditions); None = unknown → never true
        self._cache: dict[tuple, list] = {}
        self._pre = [0.0]
        for x in closes:
            self._pre.append(self._pre[-1] + x)

    def __len__(self):
        return len(self.c)

    def sma(self, n: int, t: int) -> float | None:
        return (self._pre[t + 1] - self._pre[t + 1 - n]) / n if t + 1 >= n else None

    def _roll(self, n: int, use_max: bool, include_today: bool) -> list:
        key = (n, use_max, include_today)
        if key not in self._cache:
            out, dq, c = [], deque(), self.c
            for t in range(len(c)):
                # window = c[t-n : t] (previous n bars) or c[t-n+1 : t+1] (including today)
                hi = t + 1 if include_today else t
                if hi - 1 >= 0:
                    j = hi - 1
                    while dq and (c[dq[-1]] <= c[j] if use_max else c[dq[-1]] >= c[j]):
                        dq.pop()
                    dq.append(j)
                while dq and dq[0] < hi - n:
                    dq.popleft()
                out.append(c[dq[0]] if dq and hi - n >= 0 else None)
            self._cache[key] = out
        return self._cache[key]

    def prev_max(self, n, t):
        return self._roll(n, True, False)[t]

    def prev_min(self, n, t):
        return self._roll(n, False, False)[t]

    def max_incl(self, n, t):
        return self._roll(n, True, True)[t]


@dataclass
class Pos:
    holding: bool = False
    entry_px: float = 0.0
    peak: float = 0.0
    held: int = 0          # bars since entry


def _check(c: dict, s: Series, t: int, pos: Pos) -> bool:
    typ, px = c["type"], s.c[t]
    if typ == "new_high":
        m = s.prev_max(c["n"], t)
        return m is not None and px > m
    if typ == "new_low":
        m = s.prev_min(c["n"], t)
        return m is not None and px < m
    if typ in ("ret_up", "ret_down"):
        if t < c["n"]:
            return False
        r = px / s.c[t - c["n"]] - 1
        return r >= c["pct"] / 100 if typ == "ret_up" else r <= -c["pct"] / 100
    if typ in ("above_ma", "below_ma"):
        m = s.sma(c["n"], t)
        return m is not None and (px > m if typ == "above_ma" else px < m)
    if typ in ("ma_up", "ma_down"):
        f, sl = s.sma(c["fast"], t), s.sma(c["slow"], t)
        return f is not None and sl is not None and (f > sl if typ == "ma_up" else f < sl)
    if typ == "dip":
        m = s.max_incl(c["n"], t)
        return m is not None and px <= m * (1 - c["pct"] / 100)
    if typ == "mcap_ge":
        if not s.mcap:
            return False
        return s.mcap * px / s.c[-1] >= c["b"] * 1e8
    if typ in VALUATION_CONDS:
        if not s.fair or s.fair <= 0 or px <= 0:
            return False
        up = (s.fair / px - 1) * 100
        return up >= c["pct"] if typ == "val_ge" else up <= c["pct"]
    if not pos.holding:
        return False
    if typ == "stop_loss":
        return px <= pos.entry_px * (1 - c["pct"] / 100)
    if typ == "trailing":
        return px <= pos.peak * (1 - c["pct"] / 100)
    if typ == "take_profit":
        return px >= pos.entry_px * (1 + c["pct"] / 100)
    if typ == "max_hold":
        return pos.held >= c["n"]
    raise ValueError(typ)


def decide(spec: dict, s: Series, t: int, pos: Pos) -> tuple[str | None, str]:
    """Signal at bar t's close: ("buy" | "sell" | None, reason)."""
    block = spec["exit"] if pos.holding else spec["entry"]
    if not block["conds"]:
        return None, ""
    hits = [c for c in block["conds"] if _check(c, s, t, pos)]
    ok = len(hits) == len(block["conds"]) if block["mode"] == "all" else bool(hits)
    if not ok:
        return None, ""
    why = "; ".join(describe_cond(c) for c in hits)
    if any(c["type"] in ("val_ge", "val_le") for c in hits):
        why += f" (fair value {s.fair:.2f}, close {s.c[t]:.2f}, upside {(s.fair / s.c[t] - 1) * 100:+.0f}%)"
    return ("sell" if pos.holding else "buy"), why


# ---------- simulation ----------

@dataclass
class Trade:
    entry: date
    entry_px: float
    exit: date | None = None
    exit_px: float | None = None
    ret: float | None = None      # net of costs (open trades: marked to market)
    days: int = 0
    why_in: str = ""
    why_out: str = ""

    @property
    def open(self) -> bool:
        return self.exit is None


def simulate(spec: dict, s: Series, cost: float, start: int = 0):
    """Returns (equity list, trades, in-position flags) from bar `start` on (equity starts at 1)."""
    cash, shares, pos = 1.0, 0.0, Pos()
    pending, why = None, ""
    equity, trades, in_pos = [], [], []
    for t in range(start, len(s)):
        px = s.c[t]
        if pending == "buy":
            shares, cash = cash * (1 - cost) / px, 0.0
            pos = Pos(True, px, px, 0)
            trades.append(Trade(s.dates[t], px, why_in=why))
        elif pending == "sell":
            cash, shares = shares * px * (1 - cost), 0.0
            tr = trades[-1]
            tr.exit, tr.exit_px, tr.why_out = s.dates[t], px, why
            tr.ret = px / tr.entry_px * (1 - cost) ** 2 - 1
            tr.days = pos.held
            pos = Pos()
        elif pos.holding:
            pos.held += 1
            pos.peak = max(pos.peak, px)
        equity.append(cash + shares * px)
        in_pos.append(pos.holding)
        pending, why = decide(spec, s, t, pos)
    if trades and trades[-1].open:
        tr = trades[-1]
        tr.ret, tr.days = s.c[-1] / tr.entry_px * (1 - cost) - 1, pos.held
    return equity, trades, in_pos


def metrics(dates: list[date], equity: list[float], trades=None, in_pos=None, base: float | None = None) -> dict:
    """`base` = starting capital (default: the first equity value; pass 1.0 so an entry cost on day 0 counts)."""
    n = len(equity)
    if n < 2 or equity[0] <= 0:
        return {}
    base = base or equity[0]
    years = max((dates[-1] - dates[0]).days / 365.25, 1 / 365.25)
    rets = [equity[i] / equity[i - 1] - 1 for i in range(1, n)]
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1))
    peak, mdd = base, 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    out = {"total": equity[-1] / base - 1, "cagr": (equity[-1] / base) ** (1 / years) - 1,
           "vol": sd * math.sqrt(TRADING_DAYS), "mdd": mdd,
           "sharpe": mean / sd * math.sqrt(TRADING_DAYS) if sd > 0 else None}
    if trades is not None:
        out["trades"] = len(trades)
        out["win"] = sum(t.ret > 0 for t in trades) / len(trades) if trades else None
        out["avg_days"] = sum(t.days for t in trades) / len(trades) if trades else None
    if in_pos is not None:
        out["exposure"] = sum(in_pos) / len(in_pos) if in_pos else 0
    return out


HOLD = {"name": "Buy & hold", "entry": {"mode": "all", "conds": []}, "exit": {"mode": "any", "conds": []}}


@dataclass
class Result:
    key: str
    spec: dict
    equity: list[float]
    trades: list[Trade]
    m: dict
    m_in: dict
    m_out: dict
    cost_drag: float | None = None
    sens: list[dict] = field(default_factory=list)


def _variants(spec: dict):
    """One-at-a-time parameter nudges: ×0.5, ×0.75, ×1.25, ×1.5."""
    for side in ("entry", "exit"):
        for i, c in enumerate(spec[side]["conds"]):
            for name, label, _, kind in CONDS[c["type"]][1]:
                seen = {c[name]}
                for f in (0.5, 0.75, 1.25, 1.5):
                    v = max(1, int(round(c[name] * f))) if kind == "int" else round(c[name] * f, 2)
                    if v in seen or (kind == "pct" and v > 100):
                        continue
                    seen.add(v)
                    alt = json.loads(json.dumps(spec))
                    alt[side]["conds"][i][name] = v
                    try:
                        alt = validate(alt)
                    except ValueError:
                        continue
                    yield f"{describe_cond(c)}: {label} {_fmt(c[name])}→{_fmt(v)}", alt


def run(dates: list[date], closes: list[float], specs: list[tuple[str, dict]], cost: float = 0.001,
        split: float = 0.7, sensitivity: bool = True) -> dict:
    """Backtest each spec plus buy-and-hold. `split` = in-sample share; the rest is the out-of-sample check."""
    s = Series(dates, closes)
    cut = max(1, min(len(dates) - 2, int(len(dates) * split)))
    hold_eq = [c / closes[0] * (1 - cost) for c in closes]
    hold_m = metrics(dates, hold_eq, base=1.0)
    hold_out = metrics(dates[cut:], hold_eq[cut:])

    def pack(key, spec, eq, trades, in_pos):
        return Result(key, spec, eq, trades, metrics(dates, eq, trades, in_pos, base=1.0),
                      metrics(dates[:cut + 1], eq[:cut + 1], base=1.0), metrics(dates[cut:], eq[cut:]))

    hold_tr = [Trade(dates[0], closes[0], ret=hold_eq[-1] - 1, days=len(dates) - 1)]
    results = [pack("hold", HOLD, hold_eq, hold_tr, [True] * len(dates))]
    for key, spec in specs:
        eq, trades, in_pos = simulate(spec, s, cost)
        r = pack(key, spec, eq, trades, in_pos)
        free = simulate(spec, s, 0.0)[0]
        r.cost_drag = metrics(dates, free)["cagr"] - r.m["cagr"]
        if sensitivity:
            for label, alt in _variants(spec):
                e = simulate(alt, s, cost)[0]
                r.sens.append({"label": label, "all": metrics(dates, e, base=1.0)["cagr"] - hold_m["cagr"],
                               "out": metrics(dates[cut:], e[cut:])["cagr"] - hold_out["cagr"]})
        results.append(r)
    return {"results": results, "cut": cut, "cut_date": dates[cut], "hold": hold_m, "hold_out": hold_out}


# ---------- data ----------

_cache: dict[tuple, tuple[float, list[date], list[float]]] = {}


def fetch_history(ticker: str, years: float, max_age: float = 6 * 3600,
                  ticker_factory=None) -> tuple[list[date], list[float]]:
    """Daily closes adjusted for splits and dividends (so buy-and-hold includes dividends).
    During market hours the last bar is today's partial bar (yfinance behaviour)."""
    key = (ticker, years)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < max_age:
        return hit[1], hit[2]
    if ticker_factory is None:
        import yfinance as yf
        ticker_factory = yf.Ticker
    start = date.today() - timedelta(days=int(years * 365.25) + 5)
    hist = ticker_factory(ticker).history(start=start.isoformat(), interval="1d", auto_adjust=True)
    dates, closes = [], []
    for ts, row in hist.iterrows():
        c = row.get("Close")
        if c is None or c != c or c <= 0:
            continue
        dates.append(ts.date())
        closes.append(float(c))
    _cache[key] = (time.time(), dates, closes)
    return dates, closes
