"""Real-time news alerts: poll fast news feeds every few minutes, let Claude judge which headlines can move a
holding or the market right now, and push those at once (not in the next daily brief).

Feeds (all free, no key): Google News search for the people / topics in config/news.yaml (last hour), Yahoo
Finance headlines for the tracked tickers, and any extra RSS feeds listed there (CNBC, the Fed, Truth Social).
Same story from several outlets = one row (id = hash of the normalized headline).
"""
from __future__ import annotations

import email.utils
import hashlib
import html
import json
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import httpx

from radar.db import _ts
from radar.models import Fire

log = logging.getLogger(__name__)
UA = "InvestRadar/0.1 (self-hosted personal dashboard; RSS reader)"
Fetch = Callable[[str], str]
MAX_JUDGE = 40          # headlines per Claude call
LEVEL = {3: "urgent", 2: "notice"}


def _plain(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _when(s: str | None, now: datetime) -> datetime:
    try:
        d = email.utils.parsedate_to_datetime(s or "")
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        except ValueError:
            return now


def norm(title: str) -> str:
    """Headline without the ' - Outlet' suffix Google News adds, lower-case, letters/digits only."""
    t = re.sub(r"\s+[-–|]\s+[^-–|]{2,40}$", "", title)
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def item_id(title: str) -> str:
    return hashlib.sha1(norm(title).encode()).hexdigest()[:16]


def parse_rss(xml: str, feed: str, now: datetime) -> list[dict]:
    """RSS 2.0 items → {id, source, title, link, published}. Posts without a title use their text."""
    out = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        log.warning("news feed %s: not XML", feed)
        return out
    for it in root.iter("item"):
        title = _plain(it.findtext("title"))
        if not title or title.startswith("[No Title]"):
            title = _plain(it.findtext("description"))[:240]
        if not title:
            continue
        src = it.find("source")
        source = (src.text or "").strip() if src is not None and src.text else feed
        out.append({"id": item_id(title), "source": source, "title": title,
                    "link": (it.findtext("link") or "").strip(), "published": _when(it.findtext("pubDate"), now)})
    return out


def google_news(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query + ' when:1h')}&hl=en-US&gl=US&ceid=US:en"


def feed_urls(cfg_news: dict, tickers: list[str]) -> list[tuple[str, str]]:
    urls = []
    people, topics = cfg_news.get("people") or [], cfg_news.get("topics") or []
    if people:
        urls.append(("Google News", google_news(" OR ".join(f'"{p}"' for p in people))))
    if topics:
        urls.append(("Google News", google_news(" OR ".join(f'"{t}"' for t in topics))))
    for i in range(0, len(tickers), 10):
        urls.append(("Yahoo Finance", "https://feeds.finance.yahoo.com/rss/2.0/headline?s="
                     + ",".join(tickers[i:i + 10]) + "&region=US&lang=en-US"))
    urls += [(f["name"], f["url"]) for f in cfg_news.get("feeds") or []]
    return urls


