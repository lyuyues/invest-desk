"""Right-hand assistant on the ledger pages (only when the ledger is unlocked).

Context = ledger summary + the report / check items on screen. When the user answers a check item,
Claude appends a ```reply block; the server saves it into that item's reply box."""
from __future__ import annotations

import re
from datetime import date

from radar import ledger, ledger_report

INSTRUCTIONS = """# Ledger assistant instructions
You are on the user’s Ledger page (household ledger, imported from a bookkeeping app). Below are the ledger summary and what the user is looking at.
- Answer only from this data; if something isn’t in it (e.g. a specific transaction), say you can’t see it and suggest searching on the “Transactions” page.
- If the user is **answering/explaining a check item** (or asks you to note down a reply), end your reply with a code block tagged reply, one line per item:
```reply
number|Status|reply text
```
  number = the number in front of the check item (0 = overall note); status = done (handled/explained), ignore (no need), open (needs follow-up).
  Rewrite the reply briefly in the user’s words, keeping the key facts (payee, purpose, amount). The system saves it to that item and carries it into next month’s report.
- If the user only asks a question and gives no answer, don’t output a reply block."""

_REPLY_RE = re.compile(r"```reply[^\n]*\n(.*?)```", re.S)


def target_report(page: str) -> int | None:
    m = re.match(r"^/ledger/reports/(\d+)", page)
    if m:
        return int(m.group(1))
    rs = ledger_report.reports()
    return rs[0]["id"] if rs else None


def _month_lines(ym: str) -> list[str]:
    v = ledger.month_view(ym)
    f = v["flows"]
    L = [f"## {ym}: income ${f['income']:,.0f}, expenses ${f['expense']:,.0f}, surplus ${f['income'] - f['expense']:,.0f}"]
    L += [f"- {g['cat']} ${g['usd']:,.0f} (last month ${g['prev']:,.0f}): " +
          ", ".join(f"{s or '—'} ${x:,.0f}" for s, x in g["subs"][:5]) for g in v["cats"][:15]]
    L.append("Large items: " + "; ".join(f"{t['occurred_at'][:10]} {t['cat']}/{t['sub'] or ''} {t['note'] or ''} "
                                 f"{t['currency']} {t['amount']:,.0f}" for t in v["big"]))
    return L


def context(page: str) -> str:
    page = (page or "/ledger").split("?")[0]
    st = ledger.stats()
    today = ledger.local_today()
    ym = min(today.strftime("%Y-%m"), (st["last"] or today.isoformat())[:7])
    L = [INSTRUCTIONS, "", f"# Ledger overview\nIn total {st['n']:,}  transactions, {(st['first'] or '')[:10]} to {(st['last'] or '')[:10]}; "
         f"last import {(st['last_sync'] or {}).get('imported_at', '—')} (amounts in USD)", "## Last 13 months"]
    L += [f"- {s['month']} Income ${s['income']:,.0f} Expense ${s['expense']:,.0f}" for s in ledger.monthly(13, ym)]
    L += _month_lines(ym)
    y = ledger.year_view(today.year)
    L.append(f"## {today.year}  year to date: income ${y['flows']['income']:,.0f}, expenses ${y['flows']['expense']:,.0f}")
    L += [f"- {g['cat']} ${g['usd']:,.0f}: " + ", ".join(f"{s or '—'} ${x:,.0f}" for s, x in g["subs"][:6])
          for g in y["cats"][:15]]
    rid = target_report(page)
    if rid and (r := ledger_report.get(rid)):
        where = "The user is viewing this report" if page.startswith("/ledger/reports/") else "Most recent report (the user is not on the report page)"
        L += ["", f"# {where}: {r['ym']} (id {rid})", r["markdown"][:12000], "", "## Check items of this report (number|Status|user’s reply)"]
        for c in ledger_report.checks(rid):
            L.append(f"{c['idx']}|{c['status']}|{c['text']}" + (f" → replied: {c['reply']}" if c["reply"] else ""))
    L.append(f"\n# Current page: {page}")
    return "\n".join(L)


def apply_replies(text: str, rid: int | None) -> list[int]:
    """Save ```reply lines into the report's check items. Returns the item numbers saved."""
    if not rid:
        return []
    by_idx = {c["idx"]: c for c in ledger_report.checks(rid)}
    saved = []
    for block in _REPLY_RE.findall(text):
        for ln in block.splitlines():
            parts = [p.strip() for p in ln.split("|", 2)]
            if len(parts) != 3 or not parts[0].isdigit() or int(parts[0]) not in by_idx or not parts[2]:
                continue
            c = by_idx[int(parts[0])]
            reply = parts[2] if not c["reply"] else f"{c['reply']}\n{parts[2]}"
            ledger_report.save_check(c["id"], reply, parts[1] if parts[1] in ledger_report.STATUS_ZH else c["status"])
            saved.append(int(parts[0]))
    return saved
