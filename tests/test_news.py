import json
from datetime import datetime, timedelta, timezone

from radar import db, news
from radar.config import Config

NOW = datetime(2026, 11, 24, 21, 0, tzinfo=timezone.utc)


def rss(*items):
    body = "".join(f"<item><title>{t}</title><link>https://x/{i}</link><pubDate>{d}</pubDate>"
                   f"<source url='https://o'>{s}</source></item>" for i, (t, d, s) in enumerate(items))
    return f"<rss><channel>{body}</channel></rss>"


def fmt(dt):
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


def cfg(tmp_path):
    return Config(holdings=["QCOM"], watchlist=["COST"], db_path=tmp_path / "n.db", quiet_start="23:00", quiet_end="07:00",
                  tz="UTC", news={"people": ["Scott Bessent"], "topics": [], "max_age_min": 120, "push_score": 2})


def test_parse_and_dedupe_across_outlets(tmp_path):
    x = rss(("Bessent says chip curbs could ease - Reuters", fmt(NOW - timedelta(minutes=5)), "Reuters"),
            ("Bessent says chip curbs could ease - CNBC", fmt(NOW - timedelta(minutes=4)), "CNBC"),
            ("Old story", fmt(NOW - timedelta(hours=5)), "AP"))
    items = news.parse_rss(x, "Google News", NOW)
    assert items[0]["id"] == items[1]["id"] and items[0]["source"] == "Reuters"
    conn = db.connect(tmp_path / "n.db")
    fresh = news.store(conn, items, NOW, 120)
    assert [f["title"] for f in fresh] == ["Bessent says chip curbs could ease - Reuters"]
    assert conn.execute("SELECT COUNT(*) FROM news_items").fetchone()[0] == 2
    assert conn.execute("SELECT judged FROM news_items WHERE title='Old story'").fetchone()[0] == 1


def test_feed_urls():
    urls = news.feed_urls({"people": ["Scott Bessent", "Jensen Huang"], "topics": ["tariff"],
                           "feeds": [{"name": "Fed", "url": "https://fed/rss"}]}, ["QCOM", "COST"])
    assert "%22Scott+Bessent%22+OR+%22Jensen+Huang%22+when%3A1h" in urls[0][1]
    assert urls[2] == ("Yahoo Finance", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=QCOM,COST&region=US&lang=en-US")
    assert urls[-1] == ("Fed", "https://fed/rss")


def test_run_job_judges_and_pushes(tmp_path):
    c = cfg(tmp_path)
    conn = db.connect(c.db_path)
    feed = rss(("Bessent signals Nvidia chip sales to China may resume", fmt(NOW - timedelta(minutes=3)), "Bloomberg"),
               ("Celebrity buys a yacht", fmt(NOW - timedelta(minutes=2)), "Tabloid"))
    prompts, pushed = [], []

    def runner(p):
        prompts.append(p)
        return "OK:\n" + json.dumps([
            {"i": 0, "score": 1, "tickers": [], "direction": "unclear", "why": "gossip"},
            {"i": 1, "score": 3, "tickers": ["qcom"], "direction": "positive", "why": "Treasury Secretary hints chip sales to China may resume"}])

    out = news.run_job(conn, c, NOW, pushed.append, runner, fetch=lambda url: feed,
                       quotes=lambda ts: "QCOM 180.00 (+2.1%)")
    assert "new=2" in out and "pushed=1" in out
    assert "QCOM" in prompts[0] and "Bessent signals" in prompts[0]
    [f] = pushed
    assert f.level == "urgent" and f.ticker == "QCOM" and f.rule_id.startswith("news:")
    assert "Treasury Secretary hints" in f.title and "QCOM 180.00" in f.body and "Bloomberg" in f.body
    # nothing new next run → no second Claude call, no repeat push
    news.run_job(conn, c, NOW + timedelta(minutes=3), pushed.append, runner, fetch=lambda url: feed)
    assert len(prompts) == 1 and len(pushed) == 1
    assert [r["score"] for r in news.recent(conn, NOW + timedelta(minutes=3))] == [1, 3]  # newest first


def test_notice_waits_for_quiet_hours_but_urgent_does_not(tmp_path):
    c = cfg(tmp_path)
    conn = db.connect(c.db_path)
    night = datetime(2026, 11, 24, 23, 30, tzinfo=timezone.utc)
    feed = rss(("A", fmt(night), "X"), ("B", fmt(night), "Y"))
    pushed = []
    runner = lambda p: json.dumps([{"i": 0, "score": 2, "tickers": ["COST"], "why": "a"},  # noqa: E731
                                   {"i": 1, "score": 3, "tickers": ["QCOM"], "why": "b"}])
    news.run_job(conn, c, night, pushed.append, runner, fetch=lambda url: feed)
    assert [f.ticker for f in pushed] == ["QCOM"]


def test_bad_json_and_dead_feed(tmp_path):
    c = cfg(tmp_path)
    conn = db.connect(c.db_path)
    calls = iter([rss(("Fresh", fmt(NOW), "X"))])

    def fetch(url):
        try:
            return next(calls)
        except StopIteration:
            raise RuntimeError("down")

    out = news.run_job(conn, c, NOW, lambda f: None, lambda p: "sorry", fetch=fetch)
    assert "pushed=0" in out and "failed=" in out
    assert conn.execute("SELECT judged FROM news_items").fetchone()[0] == 1   # not retried forever


def test_news_page(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "w.db"))
    conn = db.connect(tmp_path / "w.db")
    news.store(conn, news.parse_rss(rss(("Powell hints at a pause", fmt(datetime.now(timezone.utc)), "WSJ")), "G",
                                    datetime.now(timezone.utc)), datetime.now(timezone.utc), 120)
    conn.execute("UPDATE news_items SET judged=1, score=2, tickers='SPY', direction='negative', why='Powell hints at pausing rate cuts'")
    conn.commit()
    from fastapi.testclient import TestClient
    from web.app import app
    r = TestClient(app).get("/news")
    assert r.status_code == 200 and "Powell hints at pausing rate cuts" in r.text and "Powell hints" in r.text


