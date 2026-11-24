import pandas as pd
import numpy as np
from typing import Optional


# =======================================
#  Get the latest close from Stooq
# =======================================

# Optional manual overrides for sector inference (ticker -> sector key).
# Populate this dict in code or tests if you need to force a sector for a ticker.
MANUAL_SECTOR_OVERRIDE = {}

def get_latest_price_stooq(ticker: str) -> float:
    """
    Get the latest US stock close from Stooq (Close of the most recent trading day).
    AAPL -> aapl.us
    """
    symbol = ticker.lower() + ".us"
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"

    try:
        df = pd.read_csv(url)
    except Exception as e:
        raise RuntimeError(f"Stooq connection failed: {e}")

    if df is None or df.empty:
        raise ValueError(f"Stooq returned no data: {ticker}")

    return float(df["Close"].iloc[-1])


# =======================================
#  Infer sector from the SEC SIC code → sector key
# =======================================

def map_sic_to_sector(sic: Optional[int]) -> str:
    """
    Roughly map SEC SIC codes into a few broad categories we define:
        TECH / CONSUMER / BANK / INSURANCE / ENERGY / PHARMA
    Anything unrecognized defaults to CONSUMER.
    """
    if sic is None:
        return "CONSUMER"

    try:
        s = int(sic)
    except Exception:
        return "CONSUMER"

    # The mapping below is very coarse; refine it over time
    if 3570 <= s <= 3579 or 7370 <= s <= 7379 or 3670 <= s <= 3699:
        return "TECH"
    if 4810 <= s <= 4899:  # Telecom, communications
        return "TECH"

    if 2830 <= s <= 2839 or 3840 <= s <= 3859:  # Pharma & medical devices
        return "PHARMA"

    if 6000 <= s <= 6199:  # Banks, financial services
        return "BANK"
    if 6200 <= s <= 6299 or 6300 <= s <= 6499:  # Advisory, insurance, etc.
        return "INSURANCE"

    if 1300 <= s <= 1399 or 2900 <= s <= 2999:  # Oil, gas, refining
        return "ENERGY"

    # default: consumer, other
    return "CONSUMER"


def infer_sector_from_facts(facts: dict, ticker: str) -> str:
    tic = ticker.upper()

    # A MANUAL OVERRIDE may be added later, code below:
    if tic in MANUAL_SECTOR_OVERRIDE:
        return MANUAL_SECTOR_OVERRIDE[tic]

    sic = facts.get("sic")
    return map_sic_to_sector(sic)


# =======================================
# Decide whether a period is a full year based on start/end dates
# =======================================
from datetime import datetime

def is_true_annual_period(start_date: str, end_date: str) -> bool:
    """
    Decide whether a reported period is“close to one year”, i.e. annual data.

    Logic:
    - Parse start / end dates
    - Compute the day difference days = (end-start)+1
    - Between [330, 400] days, treat it as“annual”, otherwise treat it as quarterly / semi-annual / other

    Note:
    - Parse failure or any exception → return False (better to miss data than to take wrong data)
    """
    try:
        if not start_date or not end_date:
            return False

        s = datetime.strptime(start_date, "%Y-%m-%d")
        e = datetime.strptime(end_date, "%Y-%m-%d")

        days = (e - s).days + 1
        return 330 <= days <= 400
    except Exception:
        return False


# =======================================
#  Historical year-end prices via yfinance
# =======================================

def get_historical_year_end_prices(ticker: str, start_year: int, end_year: int) -> pd.Series:
    """Get year-end UNADJUSTED closing prices for each year using yfinance.

    yfinance always returns split-adjusted prices (even with auto_adjust=False).
    Since SEC shares outstanding are NOT split-adjusted, we need true unadjusted
    prices to compute correct Market Cap (Price × Shares).

    We reverse the split adjustment by multiplying split-adjusted prices by the
    cumulative product of all splits that occurred AFTER each date.

    Returns a Series indexed by year (int) with the last trading day's
    unadjusted closing price for each year in [start_year, end_year].
    """
    import yfinance as yf

    start_date = f"{start_year}-01-01"
    end_date = f"{end_year + 1}-01-15"

    tk = yf.Ticker(ticker)
    hist = tk.history(start=start_date, end=end_date, auto_adjust=False)

    if hist.empty:
        return pd.Series(dtype="float64", name="YearEndPrice")

    hist.index = pd.to_datetime(hist.index)
    if hist.index.tz is not None:
        hist.index = hist.index.tz_localize(None)

    # Get split history and compute reverse adjustment factor.
    # yfinance divides all pre-split prices by the split ratio,
    # so we multiply back to get the original price.
    splits = tk.splits
    if splits is not None and not splits.empty:
        splits.index = pd.to_datetime(splits.index)
        if splits.index.tz is not None:
            splits.index = splits.index.tz_localize(None)

    prices = {}
    for year in range(start_year, end_year + 1):
        year_data = hist[hist.index.year == year]
        if not year_data.empty:
            last_date = year_data.index[-1]
            adj_price = float(year_data["Close"].iloc[-1])

            # Compute cumulative split factor for all splits AFTER this date
            reverse_factor = 1.0
            if splits is not None and not splits.empty:
                future_splits = splits[splits.index > last_date]
                for ratio in future_splits:
                    reverse_factor *= ratio

            prices[year] = adj_price * reverse_factor

    result = pd.Series(prices, name="YearEndPrice")
    result.index.name = "year"
    return result


# =======================================
#  Simple WACC calculator
# =======================================

def compute_wacc(
    ticker: str,
    total_debt: float,
    stockholders_equity: float,
    interest_expense: float,
    tax_rate: float,
    risk_free_rate: float = 0.04,
    equity_risk_premium: float = 0.055,
) -> float:
    """Simple WACC estimate.

    Cost of equity = risk_free_rate + beta * equity_risk_premium
    Cost of debt = interest_expense / total_debt * (1 - tax_rate)
    WACC = (E/V) * cost_of_equity + (D/V) * cost_of_debt

    Uses yfinance for beta. Falls back to beta=1.0 if unavailable.
    """
    import yfinance as yf

    # Get beta from yfinance
    try:
        tk = yf.Ticker(ticker)
        info = tk.info
        beta = info.get("beta")
        if beta is None or not np.isfinite(beta):
            beta = 1.0
    except Exception:
        beta = 1.0

    cost_of_equity = risk_free_rate + beta * equity_risk_premium

    # Cost of debt
    if total_debt > 0 and interest_expense > 0:
        cost_of_debt = (interest_expense / total_debt) * (1.0 - tax_rate)
    else:
        cost_of_debt = 0.0

    # Capital structure weights
    total_value = stockholders_equity + total_debt
    if total_value <= 0:
        return cost_of_equity  # fallback: all-equity

    weight_equity = stockholders_equity / total_value
    weight_debt = total_debt / total_value

    wacc = weight_equity * cost_of_equity + weight_debt * cost_of_debt
    return wacc