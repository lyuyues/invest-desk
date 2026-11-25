from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from radar import db
from radar.models import Fire, Quote
from web.views import sparkline_points

NOW = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "watchlist.yaml").write_text("holdings: [COST]\nwatchlist: [QCOM, META]\n")
    (tmp_path / "rules.yaml").write_text("rules: []\n")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "w.db"))
    conn = db.connect(tmp_path / "w.db")
    db.insert_quote(conn, Quote("COST", NOW - timedelta(hours=1), 400, 395, 31.0, 470, 350))
    db.insert_quote(conn, Quote("COST", NOW, 380, 400, 30.0, 470, 350))
    db.insert_quote(conn, Quote("QCOM", NOW, 180, 175))
    db.record_alert(conn, Fire("hn", "COST", "notice", "COST ▼5.0%", "body"), NOW, pushed=True)
    conn.commit()
    conn.close()
    from web.app import app

    return TestClient(app)


def test_home(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "COST" in r.text and "QCOM" in r.text
    assert "No data yet" in r.text  # META has no quotes
    assert "COST ▼5.0%" in r.text
    assert "-5.0%" in r.text


def test_ticker_page(client):
    r = client.get("/t/cost")
    assert r.status_code == 200
    assert "<polyline" in r.text
    assert "380.00" in r.text


def test_unknown_ticker(client):
    r = client.get("/t/ZZZZ")
    assert r.status_code == 200 and "None yet" in r.text


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_sparkline():
    assert sparkline_points([1]) == ""
    pts = sparkline_points([1, 2, 3], width=100, height=10).split()
    assert pts[0] == "0.0,10.0" and pts[-1] == "100.0,0.0"


@pytest.fixture
def rich_client(tmp_path, monkeypatch):
    from radar.collectors.macro import Market, store_markets
    from radar.discovery import store_candidates

    (tmp_path / "watchlist.yaml").write_text("holdings: [COST]\nwatchlist: []\n")
    (tmp_path / "rules.yaml").write_text("rules: []\n")
    (tmp_path / "macro.yaml").write_text("fred: {DGS10: 10Y Treasury, UNRATE: Unemployment rate}\n")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "r.db"))
    conn = db.connect(tmp_path / "r.db")
    db.insert_quote(conn, Quote("COST", NOW, 380, 400, 30.0, 470, 350))
    conn.executemany("INSERT INTO macro_obs VALUES (?,?,?)",
                     [("DGS10", "2026-10-28", 4.6), ("DGS10", "2026-11-05", 4.84)])
    store_markets(conn, [Market("kalshi", "KXFED-26DEC-T3.50", "KXFED-26DEC", "Fed Dec", "Above 3.50%", "2099-12-09T18:55:00Z", 1.0),
                         Market("kalshi", "KXFED-26DEC-T3.75", "KXFED-26DEC", "Fed Dec", "Above 3.75%", "2099-12-09T18:55:00Z", 0.2),
                         Market("polymarket", "11", "fed-dec", "Fed decision in December?", "25 bps decrease", None, 0.82)], NOW)
    conn.execute("INSERT INTO filings VALUES ('a1','COST',1,'8-K','2026-11-05','5.02,9.01','https://sec/x')")
    conn.execute("INSERT INTO insider_tx VALUES ('a2',0,'COST','Doe Jane','CFO','2026-11-04','P',5000,250,1250000)")
    conn.execute("INSERT INTO earnings VALUES ('COST','2026-10-28',4.72,4.89,3.59)")
    store_candidates(conn, "2026-11-08", [{"ticker": "TRNS", "score": 2.2, "channels": ["screen", "smart_money"],
                                           "detail": {"screen": ["Transcat P/E 20"], "smart_money": ["2 insiders bought"]}}])
    conn.execute("INSERT INTO reports (created, title, markdown, ok) VALUES (?,?,?,1)",
                 ("2026-11-08T03:00:00+00:00", "Weekly report 2026-11-07", "# One-liner\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n<script>alert(1)</script>"))
    conn.commit()
    conn.close()
    from web.app import app

    return TestClient(app)


def test_macro_page(rich_client):
    r = rich_client.get("/macro")
    assert r.status_code == 200
    assert "10Y Treasury" in r.text and "4.84" in r.text
    assert "3.80%" in r.text  # 3.50 + (1.0 + 0.2) * 0.25
    assert "25 bps decrease" in r.text and "82%" in r.text


