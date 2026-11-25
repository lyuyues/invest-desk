"""Faster macro indicators (FRED groups, macro_change rule, Kalshi economy ladders) and the Reddit feed."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from radar import db, jobs, reddit, views
from radar.alerts import events
from radar.collectors import reddit as rc
from radar.collectors.macro import Market, kalshi_series, store_markets
from radar.config import ROOT, Config

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "m.db")
    yield c
    c.close()


# ---- config validation ----

def test_shipped_macro_config_is_consistent():
    m = yaml.safe_load((ROOT / "config" / "macro.yaml").read_text())
    fred = m["fred"]
    grouped = [s for ids in m["fred_groups"].values() for s in ids]
    assert set(grouped) <= set(fred), set(grouped) - set(fred)
    assert len(grouped) == len(set(grouped))
    series = kalshi_series(m["markets"])
    assert series[0] == {"series": "KXFED", "label": "KXFED", "max_events": 3}
    assert {"KXCPIYOY", "KXRECSSNBER"} <= {s["series"] for s in series}


def test_shipped_rules_and_reddit_config():
    rules = yaml.safe_load((ROOT / "config" / "rules.yaml").read_text())["rules"]
    known = {"pct_change", "level_cross", "filing_8k", "insider_buy", "insider_cluster", "earnings", "prob_shift",
             "macro_cross", "macro_change"}
    assert {r["kind"] for r in rules} <= known
    assert len({r["id"] for r in rules}) == len(rules)
    for r in rules:
        assert r.get("level", "notice") in ("urgent", "notice", "log")
        if r["kind"] == "macro_change":
            assert ("pct" in r) if r.get("vs", "avg") == "avg" else ("abs" in r)
    fred = yaml.safe_load((ROOT / "config" / "macro.yaml").read_text())["fred"]
    assert all(r["series"] in fred for r in rules if r["kind"] in ("macro_cross", "macro_change"))
    rd = yaml.safe_load((ROOT / "config" / "reddit.yaml").read_text())
    assert rd["feeds"] and all(f["name"] and f["path"].startswith("r/") and ".rss" in f["path"] for f in rd["feeds"])
    assert any(f["name"] == "top" for f in rd["feeds"])
    assert rd["min_interval_s"] >= 60


def test_kalshi_series_rejects_bad_entry():
    assert kalshi_series({"kalshi_series": ["kxfed", {"series": "KXU3", "label": "Unemployment rate", "max_events": 1}],
                          "kalshi_max_events": 2}) == [
        {"series": "KXFED", "label": "kxfed", "max_events": 2}, {"series": "KXU3", "label": "Unemployment rate", "max_events": 1}]
    with pytest.raises(ValueError):
        kalshi_series({"kalshi_series": [{"label": "no series"}]})


# ---- alert rules ----

def test_macro_change_avg_jump_up_only():
    rule = {"id": "icsa_jump", "kind": "macro_change", "series": "ICSA", "vs": "avg", "window": 4, "pct": 10,
            "direction": "up", "label": "Initial jobless claims", "level": "notice"}
    base = [("2026-10-10", 200000.0), ("2026-10-17", 200000.0), ("2026-10-24", 210000.0), ("2026-10-31", 190000.0)]
    [f] = events.macro_change_fires([rule], {"ICSA": base + [("2026-11-07", 225000.0)]})
    assert f.rule_id == "icsa_jump:2026-11-07" and f.once and "225,000" in f.title and "+12%" in f.body
    assert events.macro_change_fires([rule], {"ICSA": base + [("2026-11-07", 215000.0)]}) == []  # +7.5%
    assert events.macro_change_fires([rule], {"ICSA": base + [("2026-11-07", 170000.0)]}) == []  # down
    assert events.macro_change_fires([rule], {"ICSA": base[-2:] + [("2026-11-07", 300000.0)]}) == []  # short window


def test_macro_change_days_abs_once_per_week():
    rule = {"id": "t10", "kind": "macro_change", "series": "T10YIE", "vs": "days", "days": 7, "abs": 0.15,
            "label": "10Y inflation expectations", "level": "notice"}
    obs = [("2026-11-02", 2.20), ("2026-11-03", 2.22), ("2026-11-09", 2.30), ("2026-11-10", 2.38)]
    [f] = events.macro_change_fires([rule], {"T10YIE": obs})
    assert f.rule_id == "t10:2026-W46" and "+0.16" in f.body  # vs 11-03 (last obs ≥7 days before 11-10)
    [g] = events.macro_change_fires([rule], {"T10YIE": obs + [("2026-11-11", 2.40)]})
    assert g.rule_id == f.rule_id  # same ISO week → policy drops it as already fired
    down = [("2026-11-03", 2.40), ("2026-11-10", 2.20)]
    assert "▼" in events.macro_change_fires([rule], {"T10YIE": down})[0].title
    assert events.macro_change_fires([rule], {"T10YIE": [("2026-11-09", 2.0), ("2026-11-10", 2.5)]}) == []  # no prior week


def test_macro_cross_formats_big_numbers():
    rule = {"id": "icsa", "kind": "macro_cross", "series": "ICSA", "above": 250000, "label": "Initial jobless claims", "level": "notice"}
    [f] = events.macro_cross_fires([rule], {"ICSA": [("a", 1.0), ("d1", 240000.0), ("d2", 260000.0)]})
    assert f.title == "Initial jobless claims rose above 250,000" and "240,000 → 260,000" in f.body


def test_prob_shift_exclude_series():
    rule = {"id": "ps", "kind": "prob_shift", "threshold_pp": 15, "level": "notice", "exclude_series": ["KXCPIYOY"]}
    sh = [{"source": "kalshi", "market_id": "KXCPIYOY-26NOV-T3.8", "event_title": "CPI", "label": "Above 3.8%",
           "prob": 0.8, "prob_before": 0.2},
          {"source": "kalshi", "market_id": "KXRECSSNBER-26", "event_title": "Recession", "label": "Starts",
           "prob": 0.4, "prob_before": 0.1}]
    assert [f.rule_id for f in events.prob_shift_fires([rule], sh)] == ["ps:kalshi:KXRECSSNBER-26"]


def test_macro_job_runs_change_rules(conn):
    csv = "observation_date,ICSA\n" + "\n".join(f"2026-10-{d},200000" for d in (3, 10, 17, 24)) + "\n2026-10-31,260000\n"
    pushed = []
    cfg = Config(holdings=[], watchlist=[], macro={"fred": {"ICSA": "Initial jobless claims"}}, rules=[
        {"id": "j", "kind": "macro_change", "series": "ICSA", "vs": "avg", "window": 4, "pct": 10, "level": "urgent"},
        {"id": "c", "kind": "macro_cross", "series": "ICSA", "above": 250000, "level": "urgent"}])
    out = jobs.macro_job(conn, cfg, NOW, pushed.append, lambda url: httpx.Response(200, text=csv))
    assert "pushed=2" in out and {f.rule_id for f in pushed} == {"j:2026-10-31", "c:2026-10-31"}


# ---- macro views ----

def _obs(conn, series, rows):
    conn.executemany("INSERT INTO macro_obs VALUES (?,?,?)", [(series, d, v) for d, v in rows])


def test_macro_table_prev_change_freq_and_groups(conn):
    _obs(conn, "ICSA", [("2026-10-17", 195000), ("2026-10-24", 195000), ("2026-10-31", 189000)])
    _obs(conn, "DGS10", [("2026-11-05", 4.80), ("2026-11-06", 4.84), ("2026-11-09", 4.82), ("2026-11-10", 4.90)])
    _obs(conn, "GDPNOW", [("2026-04-01", 2.1), ("2026-07-01", 3.27), ("2026-10-01", 3.48)])
    t = views.macro_table(conn, {"ICSA": "Initial claims", "DGS10": "10Y", "GDPNOW": "GDPNow", "X": "missing"})
    by = {m["series"]: m for m in t}
    assert by["ICSA"]["prev_change"] == -6000 and by["ICSA"]["freq"] == "Weekly" and by["ICSA"]["prev_date"] == "2026-10-24"
    assert by["DGS10"]["freq"] == "Daily" and by["DGS10"]["prev_change"] == pytest.approx(0.08)
    assert by["GDPNOW"]["freq"] == "Quarterly"
    groups = views.macro_groups(t, {"Rates": ["DGS10", "NOPE"], "Weekly": ["ICSA"]})
    assert [(g, [m["series"] for m in rows]) for g, rows in groups] == [
        ("Rates", ["DGS10"]), ("Weekly", ["ICSA"]), ("Other", ["GDPNOW", "X"])]
    assert views.macro_groups(t, {})[0][0] == "Macro actuals"
    lines = views.fast_macro_lines(t)
    assert len(lines) == 2 and "189,000" in lines[0] and "-6,000" in lines[0]


def test_fmt_num():
    assert views.fmt_num(189000.0) == "189,000" and views.fmt_num(-6000, True, 189000) == "-6,000"
    assert views.fmt_num(-0.459) == "-0.459" and views.fmt_num(4.84) == "4.84" and views.fmt_num(None) == "—"
    assert views.fmt_num(0.02, True, 4.8) == "+0.02"


def _ladder(event, close, probs, title="CPI YoY in November"):
    return [Market("kalshi", f"{event}-T{k}", event, title, f"Above {k}%", close, p) for k, p in probs]


def test_econ_markets_ladder_and_binary(conn):
    cfg = kalshi_series({"kalshi_series": ["KXFED", {"series": "KXCPIYOY", "label": "CPI YoY"},
                                           {"series": "KXRECSSNBER", "label": "Recession"}]})
    close = "2026-12-10T13:30:00Z"
    store_markets(conn, _ladder("KXCPIYOY-26NOV", close, [("3.6", 0.95), ("3.7", 0.5), ("3.8", 0.1)]),
                  NOW - timedelta(hours=30))
    store_markets(conn, _ladder("KXCPIYOY-26NOV", close, [("3.6", 0.95), ("3.7", 0.7), ("3.8", 0.2)]), NOW)
    store_markets(conn, _ladder("KXCPIYOY-26OCT", "2026-11-01T12:00:00Z", [("3.6", 1.0), ("3.7", 0.0)]), NOW)  # settled
    store_markets(conn, [Market("kalshi", "KXRECSSNBER-27", "KXRECSSNBER-27", "Recession in 2027?", "Yes",
                                "2028-01-31T00:00:00Z", 0.22)], NOW)
    store_markets(conn, [Market("kalshi", "KXFED-26DEC-T3.50", "KXFED-26DEC", "Fed", "Above 3.50%", close, 0.4)], NOW)
    rows = views.econ_markets(conn, cfg, NOW)
    assert [r["series"] for r in rows] == ["KXCPIYOY", "KXRECSSNBER"]
    cpi, rec = rows
    # 3.6 + (0.95 + 0.7 + 0.2) * 0.1 = 3.785 ; 24h ago 3.6 + 1.55 * 0.1 = 3.755
    assert cpi["kind"] == "ladder" and cpi["implied"] == pytest.approx(3.785) and cpi["implied_before"] == pytest.approx(3.755)
    assert cpi["median_above"] == 3.7 and cpi["close"] == "2026-12-10"
    assert rec["kind"] == "binary" and rec["prob"] == 0.22 and rec["prob_before"] is None
    lines = views.econ_lines(rows)
    assert "implied 3.79%" in lines[0] and "+0.03pp" in lines[0] and "probability 22%" in lines[1]
    ch = {c["market_id"]: c for c in views.market_changes(conn, NOW, days=1)}
    assert views.is_econ_ladder(ch["KXCPIYOY-26NOV-T3.7"], cfg)
    assert not views.is_econ_ladder(ch["KXRECSSNBER-27"], cfg) and not views.is_econ_ladder(ch["KXFED-26DEC-T3.50"], cfg)


def test_negative_strike_parsed(conn):
    store_markets(conn, _ladder("KXCPI-26NOV", "2026-12-10T13:30:00Z", [("-0.1", 0.9), ("0.0", 0.3), ("0.1", 0.1)]), NOW)
    [r] = views.econ_markets(conn, [{"series": "KXCPI", "label": "CPI MoM", "max_events": 1}], NOW)
    assert r["implied"] == pytest.approx(-0.1 + 1.3 * 0.1) and r["median_above"] == -0.1


# ---- reddit ----

ATOM = """<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
<title>top</title>
<entry><author><name>/u/alice</name></author><category term="stocks" label="r/stocks"/>
 <content type="html">&lt;div&gt;&lt;p&gt;Thinking about $COST and NFLX here, also IT stocks&lt;/p&gt;&lt;/div&gt; submitted by /u/alice</content>
 <id>t3_aaa</id><link href="https://www.reddit.com/r/stocks/comments/aaa/x/"/>
 <published>2026-11-11T08:00:00+00:00</published><title>Big tech earnings &amp;amp; rates</title></entry>
