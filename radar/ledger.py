"""Ledger panel: read side over the household ledger + password gate + CSV import (radar/ledger_csv.py).

Import is idempotent (hash per row) and never deletes rows; `python -m radar ledger_demo` loads a fictional household.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import shutil
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path

from radar.bookkeeping import db

SYNC_SOURCE = "csv"
SESSION_HOURS = 12
# Account-owner values in the ledger that mean "me" / "my partner", and my label in FI snapshots and goals.
ME_OWNER, PARTNER_OWNER, ME = "Me", "Partner", "Me"


def local_today() -> date:
    """'Today' in the configured local time zone (the server clock is UTC)."""
    from zoneinfo import ZoneInfo

    from radar.config import load_config
    try:
        tz = load_config().tz
    except Exception:
        tz = "America/Los_Angeles"
    return datetime.now(ZoneInfo(tz)).date()
PBKDF2_ITER = 300_000
FALLBACK_USD = {"USD": 1.0, "CAD": 0.72, "CNY": 0.14, "EUR": 1.08, "HKD": 0.128}


# ---------------- password / session ----------------

def _setting(key: str) -> str | None:
    r = db.query_one("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else None


def _put(key: str, value: str) -> None:
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))


def ensure() -> None:
    db.init_db()
    _migrate_months()
    seed_tasks()
    from radar import fi
    fi.seed_goals()
    if not _setting("fi_task_v1"):
        add_task("Update the FI asset snapshot", "Ledger → FI: enter this month’s investable assets (stocks/retirement/cash)", 10)
        _put("fi_task_v1", "1")


_MON = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_months(text: str) -> list[int]:
    """'(Feb, April, June, Aug)' → [2, 4, 6, 8]; only looks inside parentheses."""
    import re
    out: set[int] = set()
    for grp in re.findall(r"[((]([^))]*)[))]", text or ""):
        for w in re.findall(r"[A-Za-z]+", grp):
            if (i := _MON.get(w[:3].lower())) and len(w) >= 3:
                out.add(i)
    return sorted(out)


def _migrate_months() -> None:
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(monthly_tasks)")}
        if "months" not in cols:
            conn.execute("ALTER TABLE monthly_tasks ADD COLUMN months TEXT")  # '2,4,6' ; NULL = every month
    if not _setting("months_v1"):  # one-time: titles like 'Water bill (Apr, Aug, Dec)'
        for t in db.query_all("SELECT id, title FROM monthly_tasks WHERE months IS NULL"):
            ms = parse_months(t["title"])
            if ms:
                db.execute("UPDATE monthly_tasks SET months=? WHERE id=?", (",".join(map(str, ms)), t["id"]))
        _put("months_v1", "1")


def has_password() -> bool:
    return bool(_setting("pw_hash"))


def _hash(pw: str, salt: bytes, it: int = PBKDF2_ITER) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, it).hex()


def set_password(pw: str) -> None:
    salt = secrets.token_bytes(16)
    _put("pw_hash", f"pbkdf2_sha256${PBKDF2_ITER}${salt.hex()}${_hash(pw, salt)}")
    _put("session_secret", secrets.token_hex(32))  # rotating it logs out every old session


def check_password(pw: str) -> bool:
    stored = _setting("pw_hash")
    if not stored:
        return False
    _, it, salt, h = stored.split("$")
    return hmac.compare_digest(_hash(pw, bytes.fromhex(salt), int(it)), h)


def make_token(now: float | None = None) -> str:
    exp = int((now or time.time()) + SESSION_HOURS * 3600)
    sig = hmac.new(bytes.fromhex(_setting("session_secret") or ""), str(exp).encode(), "sha256").hexdigest()
    return f"{exp}.{sig}"


def valid_token(tok: str | None, now: float | None = None) -> bool:
    secret = _setting("session_secret")
    if not tok or not secret or "." not in tok:
        return False
    exp, sig = tok.split(".", 1)
    if not exp.isdigit() or int(exp) < (now or time.time()):
        return False
    good = hmac.new(bytes.fromhex(secret), exp.encode(), "sha256").hexdigest()
    return hmac.compare_digest(sig, good)


# ---------------- FX (display only: USD-equivalent totals) ----------------

_fx: dict = {"at": 0.0, "rates": dict(FALLBACK_USD)}


def usd_rates() -> dict[str, float]:
    if time.time() - _fx["at"] < 12 * 3600:
        return _fx["rates"]
    rates = dict(FALLBACK_USD)
    try:
        import yfinance as yf
        for c in ("CAD", "CNY", "EUR", "HKD"):
            v = yf.Ticker(f"{c}USD=X").fast_info.get("lastPrice")
            if v and v > 0:
                rates[c] = float(v)
    except Exception:
        pass
    _fx.update(at=time.time(), rates=rates)
    return rates


def _usd(rows, key="total") -> float:
    r = usd_rates()
    return sum((x[key] or 0) * r.get(x["currency"], 0) for x in rows)


# ---------------- queries ----------------

_CAT = "COALESCE(cp.name, c.name, 'Uncategorized')"
_JOIN = ("FROM transactions t LEFT JOIN categories c ON t.category_id=c.id "
         "LEFT JOIN categories cp ON c.parent_id=cp.id LEFT JOIN accounts a ON t.account_id=a.id "
         "LEFT JOIN members m ON t.member_id=m.id")


def stats() -> dict:
    r = db.query_one("SELECT COUNT(*) AS n, MIN(occurred_at) AS first, MAX(occurred_at) AS last "
                     "FROM transactions WHERE deleted_at IS NULL")
    last_sync = db.query_one("SELECT imported_at, filename FROM imports WHERE status IN ('success','partial') "
                             "ORDER BY id DESC LIMIT 1")
    return {**r, "last_sync": last_sync}


SYNC_REMIND_DAYS = 7


def sync_age_days(now: datetime | None = None) -> int | None:
    """Days since the last bookkeeping app import (None if the ledger doesn't exist yet). Safe to call from any page."""
    if not db.db_path().exists():
        return None
    try:
        r = db.query_one("SELECT MAX(imported_at) AS t FROM imports WHERE source_type=? "
                         "AND status IN ('success','partial')", (SYNC_SOURCE,))
    except sqlite3.Error:
        return None
    if not r or not r["t"]:
        return None
    return ((now or datetime.now(timezone.utc).replace(tzinfo=None)) - datetime.fromisoformat(r["t"])).days  # imported_at is SQLite UTC


def _month_bounds(ym: str) -> tuple[str, str]:
    y, m = map(int, ym.split("-"))
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y:04d}-{m:02d}-01", f"{ny:04d}-{nm:02d}-01"


