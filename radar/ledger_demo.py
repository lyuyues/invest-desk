"""Demo data for the ledger panel: a fictional two-person household with ~2 years of transactions, monthly to-dos,
FI snapshots and goals, and one sample month report — so every ledger page has something to show.

`python -m radar ledger_demo` (or the button on an empty ledger). Refuses to touch a ledger that already has data.
Everything here is made up (seeded random numbers, invented account names)."""
from __future__ import annotations

import random
from datetime import date, timedelta

from radar import fi, ledger, ledger_csv, ledger_report
from radar.bookkeeping import db

ME, PARTNER = ledger.ME_OWNER, ledger.PARTNER_OWNER
# name, type, currency, owner
ACCOUNTS = {
    "Chase Checking": ("checking", "USD", ME),
    "Ally Savings": ("savings", "USD", None),
    "Amex Gold": ("credit_card", "USD", ME),
    "Citi Double Cash": ("credit_card", "USD", PARTNER),
    "TD Chequing": ("checking", "CAD", PARTNER),
    "Fidelity Brokerage": ("investment", "USD", ME),
    "Vanguard 401k": ("investment", "USD", PARTNER),
}
MEMBERS = ("Alex", "Sam")


def _months(today: date, n: int) -> list[date]:
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(date(y, m, 1))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return list(reversed(out))


