import json
import random
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from radar import backtest, db, paper
from radar.backtest import Pos, Series

D0 = date(2020, 1, 1)


def bars(closes):
    return [D0 + timedelta(days=i) for i in range(len(closes))], list(closes)


def spec(entry, exit_, mode_in="all", mode_out="any"):
    return backtest.validate({"name": "t", "entry": {"mode": mode_in, "conds": entry},
                              "exit": {"mode": mode_out, "conds": exit_}})


def test_rolling_windows_match_bruteforce():
    rnd = random.Random(1)
    c = [100 + rnd.gauss(0, 5) for _ in range(300)]
    s = Series(*bars(c))
    for n in (1, 5, 20):
        for t in range(300):
            assert s.prev_max(n, t) == (max(c[t - n:t]) if t >= n else None)
            assert s.prev_min(n, t) == (min(c[t - n:t]) if t >= n else None)
            assert s.max_incl(n, t) == (max(c[t - n + 1:t + 1]) if t + 1 >= n else None)
        assert s.sma(n, 50) == pytest.approx(sum(c[51 - n:51]) / n)


def test_signal_executes_next_close_no_lookahead():
    c = [10, 10, 10, 12, 15, 9, 9]
    sp = spec([{"type": "new_high", "n": 2}], [{"type": "new_low", "n": 2}])
    eq, trades, in_pos = backtest.simulate(sp, Series(*bars(c)), cost=0.0)
    # breakout seen at t=3 (12 > 10) → bought at t=4 close (15); breakdown at t=5 (9) → sold at t=6 close (9)
    assert trades[0].entry == D0 + timedelta(days=4) and trades[0].entry_px == 15
    assert trades[0].exit == D0 + timedelta(days=6) and trades[0].exit_px == 9
    assert trades[0].ret == pytest.approx(9 / 15 - 1)
    assert eq[-1] == pytest.approx(9 / 15)
    assert in_pos == [False] * 4 + [True, True, False]


def test_take_profit_fill():
    c = [10, 11, 12, 13, 14, 15, 16]
    sp = spec([{"type": "ret_up", "n": 1, "pct": 5}], [{"type": "take_profit", "pct": 10}])
    _, trades, _ = backtest.simulate(sp, Series(*bars(c)), cost=0.01)
    t = trades[0]
    # ret_up seen at t=1 (11/10) → bought t=2 @12; 13.2 crossed at t=4 (14) → sold t=5 @15
    assert (t.entry_px, t.exit_px) == (12, 15)
    assert t.ret == pytest.approx(15 / 12 * 0.99 ** 2 - 1)


def test_validate_rejects_bad_specs():
    with pytest.raises(ValueError):
        spec([], [])
    with pytest.raises(ValueError):
        spec([{"type": "stop_loss", "pct": 10}], [])     # position-only condition on entry
    with pytest.raises(ValueError):
        spec([{"type": "ma_up", "fast": 200, "slow": 50}], [])
    with pytest.raises(ValueError):
        spec([{"type": "nope"}], [])
    for k, p in backtest.PRESETS.items():
        assert backtest.validate(p)["entry"]["conds"], k


def test_run_reports_hold_and_split():
    rnd = random.Random(2)
    c = [100.0]
    for _ in range(800):
        c.append(c[-1] * (1 + rnd.gauss(0.0004, 0.015)))
    d, c = bars(c)
    res = backtest.run(d, c, [("p:trend", backtest.validate(backtest.PRESETS["p:trend"]))], cost=0.001)
    hold, trend = res["results"]
    assert hold.m["total"] == pytest.approx(c[-1] / c[0] * 0.999 - 1)
    assert trend.m["trades"] >= 1 and trend.cost_drag >= 0
    assert trend.sens and all("out" in v for v in trend.sens)
    assert res["cut"] == int(len(d) * 0.7)


# ---------- paper ----------

def _ny(d: date, hh: int) -> datetime:
    from zoneinfo import ZoneInfo
    return datetime(d.year, d.month, d.day, hh, 0, tzinfo=ZoneInfo("America/New_York"))


