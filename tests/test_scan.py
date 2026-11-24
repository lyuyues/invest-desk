from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from radar import backtest, db, scan

NY = ZoneInfo("America/New_York")
SPEC = backtest.validate({"name": "v", "entry": {"mode": "all", "conds": [{"type": "val_ge", "pct": 80},
                                                                         {"type": "mcap_ge", "b": 10}]},
                          "exit": {"mode": "any", "conds": [{"type": "val_le", "pct": 5}]}})


def at(day, hh=17):
    return datetime(2026, 11, day, hh, 0, tzinfo=NY)


def screener_for(prices):
    """prices: {ticker: (price, mcap)} → fake yf.screen with paging."""
    def screen(q, size, offset, **k):
        items = list(prices.items())
        ts = int(datetime(2026, 11, DAY[0], 16, 0, tzinfo=NY).timestamp())
        page = items[offset:offset + size]
        return {"total": len(items), "quotes": [{"symbol": t, "shortName": t, "quoteType": "EQUITY", "exchange": "NYQ",
                                                 "regularMarketPrice": p, "marketCap": c, "regularMarketTime": ts,
                                                 "financialCurrency": "BRL" if t == "FX" else "USD"}
                                                for t, (p, c) in page]}
    return screen


DAY = [10]
FAIR = {"AAA": 100.0, "BBB": 100.0, "CCC": 100.0, "TINY": 100.0, "JUNK": 1000.0}


def compute(t, px, cap):
    if t == "ERR":
        raise ValueError("no 10-K")
    return FAIR.get(t), 0.1, 0.05, 0.09


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "s.db")
    scan.init(c)
    return c


def test_check_spec():
    scan.check_spec(SPEC)
    with pytest.raises(ValueError, match="上涨空间"):
        scan.check_spec(backtest.PRESETS["p:trend"])        # momentum alone isn't enough: needs a valuation entry
    with pytest.raises(ValueError, match="上涨空间"):
        scan.check_spec(backtest.validate({"entry": {"conds": [{"type": "mcap_ge", "b": 10}]}}))


def test_portfolio_lifecycle(conn):
    aid = scan.open_account(conn, {"id": "3", "spec": SPEC}, 10000, 0.0, 2, at(9), bench_px=500.0)
    # day 10: AAA +150%, BBB +100%, CCC +25%, TINY cheap but tiny, JUNK absurd upside (bad data), ERR no DCF
    DAY[0] = 10
    px = {"AAA": (40.0, 5e9), "BBB": (50.0, 5e9), "CCC": (80.0, 5e9), "TINY": (10.0, 5e8), "JUNK": (1.0, 5e9),
          "ERR": (5.0, 5e9), "FX": (1.0, 5e9)}
    out = scan.run_job(conn, at(10), screener=screener_for(px), spy_quote=lambda t: 500.0, compute=compute)
    assert "universe=6" in out                                    # FX (non-USD financials) dropped and "fair ok=5 failed=1" in out
    # fair values were computed after stepping → no signals on the very first day
    assert not conn.execute("SELECT * FROM port_pending").fetchall()
    assert [c["ticker"] for c in scan.candidates(conn, SPEC)] == ["AAA", "BBB"]

    DAY[0] = 11
    scan.run_job(conn, at(11), screener=screener_for(px), spy_quote=lambda t: 500.0, compute=compute)
    pend = {r["ticker"]: r["side"] for r in conn.execute("SELECT * FROM port_pending")}
    assert pend == {"AAA": "buy", "BBB": "buy"}                 # best upside first, 2 slots; TINY/JUNK filtered

    DAY[0] = 12
    px.update(AAA=(42.0, 5e9), BBB=(96.0, 9e9))                  # BBB ran to +4% → sell signal
    scan.run_job(conn, at(12), screener=screener_for(px), spy_quote=lambda t: 550.0, compute=compute)
    pos = {p["ticker"]: p for p in scan.positions(conn, aid)}
    assert set(pos) == {"AAA", "BBB"} and pos["AAA"]["entry_px"] == 42.0
    assert {r["ticker"]: r["side"] for r in conn.execute("SELECT * FROM port_pending")} == {"BBB": "sell"}
    eq = conn.execute("SELECT * FROM port_equity WHERE date='2026-11-12'").fetchone()
    assert eq["equity"] == pytest.approx(10000) and eq["bench"] == pytest.approx(11000)

    DAY[0] = 13
    px.update(BBB=(98.0, 9e9))
    scan.run_job(conn, at(13), screener=screener_for(px), spy_quote=lambda t: 550.0, compute=compute)
    assert set(p["ticker"] for p in scan.positions(conn, aid)) == {"AAA"}
    a = scan.account(conn, aid)
    assert a["cash"] == pytest.approx(5000 / 96 * 98)
    # BBB's slot is free again; CCC (+25%) doesn't qualify → nothing new
    assert not conn.execute("SELECT * FROM port_pending").fetchall()
    # re-running the same day is a no-op for the account
    scan.run_job(conn, at(13, 18), screener=screener_for(px), spy_quote=lambda t: 550.0, compute=compute)
    assert conn.execute("SELECT COUNT(*) FROM port_fills").fetchone()[0] == 3


