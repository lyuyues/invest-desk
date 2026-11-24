"""FI tracker: target from the ledger's spending, progress from monthly asset snapshots.

Target = (annual spending in FI − passive income) / withdrawal rate. Spending comes from the
last 12 full months of the ledger, split into buckets (one-off remodel / repaying people excluded); every bucket can
be overridden. Mortgages are kept in (not being prepaid), with a 'after payoff' number alongside.
Educational projection only — not financial advice."""
from __future__ import annotations

import json
import math

from radar import ledger
from radar.bookkeeping import db

DEFAULTS = {"wr": 0.04, "real_return": 0.05, "health": 20000.0,
            "override": {},             # bucket → annual USD
            "savings_override": None,   # household monthly savings
            "mortgage_balance": None}   # remaining principal of all mortgages (USD); None = not entered yet
BUCKETS = [("daily", "Daily spending"), ("property", "Property upkeep (tax/insurance/repairs/HOA/utilities)"), ("mortgage", "Mortgage"),
           ("social_tax", "Gifts & social / taxes"), ("health", "Health insurance (self-paid after retiring, estimate)")]


def settings() -> dict:
    raw = ledger._setting("fi_cfg")
    cfg = {**DEFAULTS, **(json.loads(raw) if raw else {})}
    return cfg


def save_settings(cfg: dict) -> None:
    ledger._put("fi_cfg", json.dumps({k: cfg[k] for k in DEFAULTS}, ensure_ascii=False))


def _classify(cat: str, sub: str) -> str | None:
    s = (sub or "").lower()
    if "remodel" in s or "remodel" in s or "repay" in s or "loan" in s:
        return None  # one-off: renovation, repaying/lending money to people
    if "mortgage" in s or "Mortgage" in s:
        return "mortgage"
    if cat.endswith("Home"):
        return "property"
    if cat in ("Gifts & social", "Finance & insurance"):
        return "social_tax"
    return "daily"


def trailing_window() -> tuple[str, str]:
    """Last 12 full months before the current (local) month."""
    t = ledger.local_today()
    end = f"{t.year:04d}-{t.month:02d}-01"
    return f"{t.year - 1:04d}-{t.month:02d}-01", end


def auto_spending() -> dict:
    d0, d1 = trailing_window()
    out = {k: 0.0 for k, _ in BUCKETS}
    oneoff = 0.0
    for g in ledger.categories(d0, d1):
        for sub, v in g["subs"]:
            b = _classify(g["cat"], sub)
            if b is None:
                oneoff += v
            else:
                out[b] += v
    rent = sum(v for g in ledger.categories(d0, d1, "income") for s, v in g["subs"]
               if g["cat"] == "Rental income" or "rental" in (s or "").lower())
    f = ledger._flows(d0, d1)
    return {"buckets": out, "oneoff": oneoff, "rent": rent, "window": (d0, d1),
            "savings_monthly": (f["income"] - f["expense"]) / 12}


def ledger_assets(max_age_days: int = 730) -> list[dict]:
    """Investment accounts tracked in the bookkeeping app (market value kept up to date with balance adjustments) → snapshot rows."""
    from datetime import timedelta
    cutoff = (ledger.local_today() - timedelta(days=max_age_days)).isoformat()
    out = []
    for a in ledger.balances():
        if a["type"] != "investment" or abs(a["balance"] or 0) < 1 or (a["last"] or "") < cutoff:
            continue
        out.append({"item": a["name"], "owner": ledger.ME if a["owner"] == ledger.ME_OWNER else "Household", "currency": a["currency"],
                    "amount": round(a["balance"], 2), "last": (a["last"] or "")[:10]})
    return sorted(out, key=lambda r: (r["owner"] != ledger.ME, -r["amount"]))


def auto_yue_monthly() -> float:
    """Average net money moved into my own (ME_OWNER) investment accounts per month, last 12 full months."""
    d0, d1 = trailing_window()
    rows = db.query_all("SELECT t.type, t.currency, SUM(t.amount) AS total FROM transactions t JOIN accounts a "
                        "ON a.id=t.account_id WHERE a.type='investment' AND a.owner=? AND t.deleted_at IS NULL "
                        "AND t.type IN ('transfer_in','transfer_out') AND t.occurred_at>=? AND t.occurred_at<? "
                        "GROUP BY t.type, t.currency", (ledger.ME_OWNER, d0, d1))
    r = ledger.usd_rates()
    net = sum((x["total"] or 0) * r.get(x["currency"], 0) * (1 if x["type"] == "transfer_in" else -1) for x in rows)
    return max(net, 0) / 12