def http_fetch(url: str) -> str:
    r = httpx.get(url, headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
    r.raise_for_status()
    return r.text


def store(conn: sqlite3.Connection, items: list[dict], now: datetime, max_age_min: float) -> list[dict]:
    """Insert unseen items; returns the new ones recent enough to judge (older ones are stored as judged)."""
    fresh = []
    cutoff = now - timedelta(minutes=max_age_min)
    for it in items:
        old = it["published"] < cutoff
        cur = conn.execute("INSERT OR IGNORE INTO news_items (id, source, title, link, published, fetched, judged) "
                           "VALUES (?,?,?,?,?,?,?)", (it["id"], it["source"], it["title"], it["link"],
                                                      _ts(it["published"]), _ts(now), int(old)))
        if cur.rowcount and not old:
            fresh.append(it)
    conn.commit()
    return fresh


PROMPT = """You watch the markets. Below are news headlines collected in the last few minutes (mostly English). The user’s holdings and watchlist:
{book}

Already pushed in the last 12 hours:
{recent}

Score each headline on whether it will move the tickers above or the market **right away** (a Treasury Secretary on TV about chip exports to China, a Fed chair speech, a tariff statement, major news from a holding’s CEO, export controls, guidance changes — the statement itself is the signal):
3 = likely to move a holding / the market noticeably within hours (policy statement, regulatory decision, major company event)
2 = relevant to a holding and worth knowing now
1 = relevant but minor / opinion piece
0 = noise, rehashed old news, the same story as one already pushed, gossip

Output only a JSON array, nothing else. Each item: {{"i": index, "score": 0-3, "tickers": ["affected tickers, at most 3; SPY for the market"], "direction": "positive|negative|unclear", "why": "in English, at most 30 words: who said or what happened, and why it affects these tickers"}}

Headlines:
{items}"""


def judge(items: list[dict], book: str, recent: list[str], runner: Callable[[str], str]) -> list[dict]:
    lines = "\n".join(f"{i}. [{it['source']} · {it['published']:%H:%M} UTC] {it['title']}" for i, it in enumerate(items))
    out = runner(PROMPT.format(book=book, recent="\n".join(f"- {r}" for r in recent) or "- None", items=lines))
    m = re.search(r"\[.*\]", out, re.S)
    rows = json.loads(m.group()) if m else []
    res = []
    for r in rows:
        try:
            i = int(r["i"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= i < len(items):
            res.append({**items[i], "score": max(0, min(3, int(r.get("score") or 0))),
                        "tickers": [str(t).upper() for t in (r.get("tickers") or [])][:3],
                        "direction": str(r.get("direction") or ""), "why": str(r.get("why") or "")[:160]})
    return res


def book_text(conn: sqlite3.Connection, holdings: list[str], watchlist: list[str]) -> str:
    names = {r["ticker"]: r["name"] for r in conn.execute("SELECT ticker, name FROM profiles")}
    fmt = lambda ts: ", ".join(f"{t} ({names[t]})" if names.get(t) else t for t in ts) or "None"  # noqa: E731
    return f"Holdings: {fmt(holdings)}\nWatchlist: {fmt(watchlist)}"


def recent_pushed(conn: sqlite3.Connection, now: datetime) -> list[str]:
    rows = conn.execute("SELECT why FROM news_items WHERE score>=2 AND fetched>=? ORDER BY fetched DESC LIMIT 30",
                        (_ts(now - timedelta(hours=12)),))
    return [r["why"] for r in rows if r["why"]]


def save_scores(conn: sqlite3.Connection, scored: list[dict], judged_ids: list[str]) -> None:
    for s in scored:
        conn.execute("UPDATE news_items SET judged=1, score=?, tickers=?, direction=?, why=? WHERE id=?",
                     (s["score"], ",".join(s["tickers"]), s["direction"], s["why"], s["id"]))
    conn.executemany("UPDATE news_items SET judged=1 WHERE id=? AND judged=0", [(i,) for i in judged_ids])
    conn.commit()


def fires(scored: list[dict], push_score: int, quote_line: Callable[[list[str]], str] = lambda ts: "") -> list[Fire]:
    out = []
    for s in scored:
        if s["score"] < push_score:
            continue
        tickers = [t for t in s["tickers"] if t != "SPY"]
        head = ", ".join(s["tickers"]) or "Macro"
        arrow = {"positive": "↑", "negative": "↓"}.get(s["direction"], "")
        body = "\n".join(x for x in [s["why"], f"{s['title']} ({s['source']} {s['published']:%H:%M} UTC) ",
                                     quote_line(tickers)] if x)
        out.append(Fire(f"news:{s['id']}", tickers[0] if tickers else "MACRO", LEVEL.get(s["score"], "notice"),
                        f"⚡ {head}{arrow} {s['why'][:90]}", body, once=True))
    return out


def run_job(conn: sqlite3.Connection, cfg, now: datetime, push, runner: Callable[[str], str] | None,
            fetch: Fetch = http_fetch, quotes: Callable[[list[str]], str] | None = None) -> str:
    from radar.pipeline import deliver

    items, bad = [], []
    for name, url in feed_urls(cfg.news, cfg.all_tickers):
        try:
            items += parse_rss(fetch(url), name, now)
        except Exception as e:  # one dead feed must not stop the others
            bad.append(f"{name}: {type(e).__name__}")
    if not items and bad:
        raise RuntimeError("all news feeds failed: " + "; ".join(bad))
    max_age = float(cfg.news.get("max_age_min", 120))
    fresh = store(conn, items, now, max_age)
    # a backlog left from an earlier run (Claude down, >MAX_JUDGE new items) is no longer news: never push it late
    conn.execute("UPDATE news_items SET judged=1 WHERE judged=0 AND published<?", (_ts(now - timedelta(minutes=max_age)),))
    pending = [dict(r) | {"published": datetime.fromisoformat(r["published"])} for r in conn.execute(
        "SELECT * FROM news_items WHERE judged=0 ORDER BY published DESC LIMIT ?", (MAX_JUDGE,))]
    pushed = 0
    if pending and runner:
        scored = judge(pending, book_text(conn, cfg.holdings, cfg.watchlist), recent_pushed(conn, now), runner)
        save_scores(conn, scored, [p["id"] for p in pending])
        d = deliver(conn, news_cfg(conn, cfg, now), fires(scored, int(cfg.news.get("push_score", 2)), quotes or (lambda ts: "")),
                    now, push)
        pushed = len(d.push)
    return f"feeds={len(feed_urls(cfg.news, cfg.all_tickers)) - len(bad)} new={len(fresh)} judged={len(pending) if runner else 0} " \
           f"pushed={pushed}" + (f" failed={'; '.join(bad)}" if bad else "")


def news_cfg(conn: sqlite3.Connection, cfg, now: datetime):
    """News has its own daily push budget (news.daily_cap, default 30) instead of sharing the 10 notices a day
    the price / filing alerts get — otherwise a busy news day folds headlines into "N more alerts"."""
    import dataclasses

    from radar.pipeline import _local_day_start
    since = _ts(_local_day_start(now, cfg.tz))
    n = lambda extra: conn.execute("SELECT COUNT(*) FROM alerts WHERE level='notice' AND pushed=1 AND ts>=?" + extra,  # noqa: E731
                                   (since,)).fetchone()[0]
    all_today, news_today = n(""), n(" AND rule_id LIKE 'news:%'")
    return dataclasses.replace(cfg, daily_cap_notice=all_today + max(int(cfg.news.get("daily_cap", 30)) - news_today, 0))


def quote_line(tickers: list[str]) -> str:
    """Current price and day change of the affected tickers (fresh quote, not the 30-min snapshot)."""
    if not tickers:
        return ""
    from radar.collectors.prices import fetch_quotes
    try:
        qs = fetch_quotes(tickers, datetime.now(timezone.utc))
    except Exception as e:
        log.info("quote for news push failed: %s", e)
        return ""
    return " · ".join(f"{q.ticker} {q.price:.2f}" + (f" ({q.pct_change:+.1f}%)" if q.pct_change is not None else "")
                      for q in qs)


def recent(conn: sqlite3.Connection, now: datetime, hours: int = 48, min_score: int = 1) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM news_items WHERE published>=? AND judged=1 AND COALESCE(score,0)>=? "
                        "ORDER BY published DESC LIMIT 300", (_ts(now - timedelta(hours=hours)), min_score)).fetchall()
