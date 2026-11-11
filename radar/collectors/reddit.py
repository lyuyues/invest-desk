"""Reddit crowd sentiment from public subreddit RSS (Atom) listings — no account, no API key.

Unauthenticated RSS is rate limited to roughly one request a minute per IP, and search.rss is refused
(403) from datacenter-ish IPs, so we fetch a couple of multi-subreddit listings (config/reddit.yaml) slowly
and find ticker mentions in the post titles + bodies ourselves.
"""
from __future__ import annotations

import html
import logging
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from radar.db import _ts

log = logging.getLogger(__name__)

BASE = "https://www.reddit.com/"
DEFAULT_UA = "InvestRadar/0.1 (self-hosted personal dashboard; RSS reader, ~6 requests/day)"
ATOM = {"a": "http://www.w3.org/2005/Atom"}

# Upper-case words that show up in post titles and collide with real tickers: only "$WORD" counts.
COMMON = {
    "A", "I", "AI", "ALL", "AM", "AN", "ANY", "ARE", "AT", "ATH", "BE", "BIG", "BY", "CAN", "CEO", "CFO", "CPI", "DD",
    "EOD", "EPS", "ETF", "EU", "EV", "FAST", "FED", "FOR", "FUN", "GDP", "GO", "GOOD", "HAS", "HE", "HOLD", "HUGE",
    "IMO", "IPO", "IRS", "IT", "KEY", "LOVE", "LOW", "MAN", "NEW", "NEXT", "NOW", "ON", "ONE", "OPEN", "OR", "OUT",
    "PLAY", "POST", "REAL", "RUN", "SAFE", "SEC", "SO", "TRUE", "TV", "UK", "US", "USA", "USD", "WELL", "WSB", "YOLO",
}


@dataclass
class Post:
    id: str
    subreddit: str
    title: str
    link: str
    author: str | None
    published: str          # ISO UTC
    text: str = ""          # title + body (plain text), only used for matching
    tickers: list[str] = field(default_factory=list)


def _plain(s: str | None) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).replace("\xa0", " ")


def _iso(s: str | None) -> str:
    try:
        return _ts(datetime.fromisoformat((s or "").replace("Z", "+00:00")))
    except ValueError:
        return _ts(datetime.now(timezone.utc))


def parse_atom(text: str) -> list[Post]:
    root = ET.fromstring(text)
    out = []
    for e in root.findall("a:entry", ATOM):
        pid = (e.findtext("a:id", "", ATOM) or "").strip()
        title = html.unescape((e.findtext("a:title", "", ATOM) or "").strip())
        link = e.find("a:link", ATOM)
        cat = e.find("a:category", ATOM)
        if not pid or not title or link is None:
            continue
        out.append(Post(
            id=pid, subreddit=(cat.get("term") if cat is not None else "") or "", title=title,
            link=link.get("href", ""), author=(e.findtext("a:author/a:name", "", ATOM) or "").removeprefix("/u/") or None,
            published=_iso(e.findtext("a:published", None, ATOM) or e.findtext("a:updated", None, ATOM)),
            text=title + "\n" + _plain(e.findtext("a:content", "", ATOM))))
    return out


def ticker_patterns(tickers: list[str], require_dollar: list[str] | None = None,
                    aliases: dict | None = None) -> dict[str, re.Pattern]:
    """ticker -> regex. "$TICK" always counts; bare upper-case TICK only for ≥3 letters that aren't common words."""
    strict = COMMON | {t.upper() for t in require_dollar or []}
    out = {}
    for t in tickers:
        sym = re.escape(t.upper())
        alts = [rf"\${sym}(?![A-Za-z0-9_])"]
        if len(t) >= 3 and t.upper() not in strict:
            alts.append(rf"(?<![A-Za-z0-9_$]){sym}(?![A-Za-z0-9_])")
        alts += [rf"(?i:(?<![A-Za-z0-9_]){re.escape(a)}(?![A-Za-z0-9_]))" for a in (aliases or {}).get(t, []) or []]
        out[t] = re.compile("|".join(alts))
    return out


def match_tickers(text: str, patterns: dict[str, re.Pattern]) -> list[str]:
    return [t for t, p in patterns.items() if p.search(text)]


def make_getter(ua: str | None = None) -> Callable[[str], httpx.Response]:
    ua = ua or os.environ.get("REDDIT_USER_AGENT") or DEFAULT_UA

    def get(url: str) -> httpx.Response:
        r = httpx.get(url, headers={"User-Agent": ua}, timeout=30, follow_redirects=True)
        r.raise_for_status()
        return r

    return get


def fetch_feeds(feeds: list[dict], get, sleep=time.sleep, interval: float = 61) -> dict[str, str | Exception]:
    """name -> Atom text (or the exception). Spaced `interval` s apart; one retry after a 429."""
    out: dict[str, str | Exception] = {}
    for i, f in enumerate(feeds):
        if i:
            sleep(interval)
        url = BASE + f["path"].lstrip("/")
        for attempt in (1, 2):
            try:
                out[f["name"]] = get(url).text
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt == 1:
                    wait = float(e.response.headers.get("x-ratelimit-reset") or interval)
                    log.info("reddit 429 on %s, waiting %.0fs", f["name"], wait + 2)
                    sleep(min(wait + 2, 120))
                    continue
                out[f["name"]] = e
                break
            except Exception as e:  # network / parse
                out[f["name"]] = e
                break
    return out


def store(conn: sqlite3.Connection, posts: list[Post], now: datetime, top: bool = False) -> int:
    """Upsert posts (+ ticker mentions). Returns the number of posts not seen before."""
    ts, new = _ts(now), 0
    for rank, p in enumerate(posts, 1):
        cur = conn.execute(
            "INSERT INTO reddit_posts (id, subreddit, title, link, author, published, score, first_seen, top_seen, top_rank) "
            "VALUES (?,?,?,?,?,?,NULL,?,?,?) ON CONFLICT(id) DO NOTHING",
            (p.id, p.subreddit, p.title, p.link, p.author, p.published, ts, ts if top else None, rank if top else None))
        new += cur.rowcount
        if top:
            conn.execute("UPDATE reddit_posts SET top_seen=?, top_rank=?, title=? WHERE id=?", (ts, rank, p.title, p.id))
        conn.executemany("INSERT OR IGNORE INTO reddit_mentions VALUES (?,?,?)",
                         [(p.id, t, p.published) for t in p.tickers])
    conn.commit()
    return new


def collect(conn: sqlite3.Connection, rcfg: dict, tickers: list[str], now: datetime, get, sleep=time.sleep) -> dict:
    feeds = rcfg.get("feeds") or []
    pats = ticker_patterns(tickers, rcfg.get("require_dollar"), rcfg.get("aliases"))
    res = fetch_feeds(feeds, get, sleep, rcfg.get("min_interval_s", 61))
    stats = {"ok": 0, "failed": [], "posts": 0, "new": 0, "mentions": 0}
    for f in feeds:
        r = res.get(f["name"])
        if not isinstance(r, str):
            stats["failed"].append(f"{f['name']}: {type(r).__name__}: {r}"[:200])
            continue
        try:
            posts = parse_atom(r)
        except ET.ParseError as e:
            stats["failed"].append(f"{f['name']}: bad feed: {e}"[:200])
            continue
        for p in posts:
            p.tickers = match_tickers(p.text, pats)
        stats["ok"] += 1
        stats["posts"] += len(posts)
        stats["mentions"] += sum(len(p.tickers) for p in posts)
        stats["new"] += store(conn, posts, now, top=f["name"] == "top")
    return stats