# ---------------- snapshots ----------------

def snapshot_months() -> list[str]:
    return [r["ym"] for r in db.query_all("SELECT DISTINCT ym FROM fi_snapshots ORDER BY ym")]


def items(ym: str) -> list[dict]:
    return db.query_all("SELECT * FROM fi_snapshots WHERE ym=? ORDER BY owner DESC, item", (ym,))


def save_snapshot(ym: str, rows: list[dict]) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM fi_snapshots WHERE ym=?", (ym,))
        for r in rows:
            conn.execute("INSERT INTO fi_snapshots (ym, item, owner, currency, amount) VALUES (?,?,?,?,?)",
                         (ym, r["item"], r["owner"], r["currency"], r["amount"]))


def totals_by_month() -> list[dict]:
    rates = ledger.usd_rates()
    out = []
    for ym in snapshot_months():
        rows = items(ym)
        usd = [(r, r["amount"] * rates.get(r["currency"], 0)) for r in rows]
        out.append({"ym": ym, "household": sum(v for _, v in usd), "mine": sum(v for r, v in usd if r["owner"] == ledger.ME),
                    "by_item": {r["item"]: v for r, v in usd}, "owner": {r["item"]: r["owner"] for r, _ in usd}})
    return out


# ---------------- my own goals ----------------

def seed_goals() -> None:
    if not ledger._setting("fi_goals_seeded"):
        db.execute("INSERT INTO fi_goals (name, sort) VALUES (?, 0)", (f"Brokerage account ({ledger.ME})",))
        ledger._put("fi_goals_seeded", "1")


def goal_rows() -> list[dict]:
    return db.query_all("SELECT * FROM fi_goals ORDER BY sort, id")


def save_goal(gid: int | None, name: str, target: float | None, monthly: float | None, deadline: str | None,
              items: list[str]) -> None:
    vals = (name, target, monthly, deadline or None, ",".join(items) or None)
    if gid:
        db.execute("UPDATE fi_goals SET name=?, target=?, monthly=?, deadline=?, items=? WHERE id=?", (*vals, gid))
    else:
        db.execute("INSERT INTO fi_goals (name, target, monthly, deadline, items, sort) VALUES (?,?,?,?,?,"
                   "(SELECT COALESCE(MAX(sort),0)+1 FROM fi_goals))", vals)


def delete_goal(gid: int) -> None:
    db.execute("DELETE FROM fi_goals WHERE id=?", (gid,))


def required_monthly(target: float, start: float, months: int, real_return: float) -> float:
    """Monthly contribution needed to reach target in `months`."""
    if months <= 0:
        return max(target - start, 0)
    g = (1 + real_return) ** (1 / 12)
    fv_start = start * g ** months
    if fv_start >= target:
        return 0.0
    return (target - fv_start) * (g - 1) / (g ** months - 1) if g != 1 else (target - fv_start) / months


def goals(hist: list[dict], rr: float) -> list[dict]:
    cur = hist[-1] if hist else None
    prev = hist[-2] if len(hist) > 1 else None
    auto_m = auto_yue_monthly()
    today = ledger.local_today()
    out = []
    for g in goal_rows():
        names = [x for x in (g["items"] or "").split(",") if x]

        def amount(h):
            if not h:
                return 0.0
            return sum(v for k, v in h["by_item"].items() if (k in names if names else h["owner"][k] == ledger.ME))
        a = amount(cur)
        monthly = g["monthly"] if g["monthly"] is not None else auto_m
        left = None
        if g["deadline"]:
            y, m = map(int, g["deadline"].split("-"))
            left = (y - today.year) * 12 + (m - today.month)
        out.append({**g, "names": names, "current": a, "delta": a - amount(prev) if prev else None,
                    "monthly_eff": monthly, "monthly_auto": g["monthly"] is None,
                    "progress": a / g["target"] * 100 if g["target"] else None,
                    "months": months_to(g["target"], a, monthly, rr) if g["target"] else None,
                    "months_left": left,
                    "need_monthly": required_monthly(g["target"], a, left, rr) if g["target"] and left is not None else None})
    return out


# ---------------- math ----------------

def months_to(target: float, start: float, monthly: float, real_return: float, cap: int = 12 * 60) -> int | None:
    if start >= target:
        return 0
    g = (1 + real_return) ** (1 / 12)
    a = start
    for m in range(1, cap + 1):
        a = a * g + monthly
        if a >= target:
            return m
    return None


def project(start: float, monthly: float, real_return: float, months: int) -> list[float]:
    g = (1 + real_return) ** (1 / 12)
    out, a = [], start
    for _ in range(months):
        a = a * g + monthly
        out.append(a)
    return out


