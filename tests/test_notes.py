from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import pytest

from radar import agent, db, notes, weekly
from radar.config import Config
from tests.test_web import rich_client  # noqa: F401  (fixture)

NOW = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "n.db")
    yield c
    c.close()


def test_add_list_filter_edit_delete_restore(conn):
    a = notes.add(conn, NOW - timedelta(days=1), kind="Learning", body="Keep DCF terminal growth below GDP", ticker="")
    b = notes.add(conn, NOW, kind="Earnings", body="Azure growth **40%**", ticker="msft", earnings_date="2026-10-28",
                  source_url="https://example.com/x", title="FY26Q1")
    assert [r["id"] for r in notes.list_notes(conn)] == [b, a]  # newest first
    assert [r["id"] for r in notes.list_notes(conn, ticker="MSFT")] == [b]
    assert [r["id"] for r in notes.list_notes(conn, kind="Learning")] == [a]
    r = notes.get(conn, b)
    assert r["ticker"] == "MSFT" and r["created"].endswith("+00:00") and r["deleted"] is None
    assert notes.by_report(conn)[("MSFT", "2026-10-28")][0]["id"] == b
    assert notes.counts_by_ticker(conn) == {"MSFT": 1}

    assert notes.update(conn, a, NOW + timedelta(hours=1), kind="Other", body="edited", ticker="NVDA")
    r = notes.get(conn, a)
    assert (r["kind"], r["body"], r["ticker"]) == ("Other", "edited", "NVDA") and r["updated"] != r["created"]

    assert notes.delete(conn, a, NOW) and not notes.delete(conn, a, NOW)
    assert notes.get(conn, a) is None and [r["id"] for r in notes.list_notes(conn)] == [b]
    assert not notes.update(conn, a, NOW, kind="Learning", body="x")  # deleted notes can't be edited
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 2  # soft delete
    assert notes.restore(conn, a) and notes.get(conn, a) is not None


@pytest.mark.parametrize("fields,msg", [
    ({"kind": "gossip", "body": "x"}, "type"),
    ({"kind": "Learning", "body": "   "}, "empty"),
    ({"kind": "Learning", "body": "x", "ticker": "bad ticker"}, "ticker"),
    ({"kind": "Learning", "body": "x", "ticker": "<script>"}, "ticker"),
    ({"kind": "Learning", "body": "x", "source_url": "javascript:alert(1)"}, "Links"),
    ({"kind": "Earnings", "body": "x", "earnings_date": "2026-10-28"}, "earnings reference"),
    ({"kind": "Earnings", "body": "x", "ticker": "MSFT", "earnings_date": "yesterday"}, "earnings reference"),
    ({"kind": "Learning", "body": "x" * (notes.MAX_BODY + 1)}, "too long"),
])
def test_validation(conn, fields, msg):
    with pytest.raises(ValueError, match=msg):
        notes.add(conn, NOW, **fields)
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0


def test_for_prompt_bounded(conn):
    for i in range(30):
        notes.add(conn, NOW - timedelta(hours=i), kind="Earnings", ticker="MSFT", body=f"msft {i} " + "long" * 2000)
    for i in range(30):
        notes.add(conn, NOW - timedelta(hours=i), kind="Learning", body=f"general {i}")
    for t in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ", "KKK", "LLL", "MMM", "NNN"):
        for i in range(6):
            notes.add(conn, NOW - timedelta(hours=i), kind="Other", ticker=t, body=f"{t} {i}")
    notes.add(conn, NOW - timedelta(days=200), kind="Learning", body="too old")
    gone = notes.add(conn, NOW, kind="Learning", body="deleted one")
    notes.delete(conn, gone, NOW)

    lines = notes.for_prompt(conn, NOW)
    assert len(lines) == 60  # total cap
    wide = notes.for_prompt(conn, NOW, total=1000)
    assert sum(" MSFT" in ln for ln in wide) == 5 and sum("general" in ln for ln in wide) == 10
    assert len(wide) == 5 + 10 + 14 * 5  # per-ticker / no-ticker caps
    assert all(len(ln) < 500 for ln in lines)
    assert not any("too old" in ln or "deleted one" in ln for ln in lines)
    assert lines[0].startswith(f"- {NOW:%Y-%m-%d}")  # newest first
    only = notes.for_prompt(conn, NOW, ticker="msft", per_ticker=3)
    assert len(only) == 3 and all("MSFT" in ln for ln in only)

    bundle = weekly.build_bundle(conn, Config(holdings=["MSFT"], watchlist=[]), NOW)
    sec = bundle.split("## User notes")[1].split("\n## ")[0]
    assert "msft 0" in sec and len(sec) < 60 * 500