def transactions(today: date, months: int = 26, seed: int = 7) -> list[dict]:
    rnd = random.Random(seed)
    rows: list[dict] = []

    def add(d: date, type_: str, amount: float, account: str, cat: str = "", sub: str = "", note: str = "",
            member: str = ""):
        if d > today:
            return
        t, ccy, owner = ACCOUNTS[account]
        rows.append({"type": type_, "occurred_at": f"{d.isoformat()} {rnd.randint(8, 21):02d}:{rnd.randint(0, 59):02d}:00",
                     "amount": round(amount, 2), "currency": ccy, "account": account, "category": cat,
                     "subcategory": sub, "member": member, "note": note, "account_type": t, "owner": owner})

    def transfer(d: date, amount: float, src: str, dst: str, note: str = ""):
        add(d, "transfer_out", amount, src, note=note)
        add(d, "transfer_in", amount, dst, note=note)

    def day(m0: date, lo: int = 1, hi: int = 28) -> date:
        return m0 + timedelta(days=rnd.randint(lo, hi) - 1)

    ms = _months(today, months)
    start = ms[0]
    for acc, amt in [("Chase Checking", 18500), ("Ally Savings", 32000), ("TD Chequing", 6200),
                     ("Fidelity Brokerage", 186000), ("Vanguard 401k", 243000)]:
        add(start, "balance_adjust", amt, acc, note="Opening balance")
    for i, m0 in enumerate(ms):
        card_spend = {"Amex Gold": 0.0, "Citi Double Cash": 0.0}

        def spend(card: str, cat: str, sub: str, lo: float, hi: float, n: int, note: str = "", who: str = ""):
            for _ in range(n):
                a = rnd.uniform(lo, hi)
                add(day(m0), "expense", a, card, cat, sub, note, who)
                if card in card_spend:
                    card_spend[card] += a

        # income
        for d in (15, 28):
            add(m0 + timedelta(days=d - 1), "income", 3920 + (i % 3) * 15, "Chase Checking", "Employment income", "Salary", "Payroll", "Alex")
            add(m0 + timedelta(days=d - 1), "income", 3380, "Chase Checking", "Employment income", "Salary", "Payroll", "Sam")
        add(m0 + timedelta(days=4), "income", rnd.uniform(1800, 2600), "TD Chequing", "Employment income", "Side income", "Design contract", "Sam")
        add(m0 + timedelta(days=2), "income", 2350, "Chase Checking", "Rental income", "Oak Ave rental", "Tenant rent")
        if m0.month in (3, 6, 9, 12):
            add(day(m0, 20, 28), "income", rnd.uniform(380, 520), "Fidelity Brokerage", "Investment income", "Dividends", "Quarterly dividends")
        if m0.month == 3:
            add(day(m0, 10, 20), "income", 6500, "Chase Checking", "Employment income", "Bonus", "Annual bonus", "Alex")
        # home + rental property
        add(m0, "expense", 2860, "Chase Checking", "Maple St Home", "Maple St - Mortgage", "Mortgage autopay")
        add(m0 + timedelta(days=1), "expense", 1640, "Chase Checking", "Oak Ave Home", "Oak Ave - Mortgage", "Mortgage autopay")
        add(m0 + timedelta(days=9), "expense", 118, "Chase Checking", "Oak Ave Home", "Oak Ave - Insurance", "Landlord policy")
        if m0.month in (4, 10):
            add(m0 + timedelta(days=14), "expense", 4150, "Chase Checking", "Maple St Home", "Maple St - Property tax", "County tax")
            add(m0 + timedelta(days=14), "expense", 1980, "Chase Checking", "Oak Ave Home", "Oak Ave - Property tax", "County tax")
        if rnd.random() < 0.35:
            add(day(m0), "expense", rnd.uniform(120, 900), "Chase Checking", "Oak Ave Home", "Oak Ave - Maintenance",
                rnd.choice(["Plumber", "Furnace service", "Gutter cleaning", "Appliance repair"]))
        if i == months - 5:
            add(day(m0, 5, 12), "expense", 6800, "Chase Checking", "Maple St Home", "Maple St - Remodel", "Kitchen countertops")
        # daily life
        spend("Amex Gold", "Food & drink", "Groceries", 45, 190, 8, "Trader Joe's", "Alex")
        spend("Amex Gold", "Food & drink", "Dining out", 22, 95, 6, "", "Sam")
        spend("Citi Double Cash", "Food & drink", "Coffee", 5, 9, 10, "Coffee")
        spend("Citi Double Cash", "Transportation", "Gas", 38, 62, 4, "Gas", "Sam")
        add(m0 + timedelta(days=11), "expense", 152, "Chase Checking", "Transportation", "Car insurance", "Auto insurance")
        add(m0 + timedelta(days=17), "expense", rnd.uniform(160, 290 if m0.month in (12, 1, 2, 7, 8) else 210),
            "Chase Checking", "Home & utilities", "Utilities", "Utilities")
        add(m0 + timedelta(days=20), "expense", 135, "Chase Checking", "Home & utilities", "Internet & phone", "Internet + phones")
        spend("Citi Double Cash", "Shopping", rnd.choice(["Clothing", "Household goods"]), 30, 220, 3, "", "Sam")
        spend("Amex Gold", "Leisure", "Movies & shows", 18, 80, 1, "Tickets", "Alex")
        add(m0 + timedelta(days=6), "expense", 86, "Chase Checking", "Finance & insurance", "Life insurance", "Term life")
        if rnd.random() < 0.3:
            spend("Amex Gold", "Health", "Doctor & pharmacy", 25, 240, 1, "Pharmacy")
        if m0.month in (2, 5, 9, 12):
            spend("Amex Gold", "Gifts & social", rnd.choice(["Gifts", "Treating friends"]), 80, 420, 1, "Gift")
        if m0.month in (7, 12):
            spend("Amex Gold", "Leisure", "Travel", 1400, 2600, 1, "Flights + hotel")
        add(day(m0, 1, 28), "expense", rnd.uniform(60, 160), "TD Chequing", "Food & drink", "Groceries", "Market", "Sam")
        # money moves: pay the cards, invest, save
        for card, amt in card_spend.items():
            transfer(m0 + timedelta(days=27), amt, "Chase Checking", card, "Card payment")
        transfer(m0 + timedelta(days=16), 1500, "Chase Checking", "Fidelity Brokerage", "Monthly investing")
        add(m0 + timedelta(days=15), "transfer_in", 950, "Vanguard 401k", note="401k contribution")
        transfer(m0 + timedelta(days=22), 800, "Chase Checking", "Ally Savings", "Emergency fund")
        # quarter-end market value updates
        if m0.month in (3, 6, 9, 12):
            add(m0 + timedelta(days=27), "balance_adjust", rnd.uniform(-4000, 9000), "Fidelity Brokerage", note="Market value")
            add(m0 + timedelta(days=27), "balance_adjust", rnd.uniform(-5000, 11000), "Vanguard 401k", note="Market value")
    return rows


