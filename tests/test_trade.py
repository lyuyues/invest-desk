from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from radar import db, trade

NY = ZoneInfo("America/New_York")
OPEN = datetime(2026, 11, 10, 11, 0, tzinfo=NY)       # Tuesday, market open
PRICES = {"AAA": 100.0, "SPY": 500.0}


def q(t):
    if t not in PRICES:
        raise ValueError("no quote")
    return PRICES[t]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    trade.init(c)
    return c


def order(**kw):
    base = {"ticker": "AAA", "side": "buy", "kind": "market", "qty": "10", "px": "", "reason": "test",
            "plan_stop": "", "plan_target": ""}
    return {**base, **kw}


def test_market_round_trip_and_stats(conn):
    aid = trade.open_account(conn, "t", 10000, 0.001, OPEN, quote=q)
    trade.place(conn, aid, order(qty="10", plan_stop="90"), OPEN, quote=q)
    PRICES["AAA"] = 110.0
    trade.place(conn, aid, order(qty="10"), OPEN, quote=q)       # add at 110 → avg 105
    pos, trips = trade.book(conn, aid)
    assert pos["AAA"]["qty"] == 20 and pos["AAA"]["avg"] == pytest.approx(105)
    PRICES["AAA"] = 120.0
    trade.place(conn, aid, order(side="sell", qty="20"), OPEN, quote=q)
    pos, trips = trade.book(conn, aid)
    assert not pos and len(trips) == 1
    fees = (1000 + 1100 + 2400) * 0.001
    assert trips[0]["pnl"] == pytest.approx(15 * 20 - fees)
    assert trips[0]["plan_stop"] == 90
    a = trade.account(conn, aid)
    assert a["cash"] == pytest.approx(10000 + 300 - fees)
    st = trade.stats(trips)
    assert st["n"] == 1 and st["win"] == 1 and st["planned"] == 1
    PRICES["AAA"] = 100.0


def test_rejections(conn):
    aid = trade.open_account(conn, "t", 1000, 0.0, OPEN, quote=q)
    with pytest.raises(trade.OrderError):
        trade.place(conn, aid, order(reason=" "), OPEN, quote=q)              # reason required
    with pytest.raises(trade.OrderError):
        trade.place(conn, aid, order(side="buy", kind="stop", px="90"), OPEN, quote=q)
    with pytest.raises(trade.OrderError):
        trade.place(conn, aid, order(qty="1.5"), OPEN, quote=q)
    oid = trade.place(conn, aid, order(qty="11"), OPEN, quote=q)             # 1100 > 1000 cash
    assert conn.execute("SELECT status FROM trade_orders WHERE id=?", (oid,)).fetchone()[0] == "rejected"
    with pytest.raises(trade.OrderError):
        trade.place(conn, aid, order(side="sell", qty="1"), OPEN, quote=q)   # nothing held


def test_limit_and_stop_matching(conn):
    aid = trade.open_account(conn, "t", 10000, 0.0, OPEN, quote=q)
    lim = trade.place(conn, aid, order(kind="limit", px="95", qty="10"), OPEN, quote=q)    # rests (100 > 95)
    assert trade.orders(conn, aid, "open")[0]["id"] == lim
    bars = [  # order placed Tue 11:00 → Tue's bar is skipped (it started before the order)
        {"date": date(2026, 11, 10), "open": 100, "high": 101, "low": 90, "close": 99},
        {"date": date(2026, 11, 11), "open": 97, "high": 98, "low": 94, "close": 96},     # fills at 95
        {"date": date(2026, 11, 12), "open": 92, "high": 93, "low": 80, "close": 85},
    ]
    after = datetime(2026, 11, 11, 17, 0, tzinfo=NY)
    trade.run_job(conn, after, bars=lambda t, s: bars, quote=q)
    o = conn.execute("SELECT * FROM trade_orders WHERE id=?", (lim,)).fetchone()
    assert o["status"] == "filled" and o["fill_px"] == 95
    stop = trade.place(conn, aid, order(side="sell", kind="stop", px="93", qty="10"), after, quote=q)
    trade.run_job(conn, datetime(2026, 11, 12, 17, 0, tzinfo=NY), bars=lambda t, s: bars, quote=q)
    o = conn.execute("SELECT * FROM trade_orders WHERE id=?", (stop,)).fetchone()
    assert o["status"] == "filled" and o["fill_px"] == 92      # gapped below the stop → open price
    eq = conn.execute("SELECT * FROM trade_equity WHERE account_id=? ORDER BY date", (aid,)).fetchall()
    assert [r["date"] for r in eq] == ["2026-11-11", "2026-11-12"]
    assert eq[-1]["equity"] == pytest.approx(10000 - 30)


