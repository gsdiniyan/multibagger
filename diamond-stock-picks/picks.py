"""Run the engine's Steady and God's Plan strategies on the installed price store."""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine"))

STRATEGIES = ("steady", "gods_plan")


def _strategy(name: str):
    from diamond.strategies.gods_plan import GodsPlanStrategy
    from diamond.strategies.steady import SteadyStrategy

    return {"steady": SteadyStrategy, "gods_plan": GodsPlanStrategy}[name]()


def universe_symbols() -> list[str]:
    """Bare NSE symbols of the engine's screening universe."""
    from diamond.data import universe

    return sorted({t.replace(".NS", "") for t in universe.NSE_500 + universe.NIFTY_50})


def compute(last_date: pd.Timestamp, capital: float) -> dict[str, dict[str, float]]:
    """{strategy: {symbol: rupee amount}} as of the last available bar (inclusive)."""
    as_of = (last_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")  # engine end date is exclusive
    out: dict[str, dict[str, float]] = {}
    for name in STRATEGIES:
        st = _strategy(name)
        cands = st.screen(pd.DataFrame(), end_date=as_of)
        alloc = st.allocate(cands, capital) if not cands.empty else {}
        out[name] = {t.replace(".NS", ""): float(a) for t, a in alloc.items()}
    return out


def build_rows(alloc: dict[str, dict[str, float]], last_close: dict[str, float], stop_pct: float) -> list[dict]:
    """One row per distinct stock: list membership, reference price, quantity, stop."""
    symbols = sorted({s for a in alloc.values() for s in a})
    rows = []
    for s in symbols:
        price = float(last_close.get(s, float("nan")))
        amt = max(a.get(s, 0.0) for a in alloc.values())
        qty = int(amt / price) if price == price and price > 0 else 0
        member = [n for n in STRATEGIES if s in alloc[n]]
        shown = round(price, 2)  # stop is computed from the price actually displayed
        rows.append({
            "symbol": s,
            "lists": "Both" if len(member) == 2 else ("Steady" if member == ["steady"] else "God's Plan"),
            "price": shown,
            "qty": qty,
            "invest": round(qty * price),
            "stop": round(shown * (1 - stop_pct / 100), 2),
        })
    return rows
