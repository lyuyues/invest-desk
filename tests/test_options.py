from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from radar import db, options, trade

NY = ZoneInfo("America/New_York")
OPEN = datetime(2026, 11, 10, 11, 0, tzinfo=NY)        # Tuesday, market open
EXP = date(2026, 11, 20)
CALL = "AAA261120C00110000"
PUT = "AAA261120P00090000"
SPOT = {"AAA": 100.0, "SPY": 500.0}
OPT = {CALL: {"bid": 1.9, "ask": 2.1, "last": 2.0}, PUT: {"bid": 1.4, "ask": 1.6, "last": 1.5}}


def spot(t):
    return SPOT[t]


def oq(c):
    r = OPT[c]
    return {"contract": c, "iv": 0.3, "oi": 0, "vol": 0, "mark": (r["bid"] + r["ask"]) / 2, **r}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    trade.init(c)
    return c


def acct(conn, cash=50000):
    return trade.open_account(conn, "t", cash, 0.0, OPEN, quote=spot)


def o(**kw):
    return {"contract": CALL, "side": "buy", "kind": "market", "qty": "2", "px": "", "reason": "test", **kw}


def stock(conn, aid, side, qty):
    trade.place(conn, aid, {"ticker": "AAA", "side": side, "kind": "market", "qty": str(qty), "px": "", "reason": "t",
                            "plan_stop": "", "plan_target": ""}, OPEN, quote=spot)


def bars_at(close):
    return lambda t, since: [{"date": EXP, "open": close, "high": close, "low": close, "close": close}]


def test_parse_and_bs():
    i = options.parse(CALL)
    assert i == {"contract": CALL, "underlying": "AAA", "right": "C", "strike": 110.0, "expiry": EXP}
    c, dc = options.bs(100, 100, 0.5, 0.2, "C")
    p, dp = options.bs(100, 100, 0.5, 0.2, "P")
    assert c - p == pytest.approx(100 - 100 * 2.718281828 ** (-0.04 * 0.5), abs=1e-6)   # put-call parity
    assert 0.5 < dc < 0.6 and dp == pytest.approx(dc - 1)


def test_long_call_round_trip(conn):
    aid = acct(conn)
    options.place(conn, aid, o(), OPEN, q=oq)                       # buy 2 at the ask 2.10
    assert trade.account(conn, aid)["cash"] == pytest.approx(50000 - 420 - 1.30)
    OPT[CALL].update(bid=3.0, ask=3.2)
    options.place(conn, aid, o(side="sell"), OPEN, q=oq)             # sell to close at the bid 3.00
    pos, trips = options.book(conn, aid)
    assert not pos and trips[0]["pnl"] == pytest.approx(180 - 2.6)
    OPT[CALL].update(bid=1.9, ask=2.1)
    with pytest.raises(trade.OrderError):
        options.place(conn, aid, o(reason=""), OPEN, q=oq)


def test_naked_call_rejected_covered_ok_and_shares_locked(conn):
    aid = acct(conn)
    oid = options.place(conn, aid, o(side="sell", qty="1"), OPEN, q=oq)
    assert conn.execute("SELECT status FROM opt_orders WHERE id=?", (oid,)).fetchone()[0] == "rejected"
    stock(conn, aid, "buy", 100)
    options.place(conn, aid, o(side="sell", qty="1"), OPEN, q=oq)
    pos, _ = options.book(conn, aid)
    assert pos[CALL]["qty"] == -1
    stock(conn, aid, "sell", 50)                                      # locked by the short call
    r = conn.execute("SELECT status, msg FROM trade_orders ORDER BY id DESC").fetchone()
    assert r["status"] == "rejected" and "锁定" in r["msg"]
    oid = options.place(conn, aid, o(side="buy", qty="3"), OPEN, q=oq)   # can't flip short → long in one order
    assert conn.execute("SELECT status FROM opt_orders WHERE id=?", (oid,)).fetchone()[0] == "rejected"


def test_cash_secured_put_reserves_cash_and_assignment(conn):
    aid = acct(conn, cash=10000)
    oid = options.place(conn, aid, o(contract=PUT, side="sell", qty="2"), OPEN, q=oq)   # needs 18,000
    assert conn.execute("SELECT status FROM opt_orders WHERE id=?", (oid,)).fetchone()[0] == "rejected"
    options.place(conn, aid, o(contract=PUT, side="sell", qty="1"), OPEN, q=oq)          # +140 − 0.65
    assert options.constraints(conn, aid)[0] == 9000
    stock(conn, aid, "buy", 20)                                       # 2000 > 10139 − 9000 free cash
    assert conn.execute("SELECT status FROM trade_orders ORDER BY id DESC").fetchone()[0] == "rejected"
    cash0 = trade.account(conn, aid)["cash"]
    assert options.settle(conn, aid, EXP, bars=bars_at(85.0)) == 1   # assigned: buy 100 at 90
    assert trade.account(conn, aid)["cash"] == pytest.approx(cash0 - 9000)
    stocks, _ = trade.book(conn, aid)
    assert stocks["AAA"]["qty"] == 100
    pos, trips = options.book(conn, aid)
    assert not pos and trips[0]["how"] == "assign" and trips[0]["pnl"] == pytest.approx(140 - 500 - 0.65)
    assert options.settle(conn, aid, EXP, bars=bars_at(85.0)) == 0   # idempotent