def test_own_dcf_wins(conn, monkeypatch):
    from radar import valuation
    conn.execute("INSERT INTO scan_fair VALUES ('AAA', '2026-11-10T00:00:00', 50.0, NULL, NULL, NULL, NULL)")
    conn.execute("INSERT INTO valuations VALUES ('AAA', '2026-11-10', '{}', NULL)")
    monkeypatch.setattr(valuation, "fair_value", lambda c, t: 120.0)
    assert scan.fair_map(conn)["AAA"] == 50.0                        # auto: the scan's fresher value
    conn.execute("INSERT INTO valuation_inputs VALUES ('AAA', '{}', '2026-11-10')")
    assert scan.fair_map(conn)["AAA"] == 120.0                       # your own inputs win


def test_scan_due():
    from web.scheduler import scan_due
    ny = lambda d, h, m=0: datetime(2026, 11, d, h, m, tzinfo=NY)  # noqa: E731
    iso = lambda dt: dt.astimezone(timezone.utc).isoformat()  # noqa: E731
    assert scan_due(ny(10, 17), iso(ny(10, 15)), False)          # bootstrap run earlier today doesn't block the close run
    assert not scan_due(ny(10, 17), iso(ny(10, 16, 50)), False)
    assert not scan_due(ny(10, 12), iso(ny(10, 11)), True)        # behind, but only 1 h ago... (60 < 70 min)
    assert scan_due(ny(10, 12), iso(ny(10, 10, 30)), True)


def test_scan_pages(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from radar import trade
    from web import scheduler
    (tmp_path / "watchlist.yaml").write_text("holdings: []\nwatchlist: []\n")
    (tmp_path / "rules.yaml").write_text("rules: []\n")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "w.db"))
    monkeypatch.setattr(trade, "live_quote", lambda t, *a, **k: 500.0)
    spawned = []

    async def fake_spawn(job):
        spawned.append(job)
    monkeypatch.setattr(scheduler, "spawn", fake_spawn)
    from web.app import app
    client = TestClient(app)
    r = client.get("/paper")
    assert "全市场组合" in r.text and "估值低买高卖（DCF）" in r.text
    r = client.post("/scan", data={"strategy": "p:trend", "capital": "100000", "max_pos": "10"}, follow_redirects=True)
    assert "需要一个「DCF 上涨空间" in r.text
    r = client.post("/scan", data={"strategy": "p:value", "capital": "100000", "max_pos": "5"}, follow_redirects=False)
    loc = r.headers["location"]
    page = client.get(loc)
    assert page.status_code == 200 and "0 / 5" in page.text and "DCF 上涨空间" in page.text
    assert spawned == ["scan"]
    # a saved (editable) strategy: editing it offers 改用新版本 on the portfolio page
    import json as _j
    sid = client.post("/sim/strategy", json={"spec": SPEC}).json()["id"]
    loc = client.post("/scan", data={"strategy": sid, "capital": "100000", "max_pos": "5"}, follow_redirects=False).headers["location"]
    assert "策略改过了" not in client.get(loc).text
    new = _j.loads(_j.dumps(SPEC))
    new["entry"]["conds"].append({"type": "above_ma", "n": 50})
    client.post("/sim/strategy", json={"spec": new, "id": sid})
    assert "策略改过了" in client.get(loc).text
    r = client.post(loc + "/sync", follow_redirects=True)
    assert r.status_code == 200 and "策略改过了" not in r.text and "动量" in r.text


def test_valuation_page_builds_untracked(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import web.app as webapp
    (tmp_path / "watchlist.yaml").write_text("holdings: []\nwatchlist: []\n")
    (tmp_path / "rules.yaml").write_text("rules: []\n")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "w.db"))
    built = []
    monkeypatch.setattr(webapp, "_refresh_valuation", lambda ts: built.extend(ts))
    r = TestClient(webapp.app).get("/v/ZZZQ?from=1")
    assert r.status_code == 200 and built == ["ZZZQ"] and "回全市场组合" in r.text


