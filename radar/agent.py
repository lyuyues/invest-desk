"""Dashboard chat agent: `claude -p` in stream-json mode, resumable by session id.

The first turn gets the full data bundle (same one the weekly report uses); every turn gets
the context of the page the user is looking at. Tools: web search / fetch only.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from radar import views, weekly
from radar.collectors.edgar import item_labels
from radar.config import Config

SYSTEM = """You are the user’s investment research assistant, embedded on the right side of the user’s self-built Invest Radar dashboard.
- Answer in English, concisely and directly: conclusion first, then the evidence.
- The user message includes the latest dashboard data (prices, alerts, 8-Ks, insider trades, macro, prediction markets, candidate pool); answer primarily from this data, and use WebSearch / WebFetch only when news or background is needed.
- Timestamp every fact: state when the event happened and when it was disclosed/reported separately (e.g. for a 13F: which quarter the trade happened in and which day it was disclosed). Give source links when citing web pages.
- Do not make up data; if unsure, say so and explain how to verify.
- You may give opinions and reasoning, but clearly distinguish between"what the data shows"and"my judgment"."""

MAX_SECONDS = 300


def agent_argv(session_id: str | None) -> list[str]:
    """Reuse the weekly report's Claude binary (CLAUDE_CMD) with streaming flags."""
    base = os.environ.get("AGENT_CMD")
    if base:
        argv = shlex.split(base)
    else:
        cmd = shlex.split(os.environ.get("CLAUDE_CMD") or weekly.DEFAULT_CLAUDE)
        argv = [cmd[0], "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                "--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch", "--strict-mcp-config",
                "--append-system-prompt", SYSTEM]
    if session_id:
        argv += ["--resume", session_id]
    return argv


def page_context(conn: sqlite3.Connection, page: str) -> str:
    page = (page or "/").split("?")[0]
    if page.startswith("/t/"):
        t = page[3:].upper()
        ev = views.ticker_events(conn, t, limit=8)
        last = conn.execute("SELECT * FROM quotes WHERE ticker=? ORDER BY ts DESC LIMIT 1", (t,)).fetchone()
        L = [f"The user is viewing the stock page for {t}."]
        th = conn.execute("SELECT body, is_default FROM theses WHERE ticker=?", (t,)).fetchone()
        if th:
            L.append(f"User’s investment thesis {' (default, not edited)' if th['is_default'] else ''}: \n{th['body']}")
        if last:
            L.append(f"Last price {last['price']:.2f} ({last['ts'][:16]} UTC), P/E {last['pe'] or '—'}, "
                     f"52-week {last['low52'] or '—'}–{last['high52'] or '—'}")
        L += [f"- 8-K {f['filed']}: {item_labels(f['items'] or '')}{(': ' + f['summary']) if f['summary'] else ''} {f['url']}"
              for f in ev["filings"]]
        L += [f"- Insider {r['tx_date']} {r['owner']} ({r['title'] or ''}) {r['code']} {r['shares'] or 0:,.0f} shares "
              f"@ {r['price'] or 0:.2f}" for r in ev["insiders"]]
        L += [f"- Earnings {r['date']}: EPS actual {r['eps_actual']} / Expected {r['eps_est']}" for r in ev["earnings"]]
        from radar import notes
        mine = notes.for_prompt(conn, datetime.now(timezone.utc), days=365, per_ticker=10, ticker=t)
        if mine:
            L += [f"User’s notes on {t} (past year):"] + mine
        return "\n".join(L)
    if page == "/earnings":
        cfg_t = [r["ticker"] for r in conn.execute("SELECT ticker FROM tracked")]
        b = views.earnings_board(conn, cfg_t, datetime.now())
        L = ["The user is viewing the “Earnings” page. Upcoming:"]
        L += [f"- {e['ticker']} {e['date']} ({e['days']} days away) EPS est. {e['eps_est']}, beats in last 4 quarters {e['beats']}/{e['of']}"
              for e in b["upcoming"]]
        L.append("Recently reported:")
        L += [f"- {e['ticker']} {e['date']} EPS {e['eps_actual']} vs est. {e['eps_est']}, "
              f"Price reaction {'%+.1f%%' % e['move_pct'] if e['move_pct'] is not None else '—'}" for e in b["recent"]]
        return "\n".join(L)
    if page.startswith("/daily"):
        rid = page.rsplit("/", 1)[-1]
        r = conn.execute("SELECT title, markdown FROM reports WHERE kind='daily' AND " +
                         ("id=?" if rid.isdigit() else "1=1 ORDER BY id DESC LIMIT 1"),
                         (int(rid),) if rid.isdigit() else ()).fetchone()
        return f"The user is viewing “{r['title']}”:\n{r['markdown'][:6000]}" if r else "The user is viewing the Daily brief page (no brief yet)."
    if page.startswith("/v/") or page == "/valuation":
        from radar import valuation
        ts = [page[3:].upper()] if page.startswith("/v/") else [r["ticker"] for r in conn.execute("SELECT ticker FROM tracked")]
        L = ["The user is viewing the “Valuation” page (DCF: analyst revenue estimates for the first two years, then growth declines linearly to terminal; FCF margin × revenue, discounted)."]
        for r in valuation.summary(conn, ts):
            v = valuation.load(conn, r["ticker"])
            a = v["data"]["auto"] if v and v["data"] else None
            L.append(f"- {r['ticker']}: price {r['price']}, fair value auto {r['auto'] and round(r['auto'], 2)} / Mine {r['mine'] and round(r['mine'], 2)}, "
                     f"Upside {r['upside'] and round(r['upside'])}%" + (
                f"; auto assumptions rr {a['rr']:.1%} Terminal {a['terminal']:.1%} FCF margin {a['fcf_margin']:.1%} analyst revenue {a['analyst']} starting growth {a['g_start']:.1%}; mine {v['mine']}" if a else f"; {r['error']}"))
        return "\n".join(L)
    if page == "/notes":
        from radar import notes
        mine = notes.for_prompt(conn, datetime.now(timezone.utc), days=365, total=40)
        return "The user is viewing the “Notes” page (the user’s own learning notes / thoughts on earnings and news). Recent notes:\n" + ("\n".join(mine) or "- No notes yet")
    if page == "/theses":
        from radar import theses
        return "The user is viewing the “Theses” page:\n" + "\n\n".join(theses.for_prompt(conn, [r["ticker"] for r in conn.execute("SELECT ticker FROM tracked")]))
    if page == "/reddit":
        from radar import reddit
        now = datetime.now(timezone.utc)
        tickers = [r["ticker"] for r in conn.execute("SELECT ticker FROM tracked")]
        return f"The user is viewing the “Reddit” page. {reddit.LABEL}:\n" + "\n".join(reddit.for_prompt(conn, tickers, now, top=15, per=20))
    if page == "/macro":
        from radar.collectors.macro import kalshi_series
        from radar.config import load_config
        cfg = load_config()
        now = datetime.now(timezone.utc)
        mcfg = cfg.macro.get("markets") or {}
        L = ["The user is viewing the “Macro” page (FRED actuals by group + Kalshi Fed / economic data expectations + Polymarket).", "Economic data expectations (Kalshi):"]
        L += views.econ_lines(views.econ_markets(conn, kalshi_series(mcfg), now, mcfg.get("shift_hours", 24))) or ["- None yet"]
        L += ["High-frequency macro (FRED daily / weekly):"] + (views.fast_macro_lines(views.macro_table(conn, cfg.macro.get("fred") or {})) or ["- None yet"])
        return "\n".join(L)
    names = {"/": "Overview", "/macro": "Macro", "/discover": "Discover (candidate pool)", "/reports": "Weekly reports"}
    if page.startswith("/reports/"):
        return f"The user is viewing weekly report {page}."
    return f"The user is viewing “{names.get(page, page)}” page."


