"""Event rules: turn new filings / transactions / observations into Fires. Pure functions.

Event fires embed the event id in rule_id (e.g. "8k_material:0001193125-26-448357") so the
24h cooldown is per event, not per rule.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

from radar.models import Fire


def _rules(rules: list[dict], kind: str) -> list[dict]:
    return [r for r in rules if r["kind"] == kind]


def _tier_ok(rule: dict, tier: str) -> bool:
    return rule.get("tier", "all") in ("all", tier)


def filing_fires(rules, filings, tier_of: Callable[[str], str], today: date,
                 labels: Callable[[str], str]) -> list[Fire]:
    out = []
    for r in _rules(rules, "filing_8k"):
        wanted = set(r.get("items") or [])
        max_age = timedelta(days=r.get("max_age_days", 3))
        for f in filings:
            items = {c.strip() for c in f.items.split(",") if c.strip()}
            if not _tier_ok(r, tier_of(f.ticker)) or not (items & wanted):
                continue
            if date.fromisoformat(f.filed) < today - max_age:
                continue
            out.append(Fire(f"{r['id']}:{f.accession}", f.ticker, r["level"],
                            f"{f.ticker} 8-K：{labels(f.items)}", f"{f.filed} · {f.url}", once=True))
    return out


def insider_buy_fires(rules, txs, tier_of, today: date) -> list[Fire]:
    out = []
    for r in _rules(rules, "insider_buy"):
        max_age = timedelta(days=r.get("max_age_days", 14))
        for t in txs:
            if t.code != "P" or t.value is None or t.value < r["min_value"]:
                continue
            if not _tier_ok(r, tier_of(t.ticker)) or not t.tx_date:
                continue
            if date.fromisoformat(t.tx_date) < today - max_age:
                continue
            who = f"{t.owner}（{t.title}）" if t.title else t.owner
            out.append(Fire(f"{r['id']}:{t.accession}:{t.idx}", t.ticker, r["level"],
                            f"{t.ticker} 内部人买入 ${t.value / 1e6:.2f}M",
                            f"{who} {t.tx_date} 买入 {t.shares:,.0f} 股 @ {t.price:.2f}", once=True))
    return out


def insider_cluster_fires(rules, purchases_by_ticker: dict[str, list[dict]], tier_of) -> list[Fire]:
    """purchases_by_ticker: ticker -> open-market purchases within the rule window,
    each {"owner", "accession", "value"}; newest accession last."""
    out = []
    for r in _rules(rules, "insider_cluster"):
        for ticker, buys in purchases_by_ticker.items():
            owners = {b["owner"] for b in buys}
            if len(owners) < r.get("min_insiders", 2) or not _tier_ok(r, tier_of(ticker)):
                continue
            total = sum(b["value"] or 0 for b in buys)
            latest = max(b["accession"] for b in buys)
            out.append(Fire(f"{r['id']}:{latest}", ticker, r["level"],
                            f"{ticker} 内部人集体买入：{len(owners)} 人",
                            f"近 {r.get('days', 30)} 天共 ${total / 1e6:.2f}M：" + "、".join(sorted(owners)), once=True))
    return out


def earnings_fires(rules, rows, tier_of, today: date) -> list[Fire]:
    """rows: newly reported earnings dicts {ticker, date, eps_est, eps_actual, surprise_pct}."""
    out = []
    for r in _rules(rules, "earnings"):
        max_age = timedelta(days=r.get("max_age_days", 3))
        for e in rows:
            if e["eps_actual"] is None or not _tier_ok(r, tier_of(e["ticker"])):
                continue
            if date.fromisoformat(e["date"]) < today - max_age:
                continue
            s = e["surprise_pct"]
            verdict = "超预期" if s is not None and s > 0 else ("不及预期" if s is not None and s < 0 else "")
            est = f"{e['eps_est']:.2f}" if e["eps_est"] is not None else "—"
            out.append(Fire(f"{r['id']}:{e['date']}", e["ticker"], r["level"],
                            f"{e['ticker']} 财报{verdict} {s:+.1f}%" if s is not None else f"{e['ticker']} 发布财报",
                            f"EPS {e['eps_actual']:.2f} vs 预期 {est}（{e['date']}）", once=True))
    return out


def prob_shift_fires(rules, shifts: list[dict]) -> list[Fire]:
    """shifts: {source, market_id, event_title, label, prob, prob_before}."""
    out = []
    for r in _rules(rules, "prob_shift"):
        skip = tuple(f"{x}-" for x in r.get("exclude_series") or [])
        for s in shifts:
            if s["prob_before"] is None or (skip and s["market_id"].startswith(skip)):
                continue
            delta = (s["prob"] - s["prob_before"]) * 100
            if abs(delta) < r["threshold_pp"]:
                continue
            out.append(Fire(f"{r['id']}:{s['source']}:{s['market_id']}", "MACRO", r["level"],
                            f"预期变化 {delta:+.0f}pp：{s['label']}",
                            f"{s['event_title']} · {s['label']}：{s['prob_before']:.0%} → {s['prob']:.0%}"
                            f"（{r.get('hours', 24)}h，{s['source']}）"))
    return out


def _n(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:g}"


def macro_cross_fires(rules, latest: dict[str, list[tuple[str, float]]]) -> list[Fire]:
    """latest: series -> recent observations [(date, value), ...] oldest first (only the last two are used)."""
    out = []
    for r in _rules(rules, "macro_cross"):
        obs = latest.get(r["series"]) or []
        if len(obs) < 2:
            continue
        (_, prev), (d, cur) = obs[-2], obs[-1]
        label = r.get("label", r["series"])
        if "above" in r and prev <= r["above"] < cur:
            out.append(Fire(f"{r['id']}:{d}", "MACRO", r["level"],
                            f"{label} 升破 {_n(r['above'])}", f"{label} {_n(prev)} → {_n(cur)}（{d}）", once=True))
        if "below" in r and prev >= r["below"] > cur:
            out.append(Fire(f"{r['id']}:{d}", "MACRO", r["level"],
                            f"{label} 跌破 {_n(r['below'])}", f"{label} {_n(prev)} → {_n(cur)}（{d}）", once=True))
    return out


def macro_change_fires(rules, latest: dict[str, list[tuple[str, float]]]) -> list[Fire]:
    """Jumps in a FRED series. latest: series -> recent observations, oldest first.

    Rule fields: series, label, level, direction (up | down | both, default both), and one of
      vs: avg   window: N obs, pct: P   → latest vs the mean of the N observations before it, change ≥ P%
      vs: days  days: D,       abs: A   → latest vs the last observation ≥ D days earlier, |change| ≥ A (units of the series)
    avg rules fire once per new observation; days rules at most once per ISO week (a move on a daily series
    stays "≥ A over D days" for several days in a row)."""
    out = []
    for r in _rules(rules, "macro_change"):
        obs = latest.get(r["series"]) or []
        if len(obs) < 2:
            continue
        d, cur = obs[-1]
        label, direction = r.get("label", r["series"]), r.get("direction", "both")
        if r.get("vs", "avg") == "avg":
            window = obs[-1 - r.get("window", 4):-1]
            if len(window) < r.get("window", 4):
                continue
            base = sum(v for _, v in window) / len(window)
            if not base:
                continue
            delta = (cur / base - 1) * 100
            if abs(delta) < r["pct"]:
                continue
            what, key = f"较前 {len(window)} 期均值 {_n(base)} {delta:+.0f}%", d
        else:
            cutoff = (date.fromisoformat(d) - timedelta(days=r.get("days", 7))).isoformat()
            prior = [(pd, v) for pd, v in obs[:-1] if pd <= cutoff]
            if not prior:
                continue
            pd, base = prior[-1]
            delta = cur - base
            if abs(delta) < r["abs"]:
                continue
            y, w, _ = date.fromisoformat(d).isocalendar()
            what, key = f"{r.get('days', 7)} 天内 {delta:+.2f}（{pd} {_n(base)} → {_n(cur)}）", f"{y}-W{w:02d}"
        if (direction == "up" and delta < 0) or (direction == "down" and delta > 0):
            continue
        arrow = "▲" if delta > 0 else "▼"
        out.append(Fire(f"{r['id']}:{key}", "MACRO", r["level"], f"{label} {arrow} {_n(cur)}",
                        f"{label} {_n(cur)}（{d}），{what}", once=True))
    return out
