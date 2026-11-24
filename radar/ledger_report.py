"""Month-end household finance report: aggregate the ledger → Claude → stored in ledger.db (private).

Runs as `python -m radar ledger_report [YYYY-MM]` (the web scheduler fires it on the last day of each month).
Only aggregates and the month's largest items go to Claude, not the whole ledger."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path

from radar import ledger
from radar.bookkeeping import db

log = logging.getLogger(__name__)
PROMPT = Path(__file__).parent / "prompts" / "ledger_monthly.md"

SCHEMA = """CREATE TABLE IF NOT EXISTS ledger_reports (
  id INTEGER PRIMARY KEY, ym TEXT NOT NULL, created TEXT NOT NULL, markdown TEXT NOT NULL,
  ok INTEGER NOT NULL, data_until TEXT, bundle TEXT)"""


CHECKS = """CREATE TABLE IF NOT EXISTS ledger_checks (
  id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL, idx INTEGER NOT NULL, text TEXT NOT NULL,
  reply TEXT, status TEXT NOT NULL DEFAULT 'open', updated TEXT)"""  # status: open | done | ignore ; idx 0 = Overall note
STATUS_ZH = {"open": "Open", "done": "Done", "ignore": "Ignore"}
_CHECK_RE = re.compile(r"```checks[^\n]*\n(.*?)```", re.S)


def ensure() -> None:
    ledger.ensure()
    db.execute(SCHEMA)
    db.execute(CHECKS)


def split_checks(md: str) -> tuple[str, list[str]]:
    """Pull the ```checks block out of Claude's markdown → (markdown without it, items)."""
    m = _CHECK_RE.search(md)
    if not m:
        return md, []
    items = [re.sub(r"^\s*(?:[-*•]|\d+[., )])\s*", "", ln).strip() for ln in m.group(1).splitlines()]
    return (md[:m.start()] + md[m.end():]).rstrip(), [i for i in items if i][:12]


def checks(rid: int) -> list[dict]:
    ensure()
    return db.query_all("SELECT * FROM ledger_checks WHERE report_id=? ORDER BY idx", (rid,))


def save_check(cid: int, reply: str, status: str) -> None:
    db.execute("UPDATE ledger_checks SET reply=?, status=?, updated=? WHERE id=?",
               (reply.strip()[:2000] or None, status if status in STATUS_ZH else "open",
                datetime.now(timezone.utc).isoformat(timespec="seconds"), cid))


def previous_checks(ym: str, months: int = 3) -> str:
    """Earlier months' check items + the user's replies, for the next report's bundle."""
    rows = db.query_all(
        "SELECT r.ym, c.idx, c.text, c.reply, c.status FROM ledger_checks c JOIN ledger_reports r ON r.id=c.report_id "
        "WHERE r.ym<? AND r.ym>=? AND r.id IN (SELECT MAX(id) FROM ledger_reports GROUP BY ym) ORDER BY r.ym, c.idx",
        (ym, _shift(ym, -months)))
    rows = [r for r in rows if r["idx"] or r["reply"]]
    if not rows:
        return ""
    L = ["", "## Earlier check items and my replies"]
    for r in rows:
        head = "Overall note" if r["idx"] == 0 else r["text"]
        L.append(f"- [{r['ym']}|{STATUS_ZH.get(r['status'], r['status'])}] {head}"
                 + (f" → Me: {r['reply']}" if r["reply"] else " (no reply)"))
    return "\n".join(L)


def _shift(ym: str, months: int) -> str:
    y, m = map(int, ym.split("-"))
    i = y * 12 + (m - 1) + months
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def _usd(x: float) -> str:
    return f"${x:,.0f}"


def _cat_avg(ym: str, n: int = 12) -> dict[str, float]:
    d0, _ = ledger._month_bounds(_shift(ym, -n))
    _, d1 = ledger._month_bounds(_shift(ym, -1))
    return {g["cat"]: g["usd"] / n for g in ledger.categories(d0, d1)}


def _owner_split(d0: str, d1: str) -> list[tuple[str, float]]:
    rows = db.query_all("SELECT COALESCE(a.owner,'Shared/unassigned') AS owner, t.currency, SUM(t.amount) AS total "
                        "FROM transactions t LEFT JOIN accounts a ON t.account_id=a.id WHERE t.deleted_at IS NULL "
                        "AND t.type='expense' AND t.occurred_at>=? AND t.occurred_at<? GROUP BY owner, t.currency",
                        (d0, d1))
    r = ledger.usd_rates()
    out: dict[str, float] = {}
    for x in rows:
        out[x["owner"]] = out.get(x["owner"], 0) + (x["total"] or 0) * r.get(x["currency"], 0)
    return sorted(out.items(), key=lambda kv: -kv[1])


def build_bundle(ym: str) -> str:
    d0, d1 = ledger._month_bounds(ym)
    st = ledger.stats()
    cur = ledger._flows(d0, d1)
    prev = ledger._flows(*ledger._month_bounds(_shift(ym, -1)))
    yoy = ledger._flows(*ledger._month_bounds(_shift(ym, -12)))
    series = ledger.monthly(13, ym)
    past = series[:-1]
    avg_exp = sum(s["expense"] for s in past) / len(past)
    avg_inc = sum(s["income"] for s in past) / len(past)
    y = ym[:4]
    ytd = ledger._flows(f"{y}-01-01", d1)
    ly = str(int(y) - 1)
    lytd = ledger._flows(f"{ly}-01-01", ledger._month_bounds(f"{ly}{ym[4:]}")[1])

    def sr(f):
        return f"{(f['income'] - f['expense']) / f['income'] * 100:.0f}%" if f["income"] else "—"

    L = [f"# {ym} household finance data", f"- Latest transaction in the ledger: {(st['last'] or '')[:10]}; last import: {(st['last_sync'] or {}).get('imported_at', '—')}",
         "- FX rates (→USD): " + ", ".join(f"{c} {v:.4f}" for c, v in ledger.usd_rates().items() if c != "USD"), "",
         "## Income and spending (in USD)", "| | Income | Expense | Surplus | Savings rate |", "|---|---|---|---|---|"]
    for name, f in [("This month", cur), ("Last month", prev), ("Same month last year", yoy)]:
        L.append(f"| {name} | {_usd(f['income'])} | {_usd(f['expense'])} | {_usd(f['income'] - f['expense'])} | {sr(f)} |")
    L.append(f"| 12-month average | {_usd(avg_inc)} | {_usd(avg_exp)} | {_usd(avg_inc - avg_exp)} | — |")
    L += ["", "By currency (this month): " + "; ".join(f"{c} Income {x['income']:,.0f} Expense {x['expense']:,.0f}"
                                         for c, x in cur["by_ccy"].items())]
    cats = ledger.categories(d0, d1)
    pc = {g["cat"]: g["usd"] for g in ledger.categories(*ledger._month_bounds(_shift(ym, -1)))}
    yc = {g["cat"]: g["usd"] for g in ledger.categories(*ledger._month_bounds(_shift(ym, -12)))}
    ac = _cat_avg(ym)
    L += ["", "## Spending by category (USD)", "| Category | This month | Last month | 12-mo avg | Same month last year | Top subcategories this month |", "|---|---|---|---|---|---|"]
    for g in cats:
        subs = ", ".join(f"{s or '—'} {_usd(v)}" for s, v in g["subs"][:3])
        L.append(f"| {g['cat']} | {_usd(g['usd'])} | {_usd(pc.get(g['cat'], 0))} | {_usd(ac.get(g['cat'], 0))} | "
                 f"{_usd(yc.get(g['cat'], 0))} | {subs} |")
    gone = [c for c in ac if c not in {g['cat'] for g in cats} and ac[c] > 100]
    if gone:
        L.append("Categories missing this month but averaging >$100 a month: " + ", ".join(f"{c} (avg {_usd(ac[c])})" for c in gone))
    L += ["", "## 15 largest expenses this month", "| Date | Category | Subcategory | Account | Note | Amount |", "|---|---|---|---|---|---|"]
    for t in ledger.transactions(d0=d0, d1=d1, type_="expense", order="amount", limit=15):
        L.append(f"| {t['occurred_at'][:10]} | {t['cat']} | {t['sub'] or ''} | {t['account'] or ''} | "
                 f"{(t['note'] or '')[:40]} | {t['currency']} {t['amount']:,.2f} |")
    L += ["", "## Income sources (USD)"] + [f"- {g['cat']}: {_usd(g['usd'])} (" + ", ".join(
        f"{s or '—'} {_usd(v)}" for s, v in g["subs"][:3]) + ")" for g in ledger.categories(d0, d1, "income")]
    L += ["", "## Spending by account owner"] + [f"- {o}: {_usd(v)}" for o, v in _owner_split(d0, d1)]
    L += ["", f"## Year to date (through {ym})",
          f"- {y}: income {_usd(ytd['income'])}, expenses {_usd(ytd['expense'])}, surplus {_usd(ytd['income'] - ytd['expense'])}, savings rate {sr(ytd)}",
          f"- {ly}  same period: income {_usd(lytd['income'])}, expenses {_usd(lytd['expense'])}, surplus {_usd(lytd['income'] - lytd['expense'])}, savings rate {sr(lytd)}",
          "", "## Last 13 months", "| Month | Income | Expense |", "|---|---|---|"]
    L += [f"| {s['month']} | {_usd(s['income'])} | {_usd(s['expense'])} |" for s in series]
    return "\n".join(L)


def generate(ym: str, runner, now: datetime | None = None) -> tuple[int, bool]:
    ensure()
    from radar import fi
    bundle = build_bundle(ym) + fi.report_section() + previous_checks(ym)
    try:
        md, ok = runner(PROMPT.read_text() + "\n\n" + bundle), True
    except Exception as e:
        log.warning("claude failed: %s", e)
        md, ok = f"> ⚠️ Claude generation failed ({e}); raw data below.\n\n{bundle}", False
    md, items = split_checks(md)
    rid = db.execute("INSERT INTO ledger_reports (ym, created, markdown, ok, data_until, bundle) VALUES (?,?,?,?,?,?)",
                     (ym, (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"), md, int(ok),
                      (ledger.stats()["last"] or "")[:10], bundle if ok else None))
    for i, text in enumerate(["Overall note"] + items):
        db.execute("INSERT INTO ledger_checks (report_id, idx, text) VALUES (?,?,?)", (rid, i, text))
    return rid, ok


def reports() -> list[dict]:
    ensure()
    return db.query_all("SELECT id, ym, created, ok, data_until FROM ledger_reports ORDER BY ym DESC, id DESC")


def get(rid: int) -> dict | None:
    ensure()
    return db.query_one("SELECT * FROM ledger_reports WHERE id=?", (rid,))


def has(ym: str) -> bool:
    ensure()
    return bool(db.query_one("SELECT 1 AS x FROM ledger_reports WHERE ym=? AND ok=1", (ym,)))


def due_month(local_now: datetime, first: str = "2026-10") -> str | None:
    """Month whose report should run now: this month from 21:00 on its last day; or last month if it was missed."""
    today = local_now.date()
    ym = today.strftime("%Y-%m")
    last_day = (date(today.year + today.month // 12, today.month % 12 + 1, 1) - date.resolution).day
    if today.day == last_day and local_now.hour >= 21 and not has(ym):
        return ym
    prev = _shift(ym, -1)
    if prev >= first and not has(prev):
        return prev
    return None