def test_momentum_filter(conn):
    spec = backtest.validate({"name": "vm", "entry": {"mode": "all", "conds": [
        {"type": "val_ge", "pct": 80}, {"type": "mcap_ge", "b": 10}, {"type": "above_ma", "n": 50}]},
        "exit": {"mode": "any", "conds": [{"type": "val_le", "pct": 5}]}})
    scan.check_spec(spec)
    with pytest.raises(ValueError, match="全部满足"):
        scan.check_spec({**spec, "entry": {**spec["entry"], "mode": "any"}})
    aid = scan.open_account(conn, {"id": "3", "spec": spec}, 10000, 0.0, 2, at(9), bench_px=500.0)
    from datetime import date, timedelta
    d0 = date(2026, 11, 11)
    days = [d0 - timedelta(days=i) for i in range(60, 0, -1)]
    hist = {"AAA": (days, [60.0] * 60),           # falling: today 40 < 50-day avg → no momentum
            "BBB": (days, [40.0] * 60)}           # rising: today 50 > avg → momentum
    asked = []

    def history(ts, bar):
        asked.extend(ts)
        return {t: hist[t] for t in ts if t in hist}
    conn.execute("INSERT INTO scan_fair VALUES ('AAA','2026-11-10',100,NULL,NULL,NULL,NULL),"
                 "('BBB','2026-11-10',100,NULL,NULL,NULL,NULL),('CCC','2026-11-10',100,NULL,NULL,NULL,NULL),"
                 "('DDD','2026-11-10',100,NULL,NULL,NULL,NULL)")
    a = scan.account(conn, aid)
    prices = {"AAA": (40.0, 5e9), "BBB": (50.0, 5e9), "CCC": (80.0, 5e9), "DDD": (45.0, 5e9)}
    res = scan.step(conn, a, "2026-11-11", prices, scan.fair_map(conn), 500.0, at(11), history=history)
    assert res["buys"] == ["BBB"]
    assert sorted(asked) == ["AAA", "BBB", "DDD"]                  # CCC fails the valuation filter → never fetched
    conn.execute("INSERT INTO scan_universe VALUES ('AAA','A','NYQ',40,5e9,'2026-11-11','x'),"
                 "('BBB','B','NYQ',50,5e9,'2026-11-11','x'),('DDD','D','NYQ',45,5e9,'2026-11-11','x')")
    m = {c["ticker"]: c["momo"] for c in scan.candidates(conn, spec, aid=aid)}
    assert m == {"AAA": 0, "BBB": 1, "DDD": -1}


def test_update_spec_redecides_pending(conn):
    from radar import paper
    paper.init(conn)
    aid = scan.open_account(conn, {"id": "3", "spec": SPEC}, 10000, 0.0, 2, at(9), bench_px=500.0)
    conn.execute("INSERT INTO scan_fair VALUES ('AAA','2026-11-10',100,NULL,NULL,NULL,NULL),"
                 "('BBB','2026-11-10',100,NULL,NULL,NULL,NULL)")
    conn.execute("INSERT INTO scan_universe VALUES ('AAA','A','NYQ',40,5e9,'2026-11-11','x'),"
                 "('BBB','B','NYQ',50,5e9,'2026-11-11','x')")
    prices = {"AAA": (40.0, 5e9), "BBB": (50.0, 5e9)}
    scan.step(conn, scan.account(conn, aid), "2026-11-11", prices, scan.fair_map(conn), 500.0, at(11))
    assert {r["ticker"] for r in conn.execute("SELECT * FROM port_pending")} == {"AAA", "BBB"}
    from datetime import date, timedelta
    days = [date(2026, 11, 11) - timedelta(days=i) for i in range(60, 0, -1)]
    hist = {"AAA": (days, [60.0] * 60), "BBB": (days, [40.0] * 60)}
    new = {**SPEC, "entry": {"mode": "all", "conds": SPEC["entry"]["conds"] + [{"type": "above_ma", "n": 50}]}}
    assert scan.update_spec(conn, aid, new, history=lambda ts, b: {t: hist[t] for t in ts}) == ["BBB"]
    assert {r["ticker"] for r in conn.execute("SELECT * FROM port_pending")} == {"BBB"}
    assert "above_ma" in scan.account(conn, aid)["spec"]