def test_expiry_worthless_exercise_and_cash_settle(conn):
    aid = acct(conn)
    options.place(conn, aid, o(qty="1"), OPEN, q=oq)                 # long 110 call
    options.place(conn, aid, o(contract=PUT, qty="1"), OPEN, q=oq)   # long 90 put, no shares → cash settle
    cash0 = trade.account(conn, aid)["cash"]
    options.settle(conn, aid, EXP, bars=bars_at(120.0))
    _, trips = options.book(conn, aid)
    how = {t["contract"]: t["how"] for t in trips}
    assert how == {CALL: "exercise", PUT: "expire"}
    assert trade.account(conn, aid)["cash"] == pytest.approx(cash0 - 11000)   # paid the strike for 100 shares
    stocks, _ = trade.book(conn, aid)
    assert stocks["AAA"]["qty"] == 100 and stocks["AAA"]["avg"] == 120

    aid2 = acct(conn)
    options.place(conn, aid2, o(contract=PUT, qty="1"), OPEN, q=oq)
    c0 = trade.account(conn, aid2)["cash"]
    options.settle(conn, aid2, EXP, bars=bars_at(80.0))
    assert options.book(conn, aid2)[1][0]["how"] == "cash"
    assert trade.account(conn, aid2)["cash"] == pytest.approx(c0 + 1000)


def test_limit_rests_then_fills(conn):
    aid = acct(conn)
    oid = options.place(conn, aid, o(kind="limit", px="1.50"), OPEN, q=oq)
    assert conn.execute("SELECT status FROM opt_orders WHERE id=?", (oid,)).fetchone()[0] == "open"
    OPT[CALL].update(bid=1.3, ask=1.45)
    assert options.sweep_live(conn, aid, OPEN, q=oq) == 1
    r = conn.execute("SELECT status, fill_px FROM opt_orders WHERE id=?", (oid,)).fetchone()
    assert r["status"] == "filled" and r["fill_px"] == 1.45
    OPT[CALL].update(bid=1.9, ask=2.1)
    oid = options.place(conn, aid, o(side="sell", kind="limit", px="2.50"), OPEN, q=oq)
    bar = lambda t, since: [{"date": date(2026, 11, 11), "open": 2.2, "high": 2.6, "low": 2.1, "close": 2.4}]  # noqa: E731
    assert options.run_job(conn, datetime(2026, 11, 11, 17, 0, tzinfo=NY), bars=bar).startswith("filled=1")
    assert conn.execute("SELECT fill_px FROM opt_orders WHERE id=?", (oid,)).fetchone()[0] == 2.5


def test_value_and_equity_snapshot(conn):
    aid = acct(conn)
    options.place(conn, aid, o(qty="1"), OPEN, q=oq)
    total, rows, errs = options.value(conn, aid, OPEN, q=oq, spot=spot)
    assert total == pytest.approx(200) and not errs and rows[0]["delta"] < 0.5
    import radar.options as m
    orig = m.quote
    m.quote = oq
    try:
        trade.run_job(conn, datetime(2026, 11, 10, 17, 0, tzinfo=NY), bars=lambda t, s: [], quote=spot)
    finally:
        m.quote = orig
    eq = conn.execute("SELECT equity FROM trade_equity").fetchone()[0]
    assert eq == pytest.approx(50000 - 210 - 0.65 + 200)


def fake_chain(u, e, *a, **k):
    rows = {"calls": [], "puts": []}
    for k_ in (90.0, 100.0, 110.0):
        for right, key in (("C", "calls"), ("P", "puts")):
            c = f"AAA261120{right}{int(k_ * 1000):08d}"
            r = OPT.get(c, {"bid": 1.0, "ask": 1.2, "last": 1.1})
            rows[key].append({"contract": c, "strike": k_, "right": right, "iv": 0.3, "oi": 5, "vol": 1,
                              "mark": (r["bid"] + r["ask"]) / 2, **r})
    return {"spot": 100.0, **rows, "by": {r["contract"]: r for r in rows["calls"] + rows["puts"]}}


def test_option_pages(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "w.db"))
    monkeypatch.setattr(trade, "live_quote", lambda t, *a, **k: spot(t))
    monkeypatch.setattr(options, "expiries", lambda u, *a, **k: ["2026-11-20", "2026-12-18"])
    monkeypatch.setattr(options, "chain", fake_chain)
    monkeypatch.setattr(options, "years_left", lambda e, now: 0.03)
    from web.app import app
    client = TestClient(app)
    loc = client.post("/trade", data={"name": "o", "capital": "50000"}, follow_redirects=False).headers["location"]
    r = client.get(loc + "/opt?u=AAA")
    assert r.status_code == 200 and "期权链 AAA" in r.text and "行权价" in r.text
    r = client.get(loc + f"/opt?u=AAA&c={CALL}&side=buy")
    assert "下单：AAA 2026-11-20 110 Call" in r.text and "payoff" in r.text
    r = client.post(loc + "/opt/order", data={**o(qty="1"), "u": "AAA"}, follow_redirects=True)
    assert "已成交" in r.text
    r = client.get(loc)
    assert "期权持仓" in r.text and "AAA 2026-11-20 110 Call" in r.text
    r = client.post(loc + "/opt/order", data={**o(contract=PUT, side="sell", qty="9"), "u": "AAA"}, follow_redirects=True)
    assert "现金不够" in r.text