def test_bundle_without_notes(conn):
    bundle = weekly.build_bundle(conn, Config(holdings=["MSFT"], watchlist=[]), NOW)
    assert "## User notes" in bundle and "## Thesis summaries" in bundle


def test_page_context_includes_notes(conn):
    notes.add(conn, datetime.now(timezone.utc), kind="Earnings", ticker="NVDA", body="Data center revenue beat again")
    assert "Data center revenue beat again" in agent.page_context(conn, "/t/nvda")
    assert "Data center revenue beat again" in agent.page_context(conn, "/notes")
    assert "Data center revenue" not in agent.page_context(conn, "/t/msft")


def test_notes_routes(rich_client, tmp_path):  # noqa: F811
    c = rich_client
    r = c.get("/notes")
    assert r.status_code == 200 and "Add note" in r.text and "No notes yet" in r.text and 'href="/notes"' in r.text

    r = c.post("/notes", data={"kind": "Learning", "body": "Moat: look at **ROIC**", "ticker": "", "source_url": ""},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/notes#n-")
    nid = int(r.headers["location"].split("-")[-1])
    r = c.post("/notes", data={"kind": "Earnings", "body": "I think capex is too high", "ticker": "msft",
                               "earnings_date": "2026-10-28", "back": "/earnings#e-MSFT-2026-10-28"},
               follow_redirects=False)
    assert r.headers["location"] == "/earnings#e-MSFT-2026-10-28"
    r = c.post("/notes", data={"kind": "Learning", "body": "x", "back": "//evil.com"}, follow_redirects=False)
    assert r.headers["location"].startswith("/notes#n-")

    page = c.get("/notes").text
    assert "<strong>ROIC</strong>" in page and "I think capex is too high" in page
    assert page.index("I think capex is too high") < page.index("ROIC")  # newest first
    msft = c.get("/notes?ticker=MSFT").text
    assert "capex" in msft and "ROIC" not in msft
    assert "ROIC" in c.get("/notes?kind=Learning").text and "capex" not in c.get("/notes?kind=Learning").text

    # bad input re-renders the form with the typed text kept
    r = c.post("/notes", data={"kind": "Learning", "body": "keep me", "ticker": "bad t"})
    assert r.status_code == 400 and "Invalid ticker format" in r.text and "keep me" in r.text
    r = c.post("/notes", data={"kind": "Learning", "body": "<b>x</b>", "source_url": "javascript:alert(1)"})
    assert r.status_code == 400

    # edit
    assert 'name="body"' in c.get(f"/notes?edit={nid}").text
    r = c.post(f"/notes/{nid}", data={"kind": "Other", "body": "edited <script>", "back": "/notes?kind=Other"},
               follow_redirects=False)
    assert unquote(r.headers["location"]) == f"/notes?kind=Other#n-{nid}"
    page = c.get("/notes?kind=Other").text
    assert "edited &lt;script&gt;" in page and "edited <script>" not in page
    assert c.post("/notes/99999", data={"kind": "Learning", "body": "x"}).status_code == 404
    assert c.post(f"/notes/{nid}", data={"kind": "Learning", "body": ""}).status_code == 400

    # Earnings page shows the attached note + “Add note” prefill link; Theses page shows the ticker's notes
    e = c.get("/earnings").text
    assert "I think capex is too high" in e and "/notes?new=1&ticker=MSFT&kind=Earnings&ref=2026-10-28&back=/earnings%23e-MSFT-2026-10-28" in e
    assert "MSFT All notes (1)" in e
    pre = c.get("/notes?new=1&ticker=MSFT&kind=Earnings&ref=2026-10-28&back=/earnings%23e-MSFT-2026-10-28").text
    assert 'name="earnings_date" value="2026-10-28"' in pre and '<option value="Earnings" selected>' in pre
    assert 'value="/earnings#e-MSFT-2026-10-28"' in pre
    t = c.get("/theses").text
    assert "I think capex is too high" in t and "All notes (1)" in t

    # delete (soft) + undo
    r = c.post(f"/notes/{nid}/delete", data={"back": "/notes"}, follow_redirects=False)
    assert r.headers["location"] == f"/notes?deleted={nid}"
    page = c.get(f"/notes?deleted={nid}").text
    assert "edited" not in page and f"/notes/{nid}/restore" in page
    assert c.post(f"/notes/{nid}/delete").status_code == 404
    c.post(f"/notes/{nid}/restore")
    assert "edited" in c.get("/notes").text