def test_paper_steps_only_completed_bars(tmp_path):
    conn = db.connect(tmp_path / "p.db")
    sp = spec([{"type": "new_high", "n": 2}], [{"type": "new_low", "n": 2}])
    st = {"id": "p:x", "spec": sp}
    c = [10, 10, 10, 12, 15, 9, 9]
    d, _ = bars(c)
    aid = paper.open_account(conn, st, "XYZ", 1000, 0.0, _ny(d[1], 20))   # after close on d1 → starts d2
    a = conn.execute("SELECT * FROM paper_accounts WHERE id=?", (aid,)).fetchone()
    assert a["start"] == d[2].isoformat()

    # Mid-session on d4: bars d2, d3 final; d4 is partial and must be ignored.
    fires = paper.step_account(conn, a, d[:5], c[:5], _ny(d[4], 11))
    a = conn.execute("SELECT * FROM paper_accounts WHERE id=?", (aid,)).fetchone()
    assert a["last_date"] == d[3].isoformat() and a["pending"] == "buy" and len(fires) == 1
    assert a["hold_px"] == 10

    # Live preview with a provisional d4 bar.
    pv = paper.preview(sp, a, d[:5], c[:5], _ny(d[4], 11))
    assert pv["holding"] and pv["price"] == 15

    # After the close on d6: buy at d4 (15), sell signal d5 (9), sold d6 (9). Re-running is idempotent.
    for _ in range(2):
        a = conn.execute("SELECT * FROM paper_accounts WHERE id=?", (aid,)).fetchone()
        paper.step_account(conn, a, d, c, _ny(d[6], 17))
    fills = conn.execute("SELECT side, price, date FROM paper_fills WHERE account_id=? ORDER BY id", (aid,)).fetchall()
    assert [(f["side"], f["price"]) for f in fills] == [("buy", 15), ("sell", 9)]
    a = conn.execute("SELECT * FROM paper_accounts WHERE id=?", (aid,)).fetchone()
    assert a["shares"] == 0 and a["cash"] == pytest.approx(1000 * 9 / 15)
    eq = conn.execute("SELECT * FROM paper_equity WHERE account_id=? ORDER BY date", (aid,)).fetchall()
    assert len(eq) == 5 and eq[-1]["hold"] == pytest.approx(1000 * 9 / 10)


def test_paper_spec_is_frozen(tmp_path):
    conn = db.connect(tmp_path / "p.db")
    now = datetime(2026, 11, 11, 15, tzinfo=timezone.utc)
    sid = paper.save_strategy(conn, {"name": "mine", "entry": {"mode": "all", "conds": [{"type": "new_high", "n": 20}]},
                                     "exit": {"mode": "any", "conds": []}}, now)
    aid = paper.open_account(conn, paper.get_strategy(conn, sid), "SPY", 1000, 0.001, now)
    paper.save_strategy(conn, {"name": "mine2", "entry": {"mode": "all", "conds": [{"type": "new_high", "n": 99}]},
                               "exit": {"mode": "any", "conds": []}}, now, sid)
    a = conn.execute("SELECT spec FROM paper_accounts WHERE id=?", (aid,)).fetchone()
    assert json.loads(a["spec"])["entry"]["conds"][0]["n"] == 20


def test_paper_due():
    from web.scheduler import paper_due
    t = datetime(2026, 11, 11, 22, 0, tzinfo=timezone.utc)       # 17:00 New York
    assert paper_due(t, None)
    assert not paper_due(t, "2026-11-11T21:50:00+00:00")
    assert paper_due(t, "2026-11-10T21:50:00+00:00")
    assert not paper_due(datetime(2026, 11, 11, 20, 0, tzinfo=timezone.utc), None)  # 15:00 NY


# ---------- web ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "watchlist.yaml").write_text("holdings: [MSFT]\nwatchlist: []\n")
    (tmp_path / "rules.yaml").write_text("rules: []\n")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "w.db"))
    rnd = random.Random(3)
    c = [100.0]
    for _ in range(600):
        c.append(c[-1] * (1 + rnd.gauss(0.0003, 0.012)))
    d = [date(2024, 1, 1) + timedelta(days=i) for i in range(len(c))]
    monkeypatch.setattr(backtest, "fetch_history", lambda *a, **k: (d, c))
    from web.app import app
    return TestClient(app)


def test_sim_page(client):
    r = client.get("/sim?ticker=spy&s=p:trend&s=p:chase&s=p:dip")
    assert r.status_code == 200
    assert "趋势跟随" in r.text and "追涨杀跌" in r.text and "<polyline" in r.text and "样本外" in r.text