def test_discover_page(rich_client):
    r = rich_client.get("/discover")
    assert r.status_code == 200 and "TRNS" in r.text and "Smart money" in r.text and "2026-11-08" in r.text


def test_reports_pages_escape_html(rich_client):
    assert "Weekly report 2026-11-07" in rich_client.get("/reports").text
    r = rich_client.get("/reports/1")
    assert r.status_code == 200 and "<table>" in r.text and "<script>alert" not in r.text
    assert rich_client.get("/reports/999").status_code == 404


def test_ticker_events(rich_client):
    r = rich_client.get("/t/COST")
    assert "Doe Jane" in r.text and "$1.25M" in r.text and "Officer/director change" in r.text and "+3.6%" in r.text


def test_home_shows_fed(rich_client):
    r = rich_client.get("/")
    assert "Fed expectations" in r.text and "3.80%" in r.text


def test_tracked_seed_add_move_delete(client, tmp_path, monkeypatch):
    import web.app as appmod
    monkeypatch.setattr(appmod, "_fetch_new", lambda t: None)
    r = client.post("/tracked", data={"ticker": "asml", "tier": "watch"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    from radar.config import load_config
    conn = db.connect(tmp_path / "w.db")
    cfg = load_config()
    db.apply_tracked(conn, cfg)
    assert cfg.holdings == ["COST"] and set(cfg.watchlist) == {"QCOM", "META", "ASML"}

    client.post("/tracked", data={"ticker": "QCOM", "tier": "holding"})
    client.post("/tracked/cost/delete")
    cfg = load_config()
    db.apply_tracked(conn, cfg)
    assert cfg.holdings == ["QCOM"] and "COST" not in cfg.all_tickers

    # removing everything must not re-seed from YAML
    for t in cfg.all_tickers:
        client.post(f"/tracked/{t}/delete")
    cfg = load_config()
    db.apply_tracked(conn, cfg)
    assert cfg.all_tickers == []
    conn.close()


def test_tracked_rejects_bad_input(client, monkeypatch):
    import web.app as appmod
    monkeypatch.setattr(appmod, "_fetch_new", lambda t: None)  # no network in tests
    assert client.post("/tracked", data={"ticker": "<script>", "tier": "watch"}).status_code == 400
    assert client.post("/tracked", data={"ticker": "NFLX", "tier": "boss"}).status_code == 400
    r = client.post("/tracked", data={"ticker": "NFLX", "tier": "watch", "back": "//evil.com"},
                    follow_redirects=False)
    assert r.headers["location"] == "/"


def test_earnings_page(rich_client, tmp_path):
    conn = db.connect(tmp_path / "r.db")
    conn.execute("INSERT INTO earnings VALUES ('COST','2099-01-28',5.10,NULL,NULL)")
    conn.commit()
    conn.close()
    r = rich_client.get("/earnings")
    assert r.status_code == 200
    assert "2099-01-28" in r.text and "5.10" in r.text and "1/1" in r.text  # upcoming + beat record
    assert "2026-10-28" in r.text and "+3.6%" in r.text  # recent result


def test_earnings_board_reaction(tmp_path):
    from radar import views
    conn = db.connect(tmp_path / "e.db")
    for ts, p in [("2026-10-28T04:00:00+00:00", 100.0), ("2026-10-29T04:00:00+00:00", 110.0)]:
        conn.execute("INSERT INTO quotes (ticker, ts, price) VALUES ('X', ?, ?)", (ts, p))
    conn.execute("INSERT INTO quotes (ticker, ts, price) VALUES ('X', '2026-10-27T04:00:00+00:00', 95.0)")
    conn.execute("INSERT INTO earnings VALUES ('X','2026-10-28',1.0,1.2,20.0)")
    b = views.earnings_board(conn, ["X"], datetime(2026, 11, 9, tzinfo=timezone.utc))
    assert b["upcoming"] == []
    assert round(b["recent"][0]["move_pct"], 1) == round((110 / 95 - 1) * 100, 1)


def test_theses_seed_edit(rich_client, tmp_path):
    r = rich_client.get("/theses")
    assert r.status_code == 200 and "General principles" in r.text and "Default" in r.text
    r = rich_client.post("/theses/COST", data={"body": "My COST thesis"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/theses#th-COST"
    rich_client.get("/theses")  # re-seed must not overwrite
    assert "My COST thesis" in rich_client.get("/t/COST").text
    r = rich_client.post("/theses/COST", data={"body": "v2", "back": "/t/COST"}, follow_redirects=False)
    assert r.headers["location"] == "/t/COST"
    assert rich_client.post("/theses/bad%20t", data={"body": "x"}).status_code == 400
    from radar import theses
    conn = db.connect(tmp_path / "r.db")
    p = "\n".join(theses.for_prompt(conn, ["COST"]))
    assert "v2" in p and "General principles (default" in p


def test_earnings_notes_flow(rich_client, tmp_path):
    from radar import earnings_notes as en
    from radar.config import load_config
    out = "## One-liner\nAzure accelerating\n\n## Source\nx\n\nTAGS: Beat, Guidance raised | Importance: High"
    body, tags, lvl = en.parse(out)
    assert tags == ["Beat", "Guidance raised"] and lvl == "High" and "TAGS" not in body and "Azure" in body
    assert en.parse("no tags")[1:] == ([], None)
    conn = db.connect(tmp_path / "r.db")
    cfg = load_config()
    prompts = []
    done = en.run(conn, cfg, NOW, lambda p: prompts.append(p) or out)
    assert [(d["ticker"], d["date"], d["level"]) for d in done] == [("COST", "2026-10-28", "High")]
    assert "EPS: actual 4.89 vs est. 4.72" in prompts[0] and "## Investment thesis" in prompts[0]
    assert en.run(conn, cfg, NOW, lambda p: out) == []  # already done
    conn.close()
    r = rich_client.get("/earnings")
    assert "Importance High" in r.text and "Guidance raised" in r.text and "Azure accelerating" in r.text
    assert "Guidance raised" in rich_client.get("/t/COST").text


def _fake_builder():
    import pandas as pd
    from types import SimpleNamespace
    hist = pd.DataFrame({2024: [100.0, 20.0, 18.0], 2025: [110.0, 22.0, 20.0]},
                        index=["Revenue (M)", "Net Income (M)", "Free Cash Flow (M)"])
    revs = [120.0, 130.0]
    g = [120 / 110 - 1, 130 / 120 - 1]
    rest = [g[1] + (0.025 - g[1]) * i / 7 for i in range(8)]
    for x in rest:
        revs.append(revs[-1] * (1 + x))
    growth = g + rest
    proj = pd.DataFrame({2026 + i: [revs[i], revs[i] * 0.2, growth[i] * 100, "Analyst" if i < 2 else "Model"]
                         for i in range(10)}, index=["Revenue (M)", "Free Cash Flow (M)", "Growth Rate (%)", "Estimate Source"])
    from radar import valuation
    fair = valuation.project({"base_year": 2025, "base_rev": 110.0, "analyst": revs[:2], "g_start": rest[0], "terminal": 0.025,
                              "rr": 0.07, "fcf_margin": 0.2, "shares": 10.0, "path": growth, "use_path": True})["fair"]
    meta = {"Terminal Growth (%)": 2.5, "Required Return (%)": 7.0, "Shares Outstanding (M)": 10.0,
            "Fair Value / Share ": fair, "Growth Model": "Analyst + Taper 10-Year"}

    def build_dcf(t):
        if t == "SOXX":
            raise ValueError("Unable to find CIK for ticker SOXX")
        return hist, proj, meta
    return SimpleNamespace(build_dcf=build_dcf, build_quarterly_table=lambda t, num_quarters=10: hist), fair


def test_valuation_flow(rich_client, tmp_path):
    from radar import valuation
    b, fair = _fake_builder()
    conn = db.connect(tmp_path / "r.db")
    res = valuation.refresh(conn, ["COST", "SOXX"], NOW, builder=b, shares_of=lambda t: 10.5, profile_of=lambda t: {"beta": 1.2, "industry": "Software - Infrastructure"})
    assert res["COST"] == "ok" and "CIK" in res["SOXX"]
    v = valuation.load(conn, "COST")
    a, w = v["data"]["auto"], v["data"]["wacc"]
    # discount rate = the company's WACC (no debt in the fixture → cost of equity = rf + 1.2 × ERP)
    assert a["engine_rr"] == pytest.approx(0.07) and a["rr"] == pytest.approx(round(w["wacc"], 4))
    assert w["wacc"] == pytest.approx(w["rf"] + (0.67 * 1.2 + 0.33) * 0.05) and a["net_debt"] == 0 and a["fcff_addback"] == 0
    # editable model with the engine's rate (taper, not stored path) reproduces the engine
    assert valuation.project({**valuation.merged(a, None), "rr": 0.07})["fair"] == pytest.approx(fair)
    assert valuation.project({**a, "rr": 0.07, "use_path": False})["fair"] == pytest.approx(fair)
    conn.close()
    r = rich_client.get("/valuation")
    assert r.status_code == 200 and "COST" in r.text
    page = rich_client.get("/v/COST")
    assert page.status_code == 200 and "Implied exit multiple" in page.text and 'id="exit-ebitda"' in page.text
    mine = {"rr": 0.08, "terminal": 0.02, "fcf_margin": 0.25, "shares": 10, "g_start": 0.05, "analyst": [120, 130]}
    assert rich_client.post("/v/COST/inputs", json={"inputs": mine}).status_code == 200
    assert rich_client.post("/v/COST/inputs", json={"inputs": {**mine, "rr": 0.01}}).status_code == 400
    conn = db.connect(tmp_path / "r.db")
    [row] = [x for x in valuation.summary(conn, ["COST"])]
    assert row["mine"] is not None and row["mine"] != pytest.approx(row["auto"])
    assert row["upside"] == pytest.approx((row["mine"] / 380 - 1) * 100)


def test_valuation_schedule():
    from web import scheduler
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    sat = datetime(2026, 11, 14, 10, 5, tzinfo=ny)
    assert scheduler.valuation_due(sat, (sat - timedelta(days=7)).isoformat())
    assert not scheduler.valuation_due(sat, (sat - timedelta(hours=1)).isoformat())
    assert not scheduler.valuation_due(datetime(2026, 11, 12, 10, 5, tzinfo=ny), (sat - timedelta(days=4)).isoformat())
    assert scheduler.valuation_due(datetime(2026, 11, 12, 10, 5, tzinfo=ny), None)


def test_fix_share_units():
    from radar import valuation
    a = {"shares": 1.44}  # BF class-A equivalents (M)
    assert "market cap" in valuation.fix_share_units(a, 2156.0) and a["shares"] == 2156.0
    b = {"shares": 5186.0}
    assert valuation.fix_share_units(b, 5190.0) is None and b["shares"] == 5186.0
    assert valuation.fix_share_units({"shares": 10.0}, None) is None


def test_discover_settings_routes(rich_client, tmp_path):
    (tmp_path / "discovery.yaml").write_text("screen: [[gt, intradaymarketcap, 10000000000]]\nthemes: {AI: {tickers: [QCOM]}}\n")
    r = rich_client.get("/discover/settings")
    assert r.status_code == 200 and "AI: QCOM" in r.text
    bad = rich_client.post("/discover/settings", data={"themes": "oops", "screen_size": "50"}, follow_redirects=False)
    assert bad.status_code == 303 and "error=" in bad.headers["location"]
    ok = rich_client.post("/discover/settings", data={"mcap_b": "5", "themes": "AI: QCOM, AMD", "funds": "",
                                                      "screen_size": "30", "openinsider": "on"}, follow_redirects=False)
    assert ok.headers["location"] == "/discover"
    assert "AI: QCOM, AMD" in rich_client.get("/discover/settings").text
    r = rich_client.get("/discover?sort=upside")
    assert r.status_code == 200 and "TRNS" in r.text


def test_earnings_add_remove(client, monkeypatch):
    import web.app as wa
    monkeypatch.setattr(wa, "_fetch_new", lambda t: None)
    r = client.post("/tracked", data={"ticker": "amd", "tier": "watch", "back": "/earnings"}, follow_redirects=False)
    assert r.headers["location"] == "/earnings"
    page = client.get("/earnings").text
    assert "Add ticker" in page and "AMD" in page  # listed under “No earnings date yet”
    r = client.post("/tracked/AMD/delete", data={"back": "/earnings"}, follow_redirects=False)
    assert r.headers["location"] == "/earnings" and ">AMD<" not in client.get("/earnings").text
