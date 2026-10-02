"""Run the engine's Steady and God's Plan strategies on the installed price store."""
from __future__ import annotations

import os
import sys

import pandas as pd

import stops

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


def build_rows(alloc: dict[str, dict[str, float]], last_close: dict[str, float], stop_pct: float,
               atr: dict[str, float] | None = None) -> list[dict]:
    """One row per distinct stock: list membership, reference price, quantity, starting stop (2.5x ATR, 5-15%,
    stop_pct when there is no ATR) and the 2R milestone. No fixed target: see stops.py."""
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
            **stops.entry_levels(shown, (atr or {}).get(s), stop_pct),
        })
    return rows


def pick_status(close: pd.DataFrame, since: dict[str, str], atr: dict[str, float] | None, stop_pct: float) -> dict:
    """Where each listed stock stands since it joined the list, by the same trailing-stop rule as holdings (stops.py),
    recomputed from daily closes on every run so it never depends on saved state:
      reference  the close on the day it joined the list (`since`, YYYY-MM-DD; the first close on or after it)
      OPEN        never closed at or below its trailing stop, 2R level not reached
      2R REACHED  a close reached the 2R level (the stop is now at least the reference price)
      STOPPED     a close at or below the trailing stop as it stood the day before (final; `at` is that day)
    {SYMBOL: {status, at, ref_price, ref_date, stop, high, two_r}}; a stock without closes is left out."""
    out: dict = {}
    for sym, day in since.items():
        col = f"{sym}.NS"
        if col not in close.columns:
            continue
        s = close[col].dropna()
        s = s[s.index >= pd.Timestamp(day)]
        if s.empty:
            continue
        ref_date, ref = s.index[0], float(s.iloc[0])
        a = (atr or {}).get(sym)
        t = stops.trail(None, ref, None, a, stop_pct)
        status, at = None, None
        for d, c in s.iloc[1:].items():
            if c <= t["stop"]:
                status, at = "STOPPED", d.date().isoformat()
                break
            was = t["two_r_hit"]
            t = stops.trail(t, ref, float(c), a, stop_pct)
            if t["two_r_hit"] and not was:
                at = d.date().isoformat()
        out[sym] = {"status": status or ("2R REACHED" if t["two_r_hit"] else "OPEN"), "at": at,
                    "ref_price": round(ref, 2), "ref_date": ref_date.date().isoformat(), "stop": t["stop"],
                    "high": t["high"], "two_r": t["two_r"]}
    return out
