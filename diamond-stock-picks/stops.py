"""Volatility-based trailing stop. No fixed exit target.

The engine's own backtest found that fixed targets (5-12%) with tight stops destroyed the returns of these 90-day
momentum picks, so the exit here is a stop that only rises:

  distance  = STOP_ATR_MULT (2.5) x ATR% (14-day average true range / close), clamped to 5-15%; STOP_PCT when there is no ATR
  start     = buy x (1 - distance)
  trail     = highest close since held x (1 - distance)
  stop      = the higher of the two, never lowered
  2R level  = buy x (1 + 2 x distance at the start). A milestone, not an exit: once a close reaches it, the stop is
              at least the buy price (break-even).
"""
from __future__ import annotations

import os

ATR_MULT = float(os.environ.get("STOP_ATR_MULT", "2.5"))  # 3-3.5 gives a wider, slower stop
MIN_DIST = 0.05
MAX_DIST = 0.15


def distance(atr_pct: float | None, fallback_pct: float) -> float:
    """Stop distance as a fraction of price."""
    if atr_pct is None or atr_pct != atr_pct or atr_pct <= 0:
        return fallback_pct / 100
    return min(max(ATR_MULT * atr_pct, MIN_DIST), MAX_DIST)


def entry_levels(price: float, atr_pct: float | None, fallback_pct: float) -> dict:
    """Stop and 2R milestone for a new entry at `price`."""
    d = distance(atr_pct, fallback_pct)
    return {"stop": round(price * (1 - d), 2), "two_r": round(price * (1 + 2 * d), 2), "risk_pct": round(d * 100, 1),
            "atr_pct": None if atr_pct is None else round(atr_pct * 100, 2)}


def trail(prev: dict | None, buy: float, close: float | None, atr_pct: float | None, fallback_pct: float) -> dict:
    """Advance one holding's trailing state. `prev` is the stored state (None the first time it is seen, or after the
    buy price changed); `close` is a daily close (None for an intraday check, which never moves the high).
    Returns {buy, dist, two_r, high, stop, two_r_hit}."""
    if not prev or prev.get("buy") != buy:
        d = distance(atr_pct, fallback_pct)
        prev = {"buy": buy, "dist": d, "two_r": round(buy * (1 + 2 * d), 2), "high": buy,
                "stop": round(buy * (1 - d), 2), "two_r_hit": False}
    st = dict(prev)
    if close is not None and close == close and close > st["high"]:
        st["high"] = round(close, 2)
    d = distance(atr_pct, fallback_pct) if atr_pct is not None else st["dist"]
    stop = max(st["stop"], round(st["high"] * (1 - d), 2))
    if st["high"] >= st["two_r"]:
        st["two_r_hit"] = True
    if st["two_r_hit"]:
        stop = max(stop, buy)
    st["stop"] = round(stop, 2)
    return st