<entry><author><name>/u/bob</name></author><category term="wallstreetbets" label="r/wallstreetbets"/>
 <content type="html">yolo</content><id>t3_bbb</id><link href="https://www.reddit.com/r/wallstreetbets/comments/bbb/y/"/>
 <published>2026-11-10T20:00:00+00:00</published><title>NVDA_bull says $F to the moon, NOW is the time</title></entry>
<entry><id></id><title>broken</title></entry>
</feed>"""


def test_parse_atom():
    posts = rc.parse_atom(ATOM)
    assert [p.id for p in posts] == ["t3_aaa", "t3_bbb"]
    p = posts[0]
    assert p.subreddit == "stocks" and p.author == "alice" and p.title == "Big tech earnings & rates"  # reddit double-escapes
    assert p.link.endswith("/aaa/x/") and p.published == "2026-11-11T08:00:00+00:00"
    assert "$COST and NFLX" in p.text and "<p>" not in p.text


def test_ticker_matching_rules():
    pats = rc.ticker_patterns(["COST", "NFLX", "F", "NOW", "QCOM", "IT", "META", "AMAT"],
                              require_dollar=["AMAT"], aliases={"META": ["Alphabet"]})
    m = lambda s: rc.match_tickers(s, pats)  # noqa: E731
    assert m("Thinking about $COST and NFLX") == ["COST", "NFLX"]
    assert m("NVDA_bull says $F to the moon, NOW is the time") == ["F"]  # NVDA_ is a username, NOW a word
    assert m("$NOW beat, IT spending up, F150 sales") == ["NOW"]
    assert m("alphabet earnings, AMAT and $AMAT.") == ["META", "AMAT"]
    assert m("cost lowercase, AAPLX, XAAPL") == []


def test_fetch_feeds_spacing_and_429_retry():
    calls, sleeps = [], []
    req = httpx.Request("GET", "https://www.reddit.com/x")

    def get(url):
        calls.append(url)
        if len(calls) == 2:
            raise httpx.HTTPStatusError("429", request=req,
                                        response=httpx.Response(429, headers={"x-ratelimit-reset": "30"}, request=req))
        if "bad" in url:
            raise httpx.HTTPStatusError("403", request=req, response=httpx.Response(403, request=req))
        return httpx.Response(200, text="ok")

    out = rc.fetch_feeds([{"name": "a", "path": "r/a/.rss"}, {"name": "b", "path": "/r/b/.rss"},
                          {"name": "c", "path": "r/bad/.rss"}], get, sleeps.append, 61)
    assert calls[0] == "https://www.reddit.com/r/a/.rss" and calls[1] == calls[2] == "https://www.reddit.com/r/b/.rss"
    assert out["a"] == "ok" and out["b"] == "ok" and isinstance(out["c"], httpx.HTTPStatusError)
    assert sleeps == [61, 32, 61]


def test_collect_store_dedup_and_counts(conn):
    rcfg = {"feeds": [{"name": "top", "path": "r/x/top/.rss"}, {"name": "new", "path": "r/x/new/.rss"}],
            "min_interval_s": 0}
    st = rc.collect(conn, rcfg, ["COST", "NFLX", "F"], NOW, lambda url: httpx.Response(200, text=ATOM), sleep=lambda s: None)
    assert st == {"ok": 2, "failed": [], "posts": 4, "new": 2, "mentions": 6}
    assert conn.execute("SELECT COUNT(*) FROM reddit_posts").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM reddit_mentions").fetchone()[0] == 3
    assert [p["id"] for p in reddit.top_posts(conn, NOW)] == ["t3_aaa", "t3_bbb"]
    assert reddit.top_posts(conn, NOW + timedelta(days=3)) == []
    # second run: nothing new; ranks refreshed
    st2 = rc.collect(conn, rcfg, ["COST"], NOW + timedelta(hours=4), lambda url: httpx.Response(200, text=ATOM),
                     sleep=lambda s: None)
    assert st2["new"] == 0
    # baseline: one day of history → "Not enough history"
    c = {r["ticker"]: r for r in reddit.mention_counts(conn, ["COST", "NFLX", "F", "F"], NOW)}
    assert set(c) == {"COST", "NFLX", "F"} and c["COST"]["n24"] == 1 and c["COST"]["base"] is None
    assert [p["id"] for p in reddit.ticker_posts(conn, "F", NOW)] == ["t3_bbb"]


def test_mention_baseline_and_prompt(conn):
    old = NOW - timedelta(days=5)
    posts = [rc.Post(f"t3_o{i}", "stocks", f"old {i}", f"https://r/{i}", None, db._ts(old - timedelta(hours=i)),
                     tickers=["COST"]) for i in range(4)]
    posts += [rc.Post(f"t3_n{i}", "stocks", f"new {i}", f"https://r/n{i}", None, db._ts(NOW - timedelta(hours=i + 1)),
                      tickers=["COST"]) for i in range(3)]
    rc.store(conn, posts, NOW - timedelta(days=6))
    [c] = reddit.mention_counts(conn, ["COST"], NOW)
    assert c["n24"] == 3 and c["days"] == 5 and c["base"] == pytest.approx(0.8) and c["ratio"] == pytest.approx(3.75)
    lines = reddit.for_prompt(conn, ["COST"], NOW)
    assert any("COST: 3 posts (prior 5-day avg 0.8, ×3.8)" in ln for ln in lines)
    assert reddit.for_prompt(conn, [], NOW - timedelta(days=30)) == ["- No data yet"]


def test_reddit_job_fails_only_when_all_feeds_fail(conn):
    req = httpx.Request("GET", "https://www.reddit.com/x")

    def blocked(url):
        raise httpx.HTTPStatusError("403", request=req, response=httpx.Response(403, request=req))

    cfg = Config(holdings=["COST"], watchlist=[], reddit={"feeds": [{"name": "top", "path": "r/x/.rss"}]})
    with pytest.raises(RuntimeError, match="all reddit feeds failed"):
        jobs.reddit_job(conn, cfg, NOW, blocked, sleep=lambda s: None)
    out = jobs.reddit_job(conn, cfg, NOW, lambda url: httpx.Response(200, text=ATOM), sleep=lambda s: None)
    assert out.startswith("feeds=1/1 posts=2 new=2 mentions=1")


def test_reddit_due():
    from web import scheduler

    assert scheduler.reddit_due(NOW, None, 4)
    assert not scheduler.reddit_due(NOW, db._ts(NOW - timedelta(hours=3)), 4)
    assert scheduler.reddit_due(NOW, db._ts(NOW - timedelta(hours=4, minutes=1)), 4)


# ---- web + bundles ----

@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "watchlist.yaml").write_text("holdings: [COST]\nwatchlist: [F]\n")
    (tmp_path / "rules.yaml").write_text("rules: []\n")
    (tmp_path / "macro.yaml").write_text(
        "fred: {ICSA: Initial jobless claims, NFCI: Chicago Fed financial conditions, DGS10: 10Y Treasury}\n"
        "fred_groups: {Real economy · Weekly: [ICSA], Financial conditions: [NFCI]}\n"
        "markets: {kalshi_series: [KXFED, {series: KXCPIYOY, label: CPI YoY}]}\n")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "r.db"))
    c = db.connect(tmp_path / "r.db")
    _obs(c, "ICSA", [("2026-10-24", 195000), ("2026-10-31", 189000)])
    _obs(c, "NFCI", [("2026-10-23", -0.467), ("2026-10-30", -0.459)])
    _obs(c, "DGS10", [("2026-11-09", 4.82), ("2026-11-10", 4.90)])
    now = datetime.now(timezone.utc)
    store_markets(c, _ladder("KXCPIYOY-26NOV", "2099-12-10T13:30:00Z", [("3.6", 0.95), ("3.7", 0.7), ("3.8", 0.2)]), now)
    rc.collect(c, {"feeds": [{"name": "top", "path": "r/x/.rss"}], "min_interval_s": 0}, ["COST", "F"], now,
               lambda url: httpx.Response(200, text=ATOM.replace("2026-11-11T08:00:00", f"{now - timedelta(hours=1):%Y-%m-%dT%H:%M:%S}")
                                          .replace("2026-11-10T20:00:00", f"{now - timedelta(hours=2):%Y-%m-%dT%H:%M:%S}")),
               sleep=lambda s: None)
    c.close()
    from web.app import app

    return TestClient(app)


def test_macro_page_groups_and_econ(client):
    r = client.get("/macro")
    assert r.status_code == 200
    t = r.text
    assert "Real economy · Weekly" in t and "Financial conditions" in t and "Other" in t
    assert "189,000" in t and "-6,000" in t and "-0.459" in t and "+0.008" in t
    assert "CPI YoY" in t and "3.79%" in t and "above 3.7%" in t


def test_reddit_page_and_agent_context(client, tmp_path):
    r = client.get("/reddit")
    assert r.status_code == 200
    assert "Big tech earnings" in r.text and "noisy" in r.text and 'href="/t/COST"' in r.text
    assert 'href="/reddit"' in r.text  # nav entry
    from radar import agent
    from radar.config import load_config

    cfg = load_config()
    conn = db.connect(cfg.db_path)
    db.apply_tracked(conn, cfg)
    try:
        ctx = agent.page_context(conn, "/reddit")
        assert "Reddit" in ctx and "Big tech earnings" in ctx
        assert "CPI YoY" in agent.page_context(conn, "/macro")
    finally:
        conn.close()
