import pytest

from radar import wacc

ROWS = {"Revenue (M)": [1000.0, 1200.0], "Total Debt (B)": [0.5, 0.6], "Total Cash (B)": [0.1, 0.2],
        "Interest Expense (M)": [24.0, 30.0], "Effective Tax Rate (%)": [20.0, 20.0]}
COLS = ["2024", "2025"]


def test_wacc_levered_company():
    w = wacc.compute(ROWS, COLS, price=50.0, shares_m=48.0, beta=1.0, rf=0.04)   # E = 2400, D = 600
    assert w["re"] == pytest.approx(0.04 + 1.0 * 0.05) and w["beta_raw"] == 1.0
    assert w["rd"] == pytest.approx(30 / 600) and w["tax"] == pytest.approx(0.20)
    assert w["we"] == pytest.approx(0.8) and w["wd"] == pytest.approx(0.2)
    assert w["wacc"] == pytest.approx(0.8 * 0.09 + 0.2 * 0.05 * 0.8)
    assert w["net_debt"] == pytest.approx(400) and w["year"] == "2025" and not w["financial"]


def test_wacc_financials_use_cost_of_equity_and_no_bridge():
    w = wacc.compute(ROWS, COLS, price=50.0, shares_m=48.0, beta=1.0, rf=0.04, sector="BANK")
    assert w["wacc"] == pytest.approx(w["re"]) and w["net_debt"] == 0 and w["financial"]

    w = wacc.compute(ROWS, COLS, price=50.0, shares_m=48.0, beta=1.0, rf=0.04, sector="CONSUMER", industry="Banks - Diversified")
    assert w["financial"] and w["net_debt"] == 0
    assert not wacc.compute(ROWS, COLS, 50.0, 48.0, 1.0, 0.04, industry="Credit Services")["financial"]


def test_wacc_fallbacks_and_clamps():
    w = wacc.compute({"Total Debt (B)": [1.0]}, ["2025"], price=10.0, shares_m=100.0, beta=None, rf=0.04)
    assert w["beta"] == pytest.approx(1.0) and w["tax"] == 0.21 and w["rd"] == pytest.approx(0.055)
    assert any("β" in n for n in w["notes"]) and any("利息" in n for n in w["notes"])
    w = wacc.compute({"Total Debt (B)": [1.0], "Interest Expense (M)": [500.0]}, ["2025"], 10.0, 100.0, 9.0, 0.04)
    assert w["rd"] == pytest.approx(0.12) and w["beta"] == pytest.approx(1.0)   # Rd capped at rf + 8%; absurd β ignored
    w = wacc.compute({}, [], price=None, shares_m=None, beta=0.4, rf=0.04)
    assert w["beta"] == pytest.approx(0.598) and w["wacc"] == pytest.approx(0.04 + 0.598 * 0.05)  # Blume: low β pulled toward 1
    assert w["we"] == 1.0   # no market cap → cost of equity


def test_interest_addback():
    assert wacc.interest_addback_margin(ROWS, 0.2) == pytest.approx((24 * 0.8 / 1000 + 30 * 0.8 / 1200) / 2)
    assert wacc.interest_addback_margin({"Revenue (M)": [100.0]}, 0.2) == 0.0


def test_latest_rf(tmp_path):
    from radar import db
    conn = db.connect(tmp_path / "x.db")
    assert wacc.latest_rf(conn) == (wacc.RF_DEFAULT, None)
    conn.execute("INSERT INTO macro_obs VALUES ('DGS10', '2026-11-09', 4.12)")
    assert wacc.latest_rf(conn) == (pytest.approx(0.0412), "2026-11-09")
