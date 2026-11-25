import sqlite3

import pytest
from fastapi.testclient import TestClient

from radar import ledger

# Toy ledger rows shared by both editions (loaded with the generic CSV importer).
ROWS = [("2026-11-03", "expense", 156.73, "USD", "Visa", "Food & drink", "COSTCO", "COSTCO"),
        ("2026-11-04", "expense", 40, "USD", "Visa", "Transportation", "Gas", "Gas fill-up"),
        ("2026-11-01", "income", 3000, "CAD", "Main Chequing", "Employment income", "Salary", "Pay"),
        ("2026-10-02", "expense", 80, "USD", "Visa", "Food & drink", "Dining out", "lunch")]


def _load(extra=()):
    from radar import ledger_csv
    ledger_csv.insert_rows([{"type": t, "occurred_at": d + " 12:00:00", "amount": a, "currency": c, "account": acc,
                             "category": cat, "subcategory": sub, "member": "", "note": note,
                             "account_type": "unknown", "owner": None}
                            for d, t, a, c, acc, cat, sub, note in [*ROWS, *extra]], "t.csv")


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "watchlist.yaml").write_text("holdings: [COST]\nwatchlist: []\n")
    (tmp_path / "rules.yaml").write_text("rules: []\n")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "w.db"))
    monkeypatch.setenv("LEDGER_DB", str(tmp_path / "ledger.db"))
    from web import ledger_routes
    ledger_routes._fails.clear()
    monkeypatch.setattr(ledger, "usd_rates", lambda: {"USD": 1.0, "CAD": 0.7})
    from web.app import app
    return TestClient(app)


def test_gate_setup_then_login(client):
    r = client.get("/ledger")
    assert "set a password just for the ledger" in r.text and "no-store" in r.headers["cache-control"]
    assert "The two entries don’t match" in client.post("/ledger/setup", data={"pw": "abcdefgh", "pw2": "x"}).text
    r = client.post("/ledger/setup", data={"pw": "abcdefgh", "pw2": "abcdefgh"}, follow_redirects=False)
    assert r.status_code == 303 and "ledger_s" in r.headers["set-cookie"]
    assert client.post("/ledger/setup", data={"pw": "zzzzzzzz", "pw2": "zzzzzzzz"}).status_code == 403
    client.cookies.clear()
    assert "Ledger password" in client.get("/ledger").text
    assert "Wrong password" in client.post("/ledger/login", data={"pw": "nope"}).text
    r = client.post("/ledger/login", data={"pw": "abcdefgh"})
    assert "The ledger is empty" in r.text


def test_lockout_after_failures(client):
    client.post("/ledger/setup", data={"pw": "abcdefgh", "pw2": "abcdefgh"})
    client.cookies.clear()
    for _ in range(5):
        client.post("/ledger/login", data={"pw": "bad"})
    assert "Too many wrong attempts" in client.post("/ledger/login", data={"pw": "abcdefgh"}).text


def test_pages_locked_without_cookie(client):
    client.post("/ledger/setup", data={"pw": "abcdefgh", "pw2": "abcdefgh"})
    client.cookies.clear()
    for path in ("/ledger/tx", "/ledger/accounts", "/ledger/sync"):
        assert "Ledger password" in client.get(path).text
    client.cookies.set("ledger_s", "9999999999.forged", path="/ledger")
    assert "Ledger password" in client.get("/ledger/tx").text


def test_bootstrap_only_when_empty(client, tmp_path):
    src = tmp_path / "old.db"
    c = sqlite3.connect(src)
    c.executescript(ledger.db.SCHEMA_PATH.read_text())
    c.execute("INSERT INTO transactions (type, occurred_at, amount, currency, source, hash) "
              "VALUES ('expense','2026-01-01',5,'USD','csv','h1')")
    c.commit(); c.close()
    assert client.post("/ledger/bootstrap", content=b"junk").status_code == 400
    r = client.post("/ledger/bootstrap", content=src.read_bytes())
    assert r.json() == {"transactions": 1}
    assert client.post("/ledger/bootstrap", content=src.read_bytes()).status_code == 403


