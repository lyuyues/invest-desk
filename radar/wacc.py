"""Per-company WACC for the DCF (discount rate for free cash flow to the firm).

  cost of equity  Re = rf + β × ERP                (CAPM; rf = latest 10Y Treasury DGS10 from FRED;
                                                    β = Blume-adjusted Yahoo 5y beta: 0.67 × raw + 0.33)
  cost of debt    Rd = interest expense / total debt (latest annual report), clamped to [rf, rf + 8%]
  tax rate        t  = effective tax rate (latest), clamped to [0, 35%], default 21%
  weights         E = price × shares (market value), D = total debt (book)
  WACC = E/(D+E)·Re + D/(D+E)·Rd·(1 − t)

Banks / insurers: debt is part of operations, so the firm-value approach does not fit — they use Re only and
no net-debt bridge. Educational estimate, not advice; every piece is shown on the page and the discount rate
stays editable.
"""
from __future__ import annotations

import math

ERP = 0.05            # equity risk premium (US, long-run consensus range ~4.5–5.5%)
RF_DEFAULT = 0.043    # used only when no DGS10 observation is stored
BETA_DEFAULT = 1.0
FINANCIAL_SECTORS = ("BANK", "INSURANCE")
FINANCIAL_INDUSTRIES = ("Banks", "Insurance")


def _last(rows: dict, key: str) -> tuple[float | None, int | None]:
    vals = rows.get(key) or []
    for i in range(len(vals) - 1, -1, -1):
        v = vals[i]
        if v is not None and not (isinstance(v, float) and math.isnan(v)):
            return float(v), i
    return None, None


def latest_rf(conn) -> tuple[float, str | None]:
    """Latest 10Y Treasury yield (decimal) and its date, from the FRED macro table."""
    try:
        r = conn.execute("SELECT date, value FROM macro_obs WHERE series='DGS10' ORDER BY date DESC LIMIT 1").fetchone()
    except Exception:
        r = None
    return (float(r["value"]) / 100, r["date"]) if r else (RF_DEFAULT, None)


def market_profile(ticker: str) -> dict:
    """Yahoo's 5-year monthly beta vs the S&P 500, and its industry (used to spot banks / insurers — the SEC
    facts the engine reads carry no SIC code, so the engine's own sector guess can't tell)."""
    try:
        import yfinance as yf

        info = yf.Ticker(ticker).info
        b = info.get("beta")
        return {"beta": float(b) if b is not None else None, "industry": info.get("industry")}
    except Exception:
        return {"beta": None, "industry": None}


def is_financial(sector: str | None, industry: str | None) -> bool:
    """Banks and insurers (Yahoo industries "Banks - …", "Insurance - …"); card networks etc. are not."""
    return (sector or "").upper() in FINANCIAL_SECTORS or (industry or "").startswith(FINANCIAL_INDUSTRIES)


def compute(hist_rows: dict, cols: list[str], price: float | None, shares_m: float | None, beta: float | None,
            rf: float, sector: str | None = None, erp: float = ERP, industry: str | None = None) -> dict:
    """WACC and its pieces. Money in $M. Returns {"wacc", "re", "rd", "tax", "we", "wd", "beta", "rf", "erp",
    "equity", "debt", "cash", "net_debt", "financial", "year", "notes": [...]}."""
    notes = []
    b = beta if beta is not None and 0.1 <= beta <= 4 else None
    if b is None:
        notes.append(f"β 缺失或异常，按 {BETA_DEFAULT:.1f}")
        b = BETA_DEFAULT
    raw = b
    b = min(max(0.67 * b + 0.33, 0.3), 3.0)  # Blume adjustment: raw betas drift toward 1 (as Bloomberg shows)
    re = rf + b * erp

    debt_b, i_debt = _last(hist_rows, "Total Debt (B)")
    cash_b, _ = _last(hist_rows, "Total Cash (B)")
    interest, _ = _last(hist_rows, "Interest Expense (M)")
    tax_pct, _ = _last(hist_rows, "Effective Tax Rate (%)")
    debt = (debt_b or 0.0) * 1000
    cash = (cash_b or 0.0) * 1000
    tax = tax_pct / 100 if tax_pct is not None and 0 <= tax_pct <= 60 else None
    if tax is None:
        notes.append("有效税率缺失，按 21%")
        tax = 0.21
    tax = min(max(tax, 0.0), 0.35)

    rd = abs(interest) / debt if interest and debt > 0 else None
    if rd is None:
        rd = rf + 0.015
        if debt > 0:
            notes.append("利息费用缺失，债务成本按 10 年美债 + 1.5%")
    rd = min(max(rd, rf), rf + 0.08)

    financial = is_financial(sector, industry)
    equity = price * shares_m if price and shares_m else None
    if financial:
        notes.append("银行/保险：负债属于经营，只用股权成本，不扣净负债")
        we, wd, wacc, net_debt = 1.0, 0.0, re, 0.0
    elif equity and equity > 0:
        we, wd = equity / (equity + debt), debt / (equity + debt)
        wacc = we * re + wd * rd * (1 - tax)
        net_debt = debt - cash
    else:
        notes.append("缺少市值，权重无法计算，按股权成本")
        we, wd, wacc, net_debt = 1.0, 0.0, re, debt - cash
    year = cols[i_debt] if i_debt is not None and i_debt < len(cols) else None
    return {"wacc": wacc, "re": re, "rd": rd, "tax": tax, "we": we, "wd": wd, "beta": b, "beta_raw": raw, "rf": rf, "erp": erp,
            "equity": equity, "debt": debt, "cash": cash, "net_debt": net_debt, "financial": financial,
            "year": year, "notes": notes}


def interest_addback_margin(hist_rows: dict, tax: float, n: int = 5) -> float:
    """After-tax interest ÷ revenue, averaged over the last n annual reports — added to the FCF margin to turn
    CFO − capex (after interest) into free cash flow to the firm."""
    rev = hist_rows.get("Revenue (M)") or []
    it = hist_rows.get("Interest Expense (M)") or []
    xs = []
    for i in range(min(len(rev), len(it)) - 1, -1, -1):
        if len(xs) >= n:
            break
        if rev[i] and rev[i] > 0 and it[i] is not None:
            xs.append(abs(it[i]) * (1 - tax) / rev[i])
    return sum(xs) / len(xs) if xs else 0.0
