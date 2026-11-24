"""Generic CSV import for the household ledger (any bank or bookkeeping app export, after renaming the columns).

Columns (header row required, English names, order free):
  date         YYYY-MM-DD or YYYY-MM-DD HH:MM[:SS]                        required
  amount       number; without a `type` column, negative = expense and positive = income   required
  account      account name, e.g. "Chase Checking"                          required
  currency     USD / CAD / CNY / EUR / HKD (default USD)
  type         expense | income | transfer_in | transfer_out | balance_adjust   (optional)
  category     top-level category, e.g. "Food"
  subcategory  e.g. "Groceries"
  member       household member the item is for
  note         free text
  account_type checking | savings | credit_card | investment (optional, used when the account is new)
  owner        account owner (optional, used when the account is new)

Idempotent: every row gets a hash of (account, date, amount, type, note, n-th duplicate), so uploading the same or
an overlapping export twice only adds the new rows. Rows are never deleted by an import.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from radar.bookkeeping import db

TYPES = {"expense", "income", "transfer_in", "transfer_out", "balance_adjust", "debt_change", "credit_change"}
CURRENCIES = {"USD", "CAD", "CNY", "EUR", "HKD"}
SOURCE = "csv"


def _date(s: str) -> str:
    s = (s or "").strip().replace("/", "-").replace("T", " ")
    if len(s) == 10:
        s += " 00:00:00"
    elif len(s) == 16:
        s += ":00"
    if len(s) != 19 or s[4] != "-" or s[7] != "-":
        raise ValueError(f"bad date {s!r}")
    return s


def parse(path: Path) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = {"date", "amount", "account"} - {(h or "").strip().lower() for h in reader.fieldnames or []}
        if missing:
            raise ValueError("missing column(s): " + ", ".join(sorted(missing)))
        for i, raw in enumerate(reader, 2):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            try:
                amount = float(r["amount"].replace(",", "").replace("$", ""))
                type_ = r.get("type", "").lower()
                if type_ and type_ not in TYPES:
                    raise ValueError(f"unknown type {type_!r}")
                if not type_:
                    type_ = "expense" if amount < 0 else "income"
                    amount = abs(amount)
                ccy = (r.get("currency") or "USD").upper()
                if ccy not in CURRENCIES:
                    raise ValueError(f"unsupported currency {ccy!r}")
                if not r["account"]:
                    raise ValueError("empty account")
                rows.append({"type": type_, "occurred_at": _date(r["date"]), "amount": amount, "currency": ccy,
                             "account": r["account"], "category": r.get("category", ""),
                             "subcategory": r.get("subcategory", ""), "member": r.get("member", ""),
                             "note": r.get("note", ""), "account_type": r.get("account_type", "") or "unknown",
                             "owner": r.get("owner", "") or None})
            except (ValueError, KeyError) as e:
                errors.append(f"line {i}: {e}")
    return rows, errors


def _id(conn, sql_sel: str, args: tuple, sql_ins: str, ins_args: tuple) -> int:
    row = conn.execute(sql_sel, args).fetchone()
    return row["id"] if row else conn.execute(sql_ins, ins_args).lastrowid


def _category(conn, cat: str, sub: str) -> int | None:
    if not cat:
        return None
    pid = _id(conn, "SELECT id FROM categories WHERE name=? AND parent_id IS NULL", (cat,),
              "INSERT INTO categories (name, parent_id) VALUES (?, NULL)", (cat,))
    if not sub:
        return pid
    return _id(conn, "SELECT id FROM categories WHERE name=? AND parent_id=?", (sub, pid),
               "INSERT INTO categories (name, parent_id) VALUES (?, ?)", (sub, pid))


def insert_rows(rows: list[dict], filename: str, source: str = SOURCE) -> dict:
    """Write parsed rows (see parse) into the ledger; returns counts like the sync page expects."""
    inserted = skipped = 0
    seen: dict[str, int] = {}
    with db.connect() as conn:
        imp = conn.execute("INSERT INTO imports (source_type, filename, status) VALUES (?, ?, 'running')",
                           (source, filename)).lastrowid
        for r in rows:
            key = json.dumps([r["account"], r["occurred_at"], round(r["amount"], 2), r["type"], r["note"]])
            seen[key] = seen.get(key, 0) + 1
            h = hashlib.sha256(f"{key}#{seen[key]}".encode()).hexdigest()[:32]
            if conn.execute("SELECT 1 FROM transactions WHERE hash=?", (h,)).fetchone():
                skipped += 1
                continue
            acc = conn.execute("SELECT id FROM accounts WHERE name=?", (r["account"],)).fetchone()
            acc_id = acc["id"] if acc else conn.execute(
                "INSERT INTO accounts (name, type, currency, owner) VALUES (?, ?, ?, ?)",
                (r["account"], r["account_type"], r["currency"], r["owner"])).lastrowid
            mem = None
            if r["member"]:
                mem = _id(conn, "SELECT id FROM members WHERE name=?", (r["member"],),
                          "INSERT INTO members (name) VALUES (?)", (r["member"],))
            conn.execute("INSERT INTO transactions (type, occurred_at, amount, currency, account_id, category_id, "
                         "member_id, note, source, import_id, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (r["type"], r["occurred_at"], r["amount"], r["currency"], acc_id,
                          _category(conn, r["category"], r["subcategory"]), mem, r["note"] or None, source, imp, h))
            inserted += 1
        conn.execute("UPDATE imports SET rows_total=?, rows_inserted=?, rows_soft_deleted=0, rows_skipped=?, "
                     "status='success' WHERE id=?", (len(rows), inserted, skipped, imp))
    return {"total": len(rows), "inserted": inserted, "deleted": 0, "skipped": skipped, "updated": 0}


def import_csv(path: Path, filename: str) -> dict:
    rows, errors = parse(path)
    out = insert_rows(rows, filename)
    out["errors"] = errors[:10]
    return out
