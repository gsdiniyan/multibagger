"""Per-stock fundamentals (yfinance snapshot) with plain-rule flags.

Gaps are common for Indian stocks (ROE, debt for banks, free cash flow). Missing values stay
None and are shown as n/a, never guessed. ROE falls back to EPS / book value (marked roe_proxy).
Flags are prompts to look twice, not buy/sell advice; thresholds are the constants below.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

PE_RICH, PE_VERY_RICH = 40, 70
PB_RICH_FINANCIAL = 4.0
DEBT_TO_EQUITY_HIGH = 100.0  # yfinance reports debt/equity in percent
ROE_LOW = 0.10
SALES_GROWTH_WEAK = 0.05
MARGIN_THIN = 0.05

_FIELDS = {
    "pe": "trailingPE", "pb": "priceToBook", "de": "debtToEquity", "roe": "returnOnEquity",
    "sales_g": "revenueGrowth", "earn_g": "earningsGrowth", "margin": "profitMargins",
    "mcap": "marketCap", "eps": "trailingEps", "book": "bookValue",
}


def _fetch(symbol: str) -> dict:
    import yfinance as yf

    info: dict = {}
    for _ in range(3):
        try:
            info = yf.Ticker(f"{symbol}.NS").info or {}
            if info:
                break
        except Exception:
            continue
    row = {k: info.get(v) for k, v in _FIELDS.items()}
    row["sector"] = info.get("sector") or "n/a"
    row["symbol"] = symbol
    return row


def assess(r: dict) -> dict:
    financial = r["sector"] == "Financial Services"
    roe, proxy = r["roe"], False
    if roe is None and r["eps"] and r["book"] and r["book"] > 0:
        roe, proxy = r["eps"] / r["book"], True
    flags: list[str] = []
    pe = r["pe"]
    if pe is None or pe <= 0:
        flags.append("no earnings / P/E n/a")
    elif pe > PE_VERY_RICH:
        flags.append(f"very rich P/E {pe:.0f}")
    elif pe > PE_RICH:
        flags.append(f"rich P/E {pe:.0f}")
    if financial and r["pb"] and r["pb"] > PB_RICH_FINANCIAL:
        flags.append(f"rich P/B {r['pb']:.1f}")
    if not financial and r["de"] is not None and r["de"] > DEBT_TO_EQUITY_HIGH:
        flags.append(f"high debt/equity {r['de']:.0f}%")
    if roe is not None and roe < ROE_LOW:
        flags.append(f"low ROE {roe:.0%}")
    if r["sales_g"] is not None and r["sales_g"] < SALES_GROWTH_WEAK:
        flags.append(f"weak sales growth {r['sales_g']:.0%}")
    if r["earn_g"] is not None and r["earn_g"] < 0:
        flags.append(f"earnings falling {r['earn_g']:.0%}")
    if not financial and r["margin"] is not None and r["margin"] < MARGIN_THIN:
        flags.append(f"thin margin {r['margin']:.0%}")
    missing = sum(v is None for v in [r["pe"], r["pb"], r["sales_g"], r["earn_g"], r["margin"], roe])
    if missing >= 3:
        flags.append(f"data thin ({missing}/6 missing)")
    no_earnings = pe is None or pe <= 0
    verdict = "AVOID" if (no_earnings or len(flags) >= 3) else "WATCH" if flags else "OK"
    return {**r, "roe": roe, "roe_proxy": proxy, "financial": financial, "flags": flags, "verdict": verdict}


def check(symbols: list[str], workers: int = 4) -> dict[str, dict]:
    with ThreadPoolExecutor(workers) as ex:
        return {r["symbol"]: assess(r) for r in ex.map(_fetch, symbols)}