def view() -> dict:
    cfg = settings()
    auto = auto_spending()
    b = {k: auto["buckets"].get(k, 0.0) for k, _ in BUCKETS}
    b["health"] = cfg["health"]
    for k, v in (cfg["override"] or {}).items():
        if k in b and v is not None:
            b[k] = float(v)
    spend = sum(b.values())
    need = max(spend - auto["rent"], 0)                    # cash need per year today (incl. mortgage)
    need_paid = max(spend - b["mortgage"] - auto["rent"], 0)  # forever part (mortgages end)
    target_paid = need_paid / cfg["wr"]
    bal = cfg.get("mortgage_balance")
    # Mortgages are finite: at a rate below the 4% withdrawal rate, covering the remaining payments costs about
    # the remaining principal — so add the balance instead of capitalising the payments forever.
    target = target_paid + (bal or 0)
    hist = totals_by_month()
    cur = hist[-1] if hist else None
    assets = cur["household"] if cur else 0.0
    monthly = cfg["savings_override"] if cfg["savings_override"] is not None else max(auto["savings_monthly"], 0)
    rr = cfg["real_return"]
    m_fi = months_to(target, assets, monthly, rr)
    m_paid = months_to(target_paid, assets, monthly, rr)
    lean = max(b["daily"] + b["property"] + b["health"] - auto["rent"], 0) / cfg["wr"]
    ms = [("6-month emergency fund", need / 2), ("25%", target * .25), ("Lean FI (daily + property upkeep + health only)", lean),
          ("50%", target * .5), ("75%", target * .75), ("Financial independence", target)]
    milestones = [{"name": n, "amount": a, "done": assets >= a, "months": months_to(a, assets, monthly, rr)}
                  for n, a in sorted(ms, key=lambda x: x[1])]
    prev = hist[-2] if len(hist) > 1 else None
    return {"cfg": cfg, "auto": auto, "mortgage_balance": bal, "buckets": b, "labels": dict(BUCKETS), "spend": spend, "need": need,
            "need_paid": need_paid, "target": target, "target_paid": target_paid, "assets": assets,
            "progress": assets / target * 100 if target else None, "runway": assets / (need / 12) if need else None,
            "monthly": monthly, "months_fi": m_fi, "months_paid": m_paid, "milestones": milestones,
            "next": next((m for m in milestones if not m["done"]), None), "hist": hist,
            "delta": (assets - prev["household"]) if prev else None, "cur_ym": cur["ym"] if cur else None,
            "sensitivity": [(r, months_to(target, assets, monthly, r)) for r in (0.02, 0.03, 0.04, 0.05, 0.06)],
            "goals": goals(hist, rr), "item_names": sorted(cur["by_item"]) if cur else [],
            "savings_auto": cfg["savings_override"] is None,
            "proj": project(assets, monthly, rr, min(m_fi or 360, 360))}


def report_section() -> str:
    """Short FI block for the month-end report bundle."""
    if not snapshot_months():
        return ""
    v = view()
    yrs = f"{v['months_fi'] / 12:.1f} years" if v["months_fi"] is not None else "not within 60 years"
    L = ["", "## FI progress (4% withdrawal rate, real return {:.0%})".format(v["cfg"]["real_return"]),
         f"- Target ${v['target']:,.0f} = perpetual part ${v['target_paid']:,.0f} (yearly net spending excluding mortgages ${v['need_paid']:,.0f} ÷ 4%)"
         f" + remaining mortgage principal {'$' + format(v['mortgage_balance'], ',.0f') if v['mortgage_balance'] else '(not entered)'}",
         f"- Household investable assets ${v['assets']:,.0f} ({v['cur_ym']}), progress {v['progress']:.1f}%, "
         f"vs last time {'+' if (v['delta'] or 0) >= 0 else ''}${(v['delta'] or 0):,.0f}; lasts {v['runway']:.0f} months",
         f"- Saving ${v['monthly']:,.0f} per month, time to go: {yrs}",
         ]
    for g in v["goals"]:
        if g["target"]:
            L.append(f"- {ledger.ME} — goal “{g['name']}”:${g['current']:,.0f} / ${g['target']:,.0f} ({g['progress']:.1f}%)"
                     + (f", {g['deadline']} needs monthly ${g['need_monthly']:,.0f}" if g["need_monthly"] is not None else ""))
    if v["next"]:
        L.append(f"- Next milestone: {v['next']['name']} ${v['next']['amount']:,.0f}")
    return "\n".join(L)