def test_match_bar_rules():
    bar = {"open": 100, "high": 110, "low": 90, "close": 105}
    assert trade.match_bar({"kind": "limit", "side": "sell", "px": 108}, bar) == 108
    assert trade.match_bar({"kind": "limit", "side": "sell", "px": 95}, bar) == 100
    assert trade.match_bar({"kind": "limit", "side": "buy", "px": 85}, bar) is None
    assert trade.match_bar({"kind": "stop", "side": "sell", "px": 95}, bar) == 95


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "watchlist.yaml").write_text("holdings: [MSFT]\nwatchlist: []\n")
    (tmp_path / "rules.yaml").write_text("rules: []\n")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "w.db"))
    monkeypatch.setattr(trade, "live_quote", lambda t, *a, **k: q(t))
    from web.app import app
    return TestClient(app)


def test_trade_pages(client):
    r = client.post("/trade", data={"name": "练习", "capital": "50000", "cost": "5"}, follow_redirects=False)
    loc = r.headers["location"]
    assert client.get("/trade", follow_redirects=False).headers["location"] == loc   # single account → straight in
    r = client.post(loc + "/order", data=order(qty="10", plan_stop="95", reason="突破"), follow_redirects=True)
    assert "已成交" in r.text and "AAA" in r.text and "突破" in r.text
    r = client.post(loc + "/order", data=order(reason=""), follow_redirects=True)
    assert "为什么" in r.text
    assert client.get("/trade/quote?t=AAA").json()["price"] == 100
    assert client.get("/trade/quote?t=ZZZ").status_code == 404
    r = client.post(loc + "/order", data=order(side="sell", qty="10", reason="到目标"), follow_redirects=True)
    assert "复盘" in r.text and "已完成交易" in r.text


def test_friendly_errors(conn):
    aid = trade.open_account(conn, "t", 10000, 0.0, OPEN, quote=q)
    with pytest.raises(trade.OrderError, match="没有 AAA 的持仓"):
        trade.place(conn, aid, order(side="sell"), OPEN, quote=q)
    with pytest.raises(trade.OrderError, match="no quote"):
        trade.place(conn, aid, order(ticker="ZZZ"), OPEN, quote=q)

    class Bad:
        def __init__(self, t):
            self.fast_info = {}
    with pytest.raises(ValueError, match="代码写对了吗"):
        trade.live_quote("MFST", ticker_factory=Bad)


def test_transfer_in(conn):
    aid = trade.open_account(conn, "t", 10000, 0.0, OPEN, quote=q)   # SPY 500 at open
    PRICES["SPY"] = 550.0
    trade.transfer_in(conn, aid, "AAA", 100, 80.0, OPEN, quote=q)     # AAA at 100, real cost 80
    a = trade.account(conn, aid)
    assert a["capital"] == 20000 and a["cash"] == 10000
    assert a["capital"] * 550 / a["bench_px"] == pytest.approx(10000 * 550 / 500 + 10000)
    pos, _ = trade.book(conn, aid)
    assert pos["AAA"]["qty"] == 100 and pos["AAA"]["avg"] == 80
    with pytest.raises(trade.OrderError):
        trade.transfer_in(conn, aid, "AAA", 1.5, None, OPEN, quote=q)
    PRICES["SPY"] = 500.0


def test_fix_transfer(conn):
    aid = trade.open_account(conn, "t", 10000, 0.0, OPEN, quote=q)
    oid = trade.transfer_in(conn, aid, "AAA", 500000, 80.0, OPEN, quote=q)     # typo
    trade.place(conn, aid, order(side="sell", kind="limit", px="200", qty="200"), OPEN, quote=q)
    trade.fix_transfer(conn, aid, oid, 1500, OPEN, quote=q)
    a = trade.account(conn, aid)
    assert a["capital"] == pytest.approx(10000 + 1500 * 100)
    assert a["capital"] * 500 / a["bench_px"] == pytest.approx(a["capital"])
    assert trade.book(conn, aid)[0]["AAA"]["qty"] == 1500
    trade.place(conn, aid, order(side="sell", qty="1000"), OPEN, quote=q)
    with pytest.raises(trade.OrderError):
        trade.fix_transfer(conn, aid, oid, 100, OPEN, quote=q)                  # already sold 1000
    assert trade.book(conn, aid)[0]["AAA"]["qty"] == 500                        # rolled back


def test_set_plan(conn):
    aid = trade.open_account(conn, "t", 10000, 0.0, OPEN, quote=q)
    trade.transfer_in(conn, aid, "AAA", 100, 80.0, OPEN, quote=q)
    trade.set_plan(conn, aid, "AAA", 90.0, 130.0)
    p = trade.book(conn, aid)[0]["AAA"]["orders"][0]
    assert (p["plan_stop"], p["plan_target"]) == (90.0, 130.0)
    with pytest.raises(trade.OrderError):
        trade.set_plan(conn, aid, "AAA", 140.0, 130.0)
    with pytest.raises(trade.OrderError):
        trade.set_plan(conn, aid, "ZZZ", 1.0, None)
