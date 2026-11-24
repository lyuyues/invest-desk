"""Pre-market daily brief (weekdays 09:25 New York): fresh pre-market quotes + overnight events,
written up by Claude in a few minutes. Stored in `reports` with kind='daily'."""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from radar import views
from radar.collectors.edgar import item_labels
from radar.config import Config
from radar.db import _ts
from radar.weekly import Runner, _fmt

log = logging.getLogger(__name__)
PROMPT = Path(__file__).parent / "prompts" / "daily.md"
NY = ZoneInfo("America/New_York")

# Overnight gauges: futures trade through the night, so they show where the open is heading.
GAUGES = {"ES=F": "S&P 500 futures", "NQ=F": "Nasdaq 100 futures", "^VIX": "VIX", "^TNX": "10Y Treasury yield",
          "DX-Y.NYB": "Dollar index", "CL=F": "WTI crude", "GC=F": "Gold", "BTC-USD": "Bitcoin"}


def _num(v) -> float | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def _prev_close(t, today) -> float | None:
    """Last completed daily close before `today` (New York date). Yahoo's own previousClose fields lag a
    day outside regular hours (before Tuesday's open they still show Friday), so derive it from bars."""
    hist = t.history(period="7d", interval="1d", auto_adjust=False)
    closes = [_num(row["Close"]) for ts, row in hist.iterrows() if ts.date() < today]
    closes = [c for c in closes if c is not None]
    return closes[-1] if closes else None


def fetch_premarket(tickers: list[str], now: datetime,
                    ticker_factory: Callable[[str], Any] | None = None) -> dict[str, dict]:
    """ticker -> {pre, prev}: pre-market price (None when there is no pre-market trade) and last close."""
    if ticker_factory is None:
        import yfinance as yf

        ticker_factory = yf.Ticker
    today, out = now.astimezone(NY).date(), {}
    for sym in tickers:
        try:
            t = ticker_factory(sym)
            out[sym] = {"pre": _num(t.info.get("preMarketPrice")), "prev": _prev_close(t, today)}
        except Exception as e:
            log.warning("premarket failed for %s: %s", sym, e)
    return out


def fetch_gauges(now: datetime, ticker_factory: Callable[[str], Any] | None = None) -> dict[str, dict]:
    if ticker_factory is None:
        import yfinance as yf

        ticker_factory = yf.Ticker
    today, out = now.astimezone(NY).date(), {}
    for sym in GAUGES:
        try:
            t = ticker_factory(sym)
            out[sym] = {"last": _num(t.fast_info["lastPrice"]), "prev": _prev_close(t, today)}
        except Exception as e:
            log.warning("gauge failed for %s: %s", sym, e)
    return out


def _pct(a, b):
    return (a / b - 1) * 100 if a is not None and b else None