def build_prompt(conn: sqlite3.Connection, cfg: Config, now: datetime, question: str, page: str,
                 first: bool) -> str:
    parts = []
    if first:
        parts.append("# Dashboard data snapshot\n" + weekly.build_bundle(conn, cfg, now))
    parts.append("# Current page\n" + page_context(conn, page))
    parts.append("# Question\n" + question.strip())
    return "\n\n".join(parts)


def parse_event(line: str) -> dict | None:
    """stream-json line → UI event: session / text / tool / done / error."""
    try:
        e = json.loads(line)
    except ValueError:
        return None
    t = e.get("type")
    if t == "system" and e.get("subtype") == "init":
        return {"t": "session", "v": e.get("session_id")}
    if t == "stream_event":
        ev = e.get("event") or {}
        if ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
            return {"t": "text", "v": ev["delta"]["text"]}
        if ev.get("type") == "content_block_start" and (ev.get("content_block") or {}).get("type") in (
                "tool_use", "server_tool_use"):
            return {"t": "tool", "v": ev["content_block"].get("name", "")}
    if t == "result":
        if e.get("is_error") or e.get("subtype") != "success":
            return {"t": "error", "v": str(e.get("result") or e.get("subtype"))[:300]}
        return {"t": "done", "v": e.get("session_id")}
    return None


async def run(argv: list[str], prompt: str, cwd: Path) -> AsyncIterator[dict]:
    cwd.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=8 * 1024 * 1024)
    proc.stdin.write(prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()
    finished = False
    try:
        async with asyncio.timeout(MAX_SECONDS):
            async for raw in proc.stdout:
                ev = parse_event(raw.decode(errors="ignore"))
                if ev:
                    finished |= ev["t"] in ("done", "error")
                    yield ev
        await proc.wait()
        if not finished:
            err = (await proc.stderr.read()).decode(errors="ignore").strip()
            yield {"t": "error", "v": (err or f"claude exited {proc.returncode}")[:300]}
    except TimeoutError:
        yield {"t": "error", "v": f"Not finished after {MAX_SECONDS}s"}
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