REPORT = """## One-liner
{ym} income ${inc:,.0f}, expenses ${exp:,.0f}, surplus ${net:,.0f}, savings rate {rate:.0f}%. The two homes are the largest fixed cost; daily spending is about the same as last month.

## Income and spending
- Income ${inc:,.0f}: two salaries plus the Oak Ave rent; Sam’s side income goes to the CAD account.
- Expenses ${exp:,.0f}; 12-month average ${avg:,.0f}.
- Savings rate {rate:.0f}%; the usual monthly transfers to investments and the emergency fund.

## Spending breakdown
- The largest items are Maple St Home (mortgage $2,860) and Oak Ave Home (mortgage + insurance).
- Food & drink is the largest variable cost; groceries outweigh dining out.
- Utilities follow the seasons, higher in winter and summer.

## Large and unusual items
- No obvious duplicate entries.
- Oak Ave maintenance was higher than usual; worth confirming it was a one-off.

## Next month
- Pay both credit cards in full and on time to avoid interest.
- If property tax is due next quarter, set the money aside in savings now.
- Update the FI asset snapshot at the start of the month."""
CHECKS = ["Is the Oak Ave repair a one-off, or should we budget for it every year?", "Should the side income in TD Chequing be converted to USD and moved to savings?"]


def load(today: date | None = None) -> dict:
    ledger.ensure()
    if not ledger.is_empty():
        raise ValueError("ledger already has transactions; demo data is only loaded into an empty ledger")
    today = today or ledger.local_today()
    rows = transactions(today)
    res = ledger_csv.insert_rows(rows, "demo-household.csv", source="demo")
    with db.connect() as conn:
        for m in MEMBERS:
            conn.execute("INSERT OR IGNORE INTO members (name) VALUES (?)", (m,))
    # to-dos
    ledger.add_task("Pay both credit cards", "Amex Gold + Citi Double Cash in full", 5)
    ledger.add_task("Oak Ave property tax", "Property tax on both homes is due twice a year", 10, [4, 10])
    # FI: settings, monthly snapshots for the last 8 months, my own goal
    cfg = fi.settings()
    cfg["mortgage_balance"] = 412000.0
    fi.save_settings(cfg)
    rnd = random.Random(11)
    fid, van, ally, cash = 186000.0, 243000.0, 32000.0, 12000.0
    for m0 in _months(today, 8):
        fid, van = fid * (1 + rnd.uniform(-0.02, 0.035)) + 1500, van * (1 + rnd.uniform(-0.02, 0.035)) + 950
        ally, cash = ally + 800, cash + rnd.uniform(-1500, 2000)
        fi.save_snapshot(m0.strftime("%Y-%m"), [
            {"item": "Fidelity Brokerage", "owner": ledger.ME, "currency": "USD", "amount": round(fid)},
            {"item": "Vanguard 401k", "owner": "Household", "currency": "USD", "amount": round(van)},
            {"item": "Ally Savings", "owner": "Household", "currency": "USD", "amount": round(ally)},
            {"item": "Chase Checking", "owner": "Household", "currency": "USD", "amount": round(cash)}])
    goals = fi.goal_rows()
    deadline = f"{today.year + 6}-12"
    if goals:
        fi.save_goal(goals[0]["id"], goals[0]["name"], 400000, None, deadline, ["Fidelity Brokerage"])
    # one sample month report (written here, no Claude needed) with check items
    ledger_report.ensure()
    ym = (_months(today, 2)[0]).strftime("%Y-%m")
    f = ledger._flows(*ledger._month_bounds(ym))
    past = [s["expense"] for s in ledger.monthly(13, ym)[:-1] if s["expense"]]
    md = REPORT.format(ym=ym, inc=f["income"], exp=f["expense"], net=f["income"] - f["expense"],
                       rate=(f["income"] - f["expense"]) / f["income"] * 100 if f["income"] else 0,
                       avg=sum(past) / len(past) if past else 0)
    rid = db.execute("INSERT INTO ledger_reports (ym, created, markdown, ok, data_until, bundle) VALUES (?,?,?,?,?,?)",
                     (ym, f"{today.isoformat()}T05:00:00+00:00", md, 1, today.isoformat(), ledger_report.build_bundle(ym)))
    for i, text in enumerate(["Overall note"] + CHECKS):
        db.execute("INSERT INTO ledger_checks (report_id, idx, text) VALUES (?,?,?)", (rid, i, text))
    return {"transactions": res["inserted"], "report": rid}