def build_bundle(conn: sqlite3.Connection, cfg: Config, now: datetime, pre: dict, gauges: dict) -> str:
    since = now - timedelta(hours=24)
    L = [f"Generated: {now.astimezone(NY):%Y-%m-%d %H:%M} New York time (pre-market)", ""]

    L += ["## Pre-market quotes (Holdings & Watchlist)", "| Ticker | Tier | Prev close | Pre-market | Pre-market chg |", "|---|---|---|---|---|"]
    for t in cfg.all_tickers:
        p = pre.get(t) or {}
        L.append(f"| {t} | {'Holdings' if cfg.tier_of(t) == 'holdings' else 'Watchlist'} | {_fmt(p.get('prev'))} | "
                 f"{_fmt(p.get('pre'), none='no pre-market trades')} | {_fmt(_pct(p.get('pre'), p.get('prev')), '{:+.1f}%')} |")

    L += ["", "## Overnight markets", "| Indicator | Latest | Prev close | Change |", "|---|---|---|---|"]
    for sym, label in GAUGES.items():
        g = gauges.get(sym) or {}
        L.append(f"| {label} ({sym}) | {_fmt(g.get('last'))} | {_fmt(g.get('prev'))} | "
                 f"{_fmt(_pct(g.get('last'), g.get('prev')), '{:+.2f}%')} |")

    L += ["", "## Alerts in the past 24 hours"]
    L += [f"- [{a['level']}] {a['ts'][:16]} {a['title']} — {a['body'][:160]}"
          for a in conn.execute("SELECT * FROM alerts WHERE ts>=? ORDER BY ts", (_ts(since),))] or ["- None"]

    day = since.date().isoformat()
    L += ["", "## New 8-Ks (since yesterday)"]
    L += [f"- {f['filed']} {f['ticker']}: {item_labels(f['items'] or '')}{(': ' + f['summary']) if f['summary'] else ''} {f['url']}"
          for f in conn.execute("SELECT f.*, n.summary FROM filings f LEFT JOIN filing_notes n USING (accession) WHERE f.form='8-K' AND f.filed>=? ORDER BY f.filed", (day,))] or ["- None"]

    L += ["", "## Earnings"]
    board = views.earnings_board(conn, cfg.all_tickers, now, recent_days=3)
    L += [f"- Just reported {e['date']} {e['ticker']}: EPS {e['eps_actual']} vs est. {e['eps_est']}"
          f"({_fmt(e['surprise_pct'], '{:+.1f}%')})"
          + (f"; summary tags: {', '.join(e['note']['tags'])}, importance {e['note']['level']}" if e.get("note") else "")
          for e in board["recent"]]
    L += [f"- Upcoming {e['date']} ({e['days']} days away) {e['ticker']}: expected EPS {_fmt(e['eps_est'])}"
          for e in board["upcoming"] if e["days"] <= 7]
    if L[-1] == "## Earnings":
        L.append("- None in the next 7 days")

    L += ["", "## Fed path (Kalshi-implied upper bound)"]
    L += [f"- {f['event']}: implied {_fmt(f['implied'])}%" for f in views.fed_path(conn, now) if f["actual"] is None] or ["- None yet"]
    L += ["", "## Biggest 24-hour prediction-market moves"]
    ch = [c for c in views.market_changes(conn, now, days=1) if c["delta_pp"] is not None and abs(c["delta_pp"]) >= 2][:10]
    L += [f"- {c['event']} · {c['label']}: {c['before']:.0%} → {c['prob']:.0%} ({c['delta_pp']:+.0f} pp)" for c in ch] or ["- No notable change"]
    from radar import reddit
    from radar.collectors.macro import kalshi_series
    mcfg = cfg.macro.get("markets") or {}
    L += ["", "## Economic data expectations (Kalshi)"]
    L += views.econ_lines(views.econ_markets(conn, kalshi_series(mcfg), now, mcfg.get("shift_hours", 24))) or ["- None yet"]
    L += ["", "## High-frequency macro (FRED daily / weekly, latest reading)"]
    L += views.fast_macro_lines(views.macro_table(conn, cfg.macro.get("fred") or {})) or ["- None yet"]
    L += ["", f"## {reddit.LABEL}"] + reddit.for_prompt(conn, cfg.all_tickers, now)
    from radar import theses
    L += ["", "## Investment theses (written by the user in the dashboard; on big moves, check whether the thesis is affected)"] + (theses.for_prompt(conn, cfg.all_tickers) or ["- None"])
    return "\n".join(L)


def generate(conn: sqlite3.Connection, cfg: Config, now: datetime, runner: Runner,
             pre: dict, gauges: dict) -> tuple[int, bool]:
    bundle = build_bundle(conn, cfg, now, pre, gauges)
    title = f"Daily brief {now.astimezone(NY):%Y-%m-%d}"
    try:
        md, ok = runner(PROMPT.read_text() + "\n" + bundle), True
    except Exception as e:
        log.warning("claude failed: %s", e)
        md, ok = f"> ⚠️ Claude generation failed ({e}); raw data below.\n\n{bundle}", False
    cur = conn.execute("INSERT INTO reports (created, title, markdown, ok, kind) VALUES (?,?,?,?, 'daily')",
                       (_ts(now), title, md, int(ok)))
    conn.commit()
    return cur.lastrowid, ok