def test_strategy_editor_roundtrip(client):
    assert client.get("/sim/edit?src=p:chase").status_code == 200
    bad = client.post("/sim/strategy", json={"spec": {"entry": {"conds": []}}})
    assert bad.status_code == 400 and "买入条件" in bad.json()["error"]
    ok = client.post("/sim/strategy", json={"spec": {"name": "我的", "entry": {"mode": "all", "conds": [
        {"type": "above_ma", "n": 50}]}, "exit": {"mode": "any", "conds": [{"type": "trailing", "pct": 12}]}}})
    sid = ok.json()["id"]
    r = client.get(f"/sim?s={sid}")
    assert "我的" in r.text and "移动止盈" in r.text
    assert client.get(f"/sim/edit?id={sid}").status_code == 200


def test_paper_pages(client):
    r = client.post("/paper", data={"strategy": "p:ma200", "ticker": "spy", "capital": "5000", "cost": "5"},
                    follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    page = client.get(loc)
    assert page.status_code == 200 and "SPY" in page.text and "200 日均线" in page.text
    assert "SPY" in client.get("/paper").text
    assert client.post(loc + "/stop", follow_redirects=False).status_code == 303
    assert "已停止" in client.get(loc).text


def test_valuation_conditions(tmp_path, monkeypatch):
    sp = backtest.validate(backtest.PRESETS["p:value"])
    assert backtest.uses_valuation(sp) and not backtest.uses_valuation(backtest.PRESETS["p:trend"])
    s = Series([date(2026, 1, 1), date(2026, 1, 2)], [40.0, 96.0], fair=100.0)
    sig, why = backtest.decide(sp, s, 0, Pos())                          # upside +150% → buy
    assert sig == "buy" and "合理价 100.00" in why
    assert backtest.decide(sp, s, 1, Pos(True, 40, 96, 1))[0] == "sell"   # upside +4% → sell
    assert backtest.decide(sp, Series(s.dates, s.c), 0, Pos())[0] is None  # no fair value → never fires
    with pytest.raises(ValueError):
        backtest.validate({"entry": {"conds": [{"type": "val_ge", "pct": 5000}]}})

    conn = db.connect(tmp_path / "v.db")
    from radar import valuation
    monkeypatch.setattr(valuation, "fair_value", lambda c, t: {"AAA": 100.0}.get(t))
    st = {"id": "p:value", "spec": sp}
    with pytest.raises(ValueError, match="没有 DCF"):
        paper.open_account(conn, st, "ZZZ", 1000, 0.0, _ny(date(2026, 1, 1), 20))
    d = [date(2026, 1, 5) + timedelta(days=i) for i in range(4)]
    c = [45.0, 48.0, 97.0, 99.0]
    aid = paper.open_account(conn, st, "AAA", 1000, 0.0, _ny(date(2026, 1, 2), 20))
    a = conn.execute("SELECT * FROM paper_accounts WHERE id=?", (aid,)).fetchone()
    paper.step_account(conn, a, d, c, _ny(d[-1], 17))
    fills = conn.execute("SELECT side, price FROM paper_fills ORDER BY id").fetchall()
    assert [(f["side"], f["price"]) for f in fills] == [("buy", 48.0), ("sell", 99.0)]


def test_sim_refuses_valuation_backtest(client):
    r = client.get("/sim?ticker=spy&s=p:value&s=p:trend")
    assert "没法回测" in r.text and "趋势跟随" in r.text
    r = client.post("/paper", data={"strategy": "p:value", "ticker": "spy", "capital": "5000"}, follow_redirects=True)
    assert "没有 DCF 估值" in r.text


def test_market_cap_condition():
    sp = backtest.validate({"entry": {"mode": "all", "conds": [{"type": "val_ge", "pct": 80}, {"type": "mcap_ge", "b": 10}]},
                            "exit": {"mode": "any", "conds": [{"type": "val_le", "pct": 5}]}})
    assert backtest.uses_valuation(sp)
    s = Series([date(2026, 1, 1)], [50.0], fair=100.0, mcap=5e9)
    assert backtest.decide(sp, s, 0, Pos())[0] == "buy"
    s.mcap = 5e8
    assert backtest.decide(sp, s, 0, Pos())[0] is None
    with pytest.raises(ValueError):
        backtest.validate({"entry": {"conds": [{"type": "mcap_ge", "b": 0}]}})