def test_stale_backlog_is_not_pushed_late(tmp_path):
    c = cfg(tmp_path)
    conn = db.connect(c.db_path)
    news.store(conn, news.parse_rss(rss(("Fresh then", fmt(NOW), "X")), "G", NOW), NOW, 120)   # never judged
    pushed = []
    later = NOW + timedelta(hours=3)
    news.run_job(conn, c, later, pushed.append, lambda p: json.dumps([{"i": 0, "score": 3, "why": "x"}]),
                 fetch=lambda url: rss())
    assert pushed == [] and conn.execute("SELECT judged FROM news_items").fetchone()[0] == 1


def test_news_has_its_own_daily_budget(tmp_path):
    from radar.models import Fire
    c = cfg(tmp_path)
    c.daily_cap_notice = 2
    c.news["daily_cap"] = 3
    conn = db.connect(c.db_path)
    for i in range(2):   # the shared budget is already used up by price alerts
        db.record_alert(conn, Fire(f"p{i}", "COST", "notice", "move", "x"), NOW - timedelta(hours=1), pushed=True)
    conn.commit()
    feed = rss(*[(f"Story {i}", fmt(NOW), "X") for i in range(5)])
    pushed = []
    runner = lambda p: json.dumps([{"i": i, "score": 2, "tickers": ["QCOM"], "why": f"w{i}"} for i in range(5)])  # noqa: E731
    news.run_job(conn, c, NOW, pushed.append, runner, fetch=lambda url: feed)
    assert len([f for f in pushed if f.rule_id.startswith("news:")]) == 3


def test_alert_links(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "w.db"))
    from radar.models import Fire
    conn = db.connect(tmp_path / "w.db")
    db.record_alert(conn, Fire("news:abc123", "QCOM", "urgent", "⚡ QCOM↓ test", "x", once=True), datetime.now(timezone.utc), pushed=True)
    conn.commit()
    from fastapi.testclient import TestClient
    from web.app import app
    assert '<a href="/news#n-abc123"><b>⚡ QCOM↓ test</b></a>' in TestClient(app).get("/").text
