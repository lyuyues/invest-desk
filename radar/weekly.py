"""Weekly report: build a data bundle from the DB, have Claude (headless) write the report."""
from __future__ import annotations

import logging
import os
import shlex
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from radar import views
from radar.collectors.edgar import item_labels
from radar.config import Config
from radar.db import _ts
from radar.discovery import latest_candidates

log = logging.getLogger(__name__)
PROMPT = Path(__file__).parent / "prompts" / "weekly.md"
DEFAULT_CLAUDE = "claude -p --output-format text --allowedTools WebSearch,WebFetch,mcp__financial-datesets"

Runner = Callable[[str], str]


def _fmt(v, spec="{:.2f}", none="—"):
    return none if v is None else spec.format(v)


def _theses(cfg: Config, tickers: list[str]) -> list[str]:
    if not cfg.theses_dir or not cfg.theses_dir.is_dir():
        return ["(THESES_DIR not configured)"]
    out = []
    for p in sorted(cfg.theses_dir.glob("*.md")):
        if p.name.startswith("_"):
            continue
        ticker_part = p.name.split("-")[0].upper()
        if any(t in ticker_part.split("_") or t == ticker_part for t in tickers) or ticker_part == "LESSONS":
            text = p.read_text(errors="ignore").strip()
            out.append(f"### {p.name}\n{text[:1500]}{' …' if len(text) > 1500 else ''}")
    return out or ["(no matching thesis file)"]


