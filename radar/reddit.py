"""Read side of the Reddit feed: top posts of the day + per-ticker mention counts vs prior days.

Crowd sentiment only — noisy, often wrong, sometimes promotional. Shown and passed to Claude with that label."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from radar.db import _ts

LABEL = "Reddit 散户讨论（群体情绪，噪音大，不是事实来源；只看热度和情绪方向）"


def top_posts(conn: sqlite3.Connection, now: datetime, limit: int = 15, hours: float = 30) -> list[sqlite3.Row]:
    """Posts that were in the most recent "top of the day" fetch, best first."""
    last = conn.execute("SELECT MAX(top_seen) AS t FROM reddit_posts WHERE top_seen>=?",
                        (_ts(now - timedelta(hours=hours)),)).fetchone()["t"]
    if not last:
        return []
    return conn.execute("SELECT * FROM reddit_posts WHERE top_seen=? ORDER BY top_rank LIMIT ?", (last, limit)).fetchall()


def coverage_days(conn: sqlite3.Connection, now: datetime) -> float:
    first = conn.execute("SELECT MIN(first_seen) AS f FROM reddit_posts").fetchone()["f"]
    return (now - datetime.fromisoformat(first)).total_seconds() / 86400 if first else 0.0


def mention_counts(conn: sqlite3.Connection, tickers: list[str], now: datetime, base_days: int = 7) -> list[dict]:
    """Per ticker: posts mentioning it in the last 24h vs the daily average over the `base_days` before.

    The baseline only uses days we were already collecting (None until we have ≥1 full prior day)."""
    d1 = _ts(now - timedelta(days=1))
    cov = coverage_days(conn, now)
    days = min(base_days, int(cov - 1)) if cov >= 2 else 0
    d0 = _ts(now - timedelta(days=1 + days))
    out = []
    for t in tickers:
        n24 = conn.execute("SELECT COUNT(*) AS n FROM reddit_mentions WHERE ticker=? AND published>=?", (t, d1)).fetchone()["n"]
        prior = conn.execute("SELECT COUNT(*) AS n FROM reddit_mentions WHERE ticker=? AND published>=? AND published<?",
                             (t, d0, d1)).fetchone()["n"] if days else None
        base = prior / days if days else None
        if not n24 and not prior:
            continue
        out.append({"ticker": t, "n24": n24, "base": base, "days": days,
                    "ratio": n24 / base if base else None})
    return sorted(out, key=lambda r: (-r["n24"], -(r["base"] or 0)))


def ticker_posts(conn: sqlite3.Connection, ticker: str, now: datetime, days: int = 3, limit: int = 5) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.* FROM reddit_mentions m JOIN reddit_posts p ON p.id=m.post_id WHERE m.ticker=? AND m.published>=? "
        "ORDER BY p.published DESC LIMIT ?", (ticker, _ts(now - timedelta(days=days)), limit)).fetchall()


def for_prompt(conn: sqlite3.Connection, tickers: list[str], now: datetime, top: int = 8, per: int = 10) -> list[str]:
    """Bounded summary lines for the daily brief / weekly bundle / agent context."""
    posts = top_posts(conn, now, top)
    counts = mention_counts(conn, tickers, now)[:per]
    if not posts and not counts:
        return ["- 暂无数据"]
    L = ["今日热帖（r/economics、investing、stocks、StockMarket、wallstreetbets 当日 top）："]
    L += [f"- r/{p['subreddit']}：{p['title'][:140]}（{p['published'][:16]} UTC）{p['link']}" for p in posts] or ["- 无"]
    L.append("跟踪标的 24 小时提及帖数（括号内为之前日均；只统计抓到的 listing，绝对数小，看相对变化）：")
    for c in counts:
        base = f"前 {c['days']} 天日均 {c['base']:.1f}" + (f"，×{c['ratio']:.1f}" if c["ratio"] else "") if c["base"] is not None \
            else "历史不足"
        L.append(f"- {c['ticker']}：{c['n24']} 帖（{base}）")
    if not counts:
        L.append("- 无")
    return L