def test_agent_context_hides_ledger(client):
    from radar import agent, db
    conn = db.connect(":memory:")
    assert "not sent to the assistant" in agent.page_context(conn, "/ledger/tx")


def test_ledger_report(client, tmp_path):
    from datetime import datetime
    from radar import ledger_report as lr
    client.post("/ledger/setup", data={"pw": "abcdefgh", "pw2": "abcdefgh"})
    _load()
    b = lr.build_bundle("2026-11")
    assert "| This month | $2,100 | $197 |" in b and "Food & drink" in b and "Gas fill-up" in b and "| 2026-10 |" in b
    seen = {}
    rid, ok = lr.generate("2026-11", lambda p: seen.setdefault("p", p) and "## One-liner\nGood")
    assert ok and "monthly household finance report" in seen["p"] and "2026-11 household finance data" in seen["p"]
    r = client.get(f"/ledger/reports/{rid}")
    assert "Good" in r.text and "Raw summary data" in r.text
    rid2, ok2 = lr.generate("2026-10", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    assert not ok2 and "data only" in client.get("/ledger/reports").text
    # due logic: last day 21:00 → this month; a missed month → catch up once
    assert lr.due_month(datetime(2026, 11, 30, 21, 5)) == "2026-10"  # 2026-11 done; 2026-10 only failed → retry
    assert lr.due_month(datetime(2026, 11, 30, 21, 5), first="2026-11") is None
    assert lr.due_month(datetime(2026, 12, 31, 20, 0)) is None
    assert lr.due_month(datetime(2026, 12, 31, 21, 0)) == "2026-12"
    assert lr.due_month(datetime(2027, 1, 2, 9, 0)) == "2026-12"
    client.cookies.clear()
    assert "Ledger password" in client.get(f"/ledger/reports/{rid}").text


def test_report_checks_and_replies(client, tmp_path):
    from radar import ledger_report as lr
    client.post("/ledger/setup", data={"pw": "abcdefgh", "pw2": "abcdefgh"})
    out = "## One-liner\nok\n\n```checks\n- Check #1001 $1,200 has no payee\n2. Unverified expense $120\n```\n"
    rid, _ = lr.generate("2026-10", lambda p: out)
    r = client.get(f"/ledger/reports/{rid}").text
    assert "```checks" not in r and "Check #1001 $1,200 has no payee" in r and "Overall note" in r
    cs = lr.checks(rid)
    assert [c["text"] for c in cs] == ["Overall note", "Check #1001 $1,200 has no payee", "Unverified expense $120"]
    client.post(f"/ledger/reports/{rid}/checks/{cs[1]['id']}", data={"reply": "Furniture store final payment", "status": "done"})
    assert client.post(f"/ledger/reports/{rid}/checks/99999", data={"reply": "x"}).status_code == 404
    seen = {}
    lr.generate("2026-11", lambda p: seen.setdefault("p", p) or "x")
    assert "Earlier check items and my replies" in seen["p"] and "[2026-10|Done] Check #1001" in seen["p"] and "Furniture store final payment" in seen["p"]
    assert "Unverified expense $120 (no reply)" in seen["p"]


def test_ledger_agent_context_and_replies(client, tmp_path, monkeypatch):
    from radar import agent, ledger_agent, ledger_report as lr
    assert client.post("/ledger/agent", json={"q": "hi", "page": "/ledger"}).status_code == 403  # locked
    client.post("/ledger/setup", data={"pw": "abcdefgh", "pw2": "abcdefgh"})
    _load()
    rid, _ = lr.generate("2026-11", lambda p: "ok\n```checks\nCheck #1001 has no payee\n```")
    ctx = ledger_agent.context(f"/ledger/reports/{rid}")
    assert "1|open|Check #1001 has no payee" in ctx and "Food & drink" in ctx and "reply" in ctx

    async def fake_run(argv, prompt, cwd):
        assert "Check #1001" in prompt and "# Question\n#1001 is the furniture store" in prompt
        for t in ["OK, noted.\n```reply\n", "1|done|Furniture store final payment, payee ABC Electric\n9|done|bogus\n```"]:
            yield {"t": "text", "v": t}
        yield {"t": "done", "v": "s"}
    monkeypatch.setattr(agent, "run", fake_run)
    r = client.post("/ledger/agent", json={"q": "#1001 is the furniture store", "page": f"/ledger/reports/{rid}"})
    assert '"t": "saved", "v": [1]' in r.text
    c = lr.checks(rid)[1]
    assert c["status"] == "done" and "ABC Electric" in c["reply"]


def test_year_insights(client, tmp_path, monkeypatch):
    from datetime import date
    monkeypatch.setattr(ledger, "PROPERTIES", ("Maple St",))
    client.post("/ledger/setup", data={"pw": "abcdefgh", "pw2": "abcdefgh"})
    _load([("2025-11-03", "expense", 100, "USD", "Visa", "Food & drink", "COSTCO", ""),
           ("2026-03-01", "expense", 2000, "USD", "Visa", "Maple St Home", "Maple St - Mortgage", ""),
           ("2026-03-02", "income", 2500, "USD", "Visa", "Rental income", "Maple St rental", "")])
    v = ledger.year_insights(2026, date(2026, 11, 10))
    ms = next(h for h in v["properties"] if h["name"] == "Maple St")
    assert ms["rent"] == 2500 and ms["mortgage"] == 2000 and ms["net"] == 500
    assert dict((k, n) for k, n, _ in v["buckets"])["Property (Home categories)"] == 2000
    assert len(v["months"]) == 11 and v["peak"]["month"] == "2026-03"
    assert any("Maple St: rent" in h for h in v["highlights"])
    r = client.get("/ledger?y=2026")
    assert r.status_code == 200 and "Properties" in r.text and "By year" in r.text and "Highlights" in r.text


def test_month_partial_compares_same_days(client, tmp_path):
    from datetime import date
    client.post("/ledger/setup", data={"pw": "abcdefgh", "pw2": "abcdefgh"})
    _load([("2026-10-25", "expense", 500, "USD", "Visa", "Food & drink", "Dining out", "")])
    v = ledger.month_view("2026-11", date(2026, 11, 10))
    assert v["partial"] and v["prev_label"] == "Same days last month (1–10)"
    assert round(v["prev_flows"]["expense"]) == 80  # 10-02 only; 10-25 excluded
    full = ledger.month_view("2026-10", date(2026, 11, 10))
    assert not full["partial"] and v["cats"][0]["prev"] == 80


def test_fi_tracker(client, tmp_path):
    from radar import fi
    client.post("/ledger/setup", data={"pw": "abcdefgh", "pw2": "abcdefgh"})
    _load([("2026-03-01", "expense", 2000, "USD", "Visa", "Maple St Home", "Maple St - Mortgage", ""),
           ("2026-03-05", "expense", 9000, "USD", "Visa", "Oak Ave Home", "Oak Ave - Remodel", ""),
           ("2026-03-02", "income", 2500, "USD", "Visa", "Rental income", "Maple St rental", "")])
    assert "No asset snapshot yet" in client.get("/ledger/fi").text
    a = fi.auto_spending()
    assert a["oneoff"] == 9000 and a["buckets"]["mortgage"] == 2000 and a["rent"] == 2500
    client.post("/ledger/fi/snapshot", data={"ym": "2026-11", "item": ["Brokerage", "Cash", ""],
                                             "owner": [ledger.ME, "Household", "Household"], "currency": ["USD", "CAD", "USD"],
                                             "amount": ["100,000", "10000", ""]})
    v = fi.view()
    assert round(v["assets"]) == 107000  # CAD 10k × 0.7
    assert v["target"] == v["target_paid"] == v["need_paid"] / 0.04  # no mortgage balance entered yet
    assert v["need"] - v["need_paid"] == 2000
    assert v["milestones"][0]["done"]
    client.post("/ledger/fi/settings", data={"wr": "4", "real_return": "5", "health": "0", "o_daily": "1000",
                                             "mortgage_balance": "500,000"})
    v = fi.view()
    assert v["target"] == v["target_paid"] + 500000
    assert v["buckets"]["daily"] == 1000
    g = v["goals"][0]
    assert g["name"] == f"Brokerage account ({ledger.ME})" and g["target"] is None and g["current"] == 100000
    client.post("/ledger/fi/goal", data={"id": str(g["id"]), "name": "Brokerage goal", "target": "200000",
                                         "monthly": "1000", "deadline": "2031-12"})
    client.post("/ledger/fi/goal", data={"name": "Cash reserve", "target": "20000", "items": ["Cash"]})
    gs = fi.view()["goals"]
    assert gs[0]["target"] == 200000 and gs[0]["months"] and gs[0]["need_monthly"] is not None
    assert gs[1]["name"] == "Cash reserve" and round(gs[1]["current"]) == 7000
    assert fi.required_monthly(100, 100, 12, 0.05) == 0
    client.post(f"/ledger/fi/goal/{gs[1]['id']}/delete")
    assert len(fi.view()["goals"]) == 1
    assert fi.months_to(100, 100, 0, 0.05) == 0 and fi.months_to(1e12, 0, 1, 0.0) is None
    r = client.get("/ledger/fi").text
    assert "Household financial independence" in r and "Brokerage goal" in r and "would last" in r
    assert "FI progress" in fi.report_section()


def test_csv_import_generic(client, tmp_path):
    from radar import ledger_csv
    client.post("/ledger/setup", data={"pw": "abcdefgh", "pw2": "abcdefgh"})
    p = tmp_path / "bank.csv"
    p.write_text("Date,Amount,Account,Category,Subcategory,Note\n2026-11-02,-12.50,Visa,Food & drink,Coffee,latte\n"
                 "2026-11-02,-12.50,Visa,Food & drink,Coffee,latte\n2026-11-15,2500,Checking,Employment income,Salary,pay\n"
                 "nope,1,Visa,,,\n", encoding="utf-8")
    r = ledger_csv.import_csv(p, "bank.csv")
    assert (r["inserted"], r["skipped"], len(r["errors"])) == (3, 0, 1)   # two identical lattes both kept
    assert ledger_csv.import_csv(p, "bank.csv")["inserted"] == 0          # idempotent
    assert ledger.month_view("2026-11")["flows"]["expense"] == 25
    bad = tmp_path / "bad.csv"
    bad.write_text("when,how much\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing column"):
        ledger_csv.import_csv(bad, "bad.csv")


def test_demo_household(client, monkeypatch):
    from datetime import date
    from radar import fi, ledger_demo, ledger_report
    monkeypatch.setattr(ledger, "PROPERTIES", ())   # public edition: every "<X> Home" category is a property
    client.post("/ledger/setup", data={"pw": "abcdefgh", "pw2": "abcdefgh"})
    r = ledger_demo.load(date(2026, 11, 20))
    assert r["transactions"] > 600
    with pytest.raises(ValueError):
        ledger_demo.load(date(2026, 11, 20))             # never on top of real data
    assert client.post("/ledger/demo").status_code == 409
    v = ledger.year_insights(2026, date(2026, 11, 20))
    assert {h["name"] for h in v["properties"]} == {"Maple St", "Oak Ave"} and v["flows"]["income"] > v["flows"]["expense"]
    assert {a["name"] for a in ledger.balances()} >= {"Chase Checking", "Amex Gold", "Fidelity Brokerage"}
    f = fi.view()
    assert f["assets"] > 400000 and f["goals"][0]["target"] == 400000 and f["auto"]["oneoff"] > 0
    [rep] = ledger_report.reports()
    assert rep["ym"] == "2026-10" and len(ledger_report.checks(rep["id"])) == 3
    for page in ("/ledger", "/ledger?y=2026", "/ledger/tx", "/ledger/accounts", "/ledger/fi", "/ledger/todo",
                 f"/ledger/reports/{rep['id']}", "/ledger/sync"):
        assert client.get(page).status_code == 200, page