def build_bundle(conn: sqlite3.Connection, cfg: Config, now: datetime) -> str:
    week_ago = now - timedelta(days=7)
    tz = ZoneInfo(cfg.tz)
    L: list[str] = [f"Generated: {now.astimezone(tz):%Y-%m-%d %H:%M} ({cfg.tz})", ""]

    L += ["## Holdings & Watchlist", "| Ticker | Tier | Price | Price time (UTC) | Day (vs prev close) | Week chg | P/E | From 52w high |",
          "|---|---|---|---|---|---|---|---|"]
    for m in views.week_moves(conn, cfg.all_tickers, now):
        L.append(f"| {m['ticker']} | {'Holdings' if cfg.tier_of(m['ticker']) == 'holdings' else 'Watchlist'} | "
                 f"{m['price']:.2f} | {m['ts'][:16]} | {_fmt(m['day_pct'], '{:+.1f}%')} | "
                 f"{_fmt(m['week_pct'], '{:+.1f}%')} | {_fmt(m['pe'], '{:.1f}')} | "
                 f"{_fmt(m['from_high'], '{:+.0f}%')} |")

    since = _ts(week_ago)
    L += ["", "## Alerts this week"]
    alerts = conn.execute("SELECT * FROM alerts WHERE ts>=? ORDER BY ts", (since,)).fetchall()
    L += [f"- [{a['level']}] {a['ts'][:10]} {a['title']} — {a['body'][:160]}" for a in alerts] or ["- None"]

    L += ["", "## 8-K material events (this week)"]
    for f in conn.execute("SELECT f.*, n.summary FROM filings f LEFT JOIN filing_notes n USING (accession) WHERE f.form='8-K' AND f.filed>=? ORDER BY f.filed",
                          (week_ago.date().isoformat(),)):
        L.append(f"- {f['filed']} {f['ticker']}: {item_labels(f['items'] or '')}{(': ' + f['summary']) if f['summary'] else ''} {f['url']}")
    if L[-1].startswith("## 8-K"):
        L.append("- None")

    L += ["", "## Insider trades (this week, open-market buys/sells)"]
    rows = conn.execute("SELECT ticker, code, COUNT(*) n, SUM(value) v, GROUP_CONCAT(DISTINCT owner) owners "
                        "FROM insider_tx WHERE tx_date>=? AND code IN ('P','S') GROUP BY ticker, code",
                        (week_ago.date().isoformat(),)).fetchall()
    L += [f"- {r['ticker']} {'Buy' if r['code'] == 'P' else 'Sell'} {r['n']} trade(s) ${(r['v'] or 0) / 1e6:.2f} M ({r['owners']})"
          for r in rows] or ["- None"]

    L += ["", "## Earnings"]
    for e in conn.execute("SELECT * FROM earnings WHERE date>=? AND date<=? ORDER BY date",
                          (week_ago.date().isoformat(), (now + timedelta(days=14)).date().isoformat())):
        if e["eps_actual"] is not None:
            L.append(f"- Reported {e['date']} {e['ticker']}: EPS {e['eps_actual']} vs est. {e['eps_est']} ({_fmt(e['surprise_pct'], '{:+.1f}%')})")
        else:
            L.append(f"- Upcoming {e['date']} {e['ticker']}: expected EPS {e['eps_est']}")
    if L[-1] == "## Earnings":
        L.append("- None")
    for n in conn.execute("SELECT * FROM earnings_notes WHERE date>=? ORDER BY date", (week_ago.date().isoformat(),)):
        L.append(f"\n### Earnings summary {n['ticker']} {n['date']} (Importance {n['level']})\n{n['markdown'][:1500]}")

    from radar import valuation
    L += ["", "## Valuation (DCF, the user’s valuation engine; Mine = the user’s saved assumptions)", "| Ticker | Price | Fair value · Auto | Fair value · Mine | Upside |", "|---|---|---|---|---|"]
    for r in valuation.summary(conn, cfg.all_tickers):
        if r["auto"] is not None:
            L.append(f"| {r['ticker']} | {_fmt(r['price'])} | {_fmt(r['auto'])} | {_fmt(r['mine'])} | {_fmt(r['upside'], '{:+.0f}%')} |")

    labels = cfg.macro.get("fred") or {}
    L += ["", "## Macro actuals (FRED; daily / weekly are high-frequency, monthly / quarterly lag)", "| Indicator | Frequency | Latest | Date | Weekly change | Note |", "|---|---|---|---|---|---|"]
    for m in views.macro_table(conn, labels):
        if m["value"] is None:
            continue
        note = f"CPI YoY {m['yoy']:.1f}%" if m.get("yoy") is not None else ""
        L.append(f"| {m['label']} | {m.get('freq', '')} | {views.fmt_num(m['value'])} | {m['date']} | {views.fmt_num(m['change'], True, m['value']) if m['change'] is not None else '—'} | {note} |")

    L += ["", "## Fed path (Kalshi implied rate ceiling) and expectation gap"]
    for f in views.fed_path(conn, now):
        line = f"- {f['event']}: implied {_fmt(f['implied'])}%"
        if f["actual"] is not None:
            line += f"; actual {f['actual']:.2f}% → Gap {f['gap_bp']:+.0f}bp"
        L.append(line)
    if L[-1].startswith("## Fed"):
        L.append("- No data yet")

    from radar.collectors.macro import kalshi_series
    series = kalshi_series(cfg.macro.get("markets") or {})
    L += ["", "## Economic data expectations (Kalshi: CPI / unemployment / GDP / recession)"]
    L += views.econ_lines(views.econ_markets(conn, series, now, 24 * 7)) or ["- None yet"]

    L += ["", "## Prediction markets: biggest movers this week"]
    changes = [c for c in views.market_changes(conn, now) if c["delta_pp"] is not None
               and not views.is_econ_ladder(c, series)][:15]
    L += [f"- [{c['source']}] {c['event']} · {c['label']}: {c['before']:.0%} → {c['prob']:.0%} ({c['delta_pp']:+.0f} pp)"
          for c in changes] or ["- Less than a week of history; no change data yet"]

    d, cands = latest_candidates(conn, cfg.discovery.get("top_n", 10))
    from radar import profiles
    prof = profiles.lookup(conn)
    L += ["", f"## Candidate pool ({d or 'Not run yet'}, scored by channel overlap)"]
    for c in cands:
        notes = "; ".join(n for v in c["detail"].values() for n in v[:2])
        p = prof.get(c["ticker"]) or {}
        who = f"{p.get('name_zh') or p.get('name') or ''}, {p.get('desc_zh') or ''}".strip(", ")
        L.append(f"- {c['ticker']}{('(' + who + ')') if who else ''} ({c['score']:.1f}, {'+'.join(c['channels'])}): {notes[:200]}")

    from radar import reddit
    L += ["", f"## {reddit.LABEL}"] + reddit.for_prompt(conn, cfg.all_tickers, now)

    from radar import notes
    L += ["", "## User notes (“Notes” page on the dashboard, last 90 days; the user’s own learning notes / thoughts on earnings and news; when citing, you can say “you noted earlier...”)"]
    L += notes.for_prompt(conn, now) or ["- None"]

    from radar import theses
    L += ["", "## Thesis summaries (“Theses” page on the dashboard)"] + (theses.for_prompt(conn, cfg.all_tickers) or ["- None"])
    if cfg.theses_dir:
        L += _theses(cfg, cfg.all_tickers)
    return "\n".join(L)


def claude_runner(cmd: str | None = None, timeout: int = 1200) -> Runner:
    argv = shlex.split(cmd or os.environ.get("CLAUDE_CMD") or DEFAULT_CLAUDE)

    def run(prompt: str) -> str:
        res = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0 or not res.stdout.strip():
            raise RuntimeError(f"claude exit {res.returncode}: {res.stderr.strip()[:300]}")
        return res.stdout.strip()

    return run


def generate(conn: sqlite3.Connection, cfg: Config, now: datetime, runner: Runner) -> tuple[int, bool]:
    bundle = build_bundle(conn, cfg, now)
    title = f"Weekly report {now.astimezone(ZoneInfo(cfg.tz)):%Y-%m-%d}"
    try:
        md, ok = runner(PROMPT.read_text() + "\n" + bundle), True
    except Exception as e:
        log.warning("claude failed: %s", e)
        md, ok = f"> ⚠️ Claude generation failed ({e}); raw data below.\n\n{bundle}", False
    cur = conn.execute("INSERT INTO reports (created, title, markdown, ok) VALUES (?,?,?,?)",
                       (_ts(now), title, md, int(ok)))
    conn.commit()
    return cur.lastrowid, ok