def _prev_month(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    return f"{y - 1:04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"


def _flows(d0: str, d1: str) -> dict:
    rows = db.query_all("SELECT type, currency, ROUND(SUM(amount),2) AS total, COUNT(*) AS n FROM transactions "
                        "WHERE deleted_at IS NULL AND occurred_at>=? AND occurred_at<? AND type IN ('income','expense') "
                        "GROUP BY type, currency", (d0, d1))
    inc = [r for r in rows if r["type"] == "income"]
    exp = [r for r in rows if r["type"] == "expense"]
    by_ccy = {c: {"income": sum(r["total"] for r in inc if r["currency"] == c),
                  "expense": sum(r["total"] for r in exp if r["currency"] == c)}
              for c in sorted({r["currency"] for r in rows})}
    return {"income": _usd(inc), "expense": _usd(exp), "n": sum(r["n"] for r in rows), "by_ccy": by_ccy}


def categories(d0: str, d1: str, type_: str = "expense") -> list[dict]:
    """Parent category → USD-equivalent total, with subcategories."""
    rows = db.query_all(f"SELECT {_CAT} AS cat, COALESCE(c.name,'') AS sub, t.currency, ROUND(SUM(t.amount),2) AS total, "
                        f"COUNT(*) AS n {_JOIN} WHERE t.deleted_at IS NULL AND t.type=? AND t.occurred_at>=? "
                        f"AND t.occurred_at<? GROUP BY cat, sub, t.currency", (type_, d0, d1))
    r = usd_rates()
    out: dict[str, dict] = {}
    for x in rows:
        v = (x["total"] or 0) * r.get(x["currency"], 0)
        g = out.setdefault(x["cat"], {"cat": x["cat"], "usd": 0.0, "n": 0, "subs": {}})
        g["usd"] += v
        g["n"] += x["n"]
        g["subs"][x["sub"]] = g["subs"].get(x["sub"], 0) + v
    res = sorted(out.values(), key=lambda g: -g["usd"])
    for g in res:
        g["subs"] = sorted(g["subs"].items(), key=lambda kv: -kv[1])
    return res


def monthly(months: int = 13, end: str | None = None) -> list[dict]:
    ym = end or local_today().strftime("%Y-%m")
    seq = [ym]
    for _ in range(months - 1):
        seq.append(_prev_month(seq[-1]))
    out = []
    for m in reversed(seq):
        f = _flows(*_month_bounds(m))
        out.append({"month": m, "income": f["income"], "expense": f["expense"]})
    return out


def month_view(ym: str, today: date | None = None) -> dict:
    """For the month in progress, compare with the same days of last month (1st..today), not the full month."""
    today = today or local_today()
    d0, d1 = _month_bounds(ym)
    cur = _flows(d0, d1)
    p0, p1 = _month_bounds(_prev_month(ym))
    partial = today.strftime("%Y-%m") == ym
    if partial:
        last_day = (date.fromisoformat(d1) - date.resolution).day
        p_last = (date.fromisoformat(p1) - date.resolution).day
        p1 = (date.fromisoformat(p0) + (min(today.day, p_last)) * date.resolution).isoformat()
    prev = _flows(p0, p1)
    prev_cats = {g["cat"]: g["usd"] for g in categories(p0, p1)}
    cats = categories(d0, d1)
    for g in cats:
        g["prev"] = prev_cats.get(g["cat"], 0)
    series = monthly(13, ym)
    past = [s["expense"] for s in series[:-1] if s["expense"]]
    return {"month": ym, "flows": cur, "prev_flows": prev, "partial": partial,
            "day": today.day if partial else None, "days": last_day if partial else None,
            "prev_label": f"Same days last month (1–{today.day})" if partial else "Last month",
            "cats": cats, "income_cats": categories(d0, d1, "income"),
            "series": series, "avg12": sum(past) / len(past) if past else None,
            "big": transactions(d0=d0, d1=d1, type_="expense", order="amount", limit=8)}


def year_view(y: int) -> dict:
    d0, d1 = f"{y}-01-01", f"{y + 1}-01-01"
    return {"year": y, "flows": _flows(d0, d1), "cats": categories(d0, d1),
            "income_cats": categories(d0, d1, "income")}


TYPE_ZH = {"expense": "Expense", "income": "Income", "transfer_in": "Transfer in", "transfer_out": "Transfer out",
           "balance_adjust": "Balance adjustment", "debt_change": "Debt change", "credit_change": "Credit change"}


def transactions(q: str = "", d0: str | None = None, d1: str | None = None, type_: str | None = None,
                 cat: str | None = None, account: str | None = None, order: str = "date", limit: int = 200) -> list[dict]:
    where, args = ["t.deleted_at IS NULL"], []
    if d0:
        where.append("t.occurred_at>=?"); args.append(d0)
    if d1:
        where.append("t.occurred_at<?"); args.append(d1)
    if type_:
        where.append("t.type=?"); args.append(type_)
    if cat:
        where.append(f"{_CAT}=?"); args.append(cat)
    if account:
        where.append("a.name=?"); args.append(account)
    if q:
        where.append("(t.note LIKE ? OR c.name LIKE ? OR cp.name LIKE ? OR a.name LIKE ? OR t.project LIKE ?)")
        args += [f"%{q}%"] * 5
    ob = "t.amount DESC" if order == "amount" else "t.occurred_at DESC, t.id DESC"
    rows = db.query_all(f"SELECT t.id, t.type, t.occurred_at, t.amount, t.currency, t.note, t.project, "
                        f"a.name AS account, {_CAT} AS cat, c.name AS sub, m.name AS member {_JOIN} "
                        f"WHERE {' AND '.join(where)} ORDER BY {ob} LIMIT ?", (*args, limit))
    for r in rows:
        r["type_zh"] = TYPE_ZH.get(r["type"], r["type"])
    return rows


def balances() -> list[dict]:
    """Per-account balance from flows (credit cards: owed = −net). Same formula as the old web app."""
    rows = db.query_all(
        "SELECT a.id, a.name, a.type, a.currency, a.owner, a.expected_balance, a.expected_balance_at, "
        "COUNT(t.id) AS n, MAX(t.occurred_at) AS last, "
        "ROUND(SUM(CASE WHEN t.type IN ('income','transfer_in','balance_adjust','debt_change') THEN t.amount "
        "WHEN t.type IN ('expense','transfer_out') THEN -t.amount ELSE 0 END),2) AS net "
        "FROM accounts a LEFT JOIN transactions t ON t.account_id=a.id AND t.deleted_at IS NULL "
        "WHERE a.archived=0 GROUP BY a.id ORDER BY a.type, a.name")
    r = usd_rates()
    for x in rows:
        bal = x["net"] or 0
        x["balance"] = -bal if x["type"] == "credit_card" else bal
        x["usd"] = bal * r.get(x["currency"], 0)  # signed: card debt counts negative
    return rows


def imports(limit: int = 20) -> list[dict]:
    return db.query_all("SELECT id, source_type, filename, imported_at, rows_total, rows_inserted, rows_soft_deleted, "
                        "rows_skipped, status FROM imports ORDER BY id DESC LIMIT ?", (limit,))


# ---------------- sync ----------------

def backup(tag: str, keep: int = 15) -> Path:
    src = db.db_path()
    d = src.parent / "ledger-backups"
    d.mkdir(exist_ok=True, mode=0o700)
    dst = d / f"ledger-{datetime.now():%Y%m%d-%H%M%S}-{tag}.db"
    s, t = sqlite3.connect(src), sqlite3.connect(dst)
    s.backup(t)
    s.close(), t.close()
    dst.chmod(0o600)
    for old in sorted(d.glob("ledger-*.db"))[:-keep]:
        old.unlink()
    return dst


def sync(path: Path, filename: str, force: bool = False) -> dict:
    """Import a CSV export (columns: radar/ledger_csv.py). Returns counts like the sync page expects."""
    from radar import ledger_csv
    backup("pre-sync")
    r = ledger_csv.import_csv(path, filename)
    mark_sync_done()
    return r


def is_empty() -> bool:
    return (db.query_one("SELECT COUNT(*) AS n FROM transactions") or {"n": 0})["n"] == 0


def restore_db(raw: bytes) -> int:
    """One-time bootstrap: replace an empty ledger with an existing bookkeeping.db (the old Mac app's)."""
    if not raw.startswith(b"SQLite format 3\x00"):
        raise ValueError("Not a SQLite file")
    dst = db.db_path()
    tmp = dst.with_suffix(".upload")
    tmp.write_bytes(raw)
    tmp.chmod(0o600)
    conn = sqlite3.connect(tmp)
    try:
        n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        conn.execute("DELETE FROM settings WHERE key IN ('pw_hash','session_secret')")
        conn.commit()
    finally:
        conn.close()
    shutil.move(tmp, dst)
    db.init_db()  # add any newer tables
    return n


# ---------------- monthly to-dos ----------------

DEFAULT_TASKS = [("Import new transactions", "Export a CSV from your bank or bookkeeping app and upload it under “Import”", 10)]
SYNC_TASK = "Import new transactions"


def seed_tasks() -> None:
    if _setting("tasks_seeded"):
        return
    for i, (t, n, d) in enumerate(DEFAULT_TASKS):
        db.execute("INSERT INTO monthly_tasks (title, note, day, sort) VALUES (?, ?, ?, ?)", (t, n, d, i))
    _put("tasks_seeded", "1")
    age = sync_age_days()
    if age is not None and age <= local_today().day - 1:  # already synced this month
        mark_sync_done()


def mark_sync_done(today: date | None = None) -> None:
    db.execute("INSERT OR IGNORE INTO monthly_done (task_id, ym) SELECT id, ? FROM monthly_tasks WHERE title=?",
               ((today or local_today()).strftime("%Y-%m"), SYNC_TASK))


def tasks(today: date | None = None) -> list[dict]:
    """All monthly tasks with this month's status: done / due (day reached, not done) / upcoming."""
    today = today or local_today()
    ym = today.strftime("%Y-%m")
    rows = db.query_all("SELECT t.*, d.done_at FROM monthly_tasks t LEFT JOIN monthly_done d "
                        "ON d.task_id=t.id AND d.ym=? ORDER BY t.day, t.sort, t.id", (ym,))
    for r in rows:
        r["month_list"] = [int(x) for x in (r.get("months") or "").split(",") if x]
        if r["month_list"] and today.month not in r["month_list"]:
            r["status"] = "off"  # not this month
        else:
            r["status"] = "done" if r["done_at"] else "due" if today.day >= r["day"] else "upcoming"
    return rows


def due_tasks(today: date | None = None) -> list[dict]:
    return [t for t in tasks(today) if t["status"] == "due"]


def due_count(today: date | None = None) -> int:
    """For the (non-password) Overview banner: only a count, never task text."""
    if not db.db_path().exists():
        return 0
    try:
        return len(due_tasks(today))
    except sqlite3.Error:
        return 0


def _ms(months: list[int] | None) -> str | None:
    ms = sorted({m for m in (months or []) if 1 <= m <= 12})
    return ",".join(map(str, ms)) if ms and len(ms) < 12 else None


def add_task(title: str, note: str, day: int, months: list[int] | None = None) -> None:
    db.execute("INSERT INTO monthly_tasks (title, note, day, months, sort) VALUES (?, ?, ?, ?, "
               "(SELECT COALESCE(MAX(sort),0)+1 FROM monthly_tasks))", (title, note or None, day, _ms(months)))


def update_task(tid: int, title: str, note: str, day: int, months: list[int] | None = None) -> None:
    db.execute("UPDATE monthly_tasks SET title=?, note=?, day=?, months=? WHERE id=?",
               (title, note or None, day, _ms(months), tid))


def delete_task(tid: int) -> None:
    db.execute("DELETE FROM monthly_done WHERE task_id=?", (tid,))
    db.execute("DELETE FROM monthly_tasks WHERE id=?", (tid,))


def toggle_done(tid: int, today: date | None = None) -> None:
    ym = (today or local_today()).strftime("%Y-%m")
    with db.connect() as conn:
        if conn.execute("DELETE FROM monthly_done WHERE task_id=? AND ym=?", (tid, ym)).rowcount == 0:
            conn.execute("INSERT INTO monthly_done (task_id, ym) VALUES (?, ?)", (tid, ym))


# ---------------- year insights ----------------

PROPERTIES: tuple[str, ...] = ()  # empty = every expense category named "<X> Home" is a property


def _bucket(cat: str, sub: str) -> str:
    if cat.endswith("Home"):
        return "Property (Home categories)"
    if cat == "Finance & insurance":
        return "Mortgage payment" if "Mortgage" in (sub or "") else "Taxes/finance"
    if cat == "Gifts & social":
        return "Gifts & social"
    return "Daily spending"


def buckets(d0: str, d1: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for g in categories(d0, d1):
        for sub, v in g["subs"]:
            b = _bucket(g["cat"], sub)
            out[b] = out.get(b, 0) + v
    return out


def _owner_split(d0: str, d1: str) -> dict[str, float]:
    rows = db.query_all("SELECT COALESCE(a.owner,'Shared/unassigned') AS owner, t.currency, SUM(t.amount) AS total "
                        "FROM transactions t LEFT JOIN accounts a ON t.account_id=a.id WHERE t.deleted_at IS NULL "
                        "AND t.type='expense' AND t.occurred_at>=? AND t.occurred_at<? GROUP BY owner, t.currency",
                        (d0, d1))
    r = usd_rates()
    out: dict[str, float] = {}
    for x in rows:
        out[x["owner"]] = out.get(x["owner"], 0) + (x["total"] or 0) * r.get(x["currency"], 0)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def properties(d0: str, d1: str) -> list[dict]:
    """Per property: expenses (its 'X Home' category, by sub) vs rent income (income subs naming it)."""
    exp = {g["cat"]: g for g in categories(d0, d1)}
    inc = [(s, v) for g in categories(d0, d1, "income") for s, v in g["subs"]]
    out = []
    names = PROPERTIES or tuple(k[:-4].strip() for k in exp if k.endswith("Home") and k[:-4].strip())
    for p in names:
        g = next((v for k, v in exp.items() if k.lower().startswith(p.lower())), None)
        rent = sum(v for s, v in inc if p.lower() in (s or "").lower())
        cost = g["usd"] if g else 0
        mortgage = sum(v for s, v in (g["subs"] if g else []) if "mortgage" in (s or "").lower())
        if cost or rent:
            out.append({"name": p, "cost": cost, "mortgage": mortgage, "rent": rent, "net": rent - cost,
                        "subs": (g["subs"][:6] if g else [])})
    return out


def year_insights(y: int, today: date | None = None) -> dict:
    today = today or local_today()
    current = y == today.year
    d0, d1 = f"{y}-01-01", (today.isoformat() if current else f"{y + 1}-01-01")
    # same span last year (YTD vs YTD when the year isn't finished)
    l0 = f"{y - 1}-01-01"
    l1 = f"{y - 1}-{d1[5:]}" if current else f"{y}-01-01"
    if current and d1[5:] == "02-29":
        l1 = f"{y - 1}-03-01"
    cur, last = _flows(d0, d1), _flows(l0, l1)
    cats_now = categories(d0, d1)
    cats_last = {g["cat"]: g["usd"] for g in categories(l0, l1)}
    deltas = sorted(({"cat": g["cat"], "now": g["usd"], "last": cats_last.get(g["cat"], 0),
                      "d": g["usd"] - cats_last.get(g["cat"], 0)} for g in cats_now), key=lambda x: -abs(x["d"]))
    for c, v in cats_last.items():
        if c not in {g["cat"] for g in cats_now} and v > 500:
            deltas.append({"cat": c, "now": 0, "last": v, "d": -v})
    months = []
    for m in range(1, 13):
        ym = f"{y}-{m:02d}"
        if current and ym > today.strftime("%Y-%m"):
            break
        f = _flows(*_month_bounds(ym))
        fl = _flows(*_month_bounds(f"{y - 1}-{m:02d}"))
        bd = buckets(*_month_bounds(ym))
        months.append({"month": ym, "income": f["income"], "expense": f["expense"], "last_expense": fl["expense"],
                       "daily": bd.get("Daily spending", 0)})
    b_now, b_last = buckets(d0, d1), buckets(l0, l1)
    n_m = len(months) or 1
    yrs = []
    first = int((stats()["first"] or f"{y}")[:4])
    for yy in range(first, today.year + 1):
        e = f"{yy + 1}-01-01" if yy < today.year else today.isoformat()
        f = _flows(f"{yy}-01-01", e)
        if f["n"]:
            yrs.append({"year": yy, "income": f["income"], "expense": f["expense"], "ytd": yy == today.year,
                        "rate": (f["income"] - f["expense"]) / f["income"] * 100 if f["income"] else None})
    big = transactions(d0=d0, d1=d1, type_="expense", order="amount", limit=10)
    peak = max(months, key=lambda m: m["expense"]) if months else None
    rate = (cur["income"] - cur["expense"]) / cur["income"] * 100 if cur["income"] else None
    last_rate = (last["income"] - last["expense"]) / last["income"] * 100 if last["income"] else None
    props = properties(d0, d1)
    hl = []
    if rate is not None:
        hl.append(f"Savings rate {rate:.0f}%" + (f" (same period last year {last_rate:.0f}%)" if last_rate is not None else "")
                  + f", surplus ${cur['income'] - cur['expense']:,.0f}.")
    dn, dl = b_now.get("Daily spending", 0), b_last.get("Daily spending", 0)
    if dl:
        hl.append(f"Daily spending ${dn:,.0f}; vs. same period last year: {'up' if dn > dl else 'down'} ${abs(dn - dl):,.0f}"
                  f"({(dn - dl) / dl * 100:+.0f}%), monthly avg ${dn / n_m:,.0f}.")
    fixed = sum(b_now.get(k, 0) for k in ("Property (Home categories)", "Mortgage payment"))
    if cur["expense"]:
        hl.append(f"Property + mortgage are {fixed / cur['expense'] * 100:.0f}% of all spending (${fixed:,.0f}).")
    for d in deltas[:2]:
        hl.append(f"{d['cat']} vs. same period last year: {'up' if d['d'] > 0 else 'down'} ${abs(d['d']):,.0f}.")
    for h in props:
        if h["rent"]:
            hl.append(f"{h['name']}: rent ${h['rent']:,.0f} − Expense ${h['cost']:,.0f} = net cash flow ${h['net']:,.0f}.")
    if peak:
        hl.append(f"Highest spending was in month {peak['month'][5:]}  (${peak['expense']:,.0f}).")
    return {"highlights": hl,"year": y, "current": current, "span": (d0, d1), "last_span": (l0, l1), "flows": cur, "last": last,
            "rate": (cur["income"] - cur["expense"]) / cur["income"] * 100 if cur["income"] else None,
            "last_rate": (last["income"] - last["expense"]) / last["income"] * 100 if last["income"] else None,
            "deltas": deltas[:8], "months": months, "buckets": [(k, b_now.get(k, 0), b_last.get(k, 0)) for k in
                                                               ("Property (Home categories)", "Mortgage payment", "Taxes/finance", "Gifts & social", "Daily spending")],
            "daily_avg": b_now.get("Daily spending", 0) / n_m, "properties": properties(d0, d1),
            "owners": _owner_split(d0, d1), "years": yrs, "big": big, "peak": peak,
            "cats": cats_now, "income_cats": categories(d0, d1, "income")}
