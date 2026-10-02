"""The volatility-based trailing stop (stops.py) and how picks / holdings use it."""
from __future__ import annotations

import pandas as pd

import alerts as al
import picks as pk
import prices as px
import stops


def test_distance_is_2_5x_atr_clamped_to_5_15_pct():
    assert stops.distance(0.02, 10) == 0.05          # 2.5 x 2% = 5%
    assert round(stops.distance(0.03, 10), 4) == 0.075
    assert stops.distance(0.01, 10) == 0.05          # floor: a very quiet stock still gets 5%
    assert stops.distance(0.09, 10) == 0.15          # cap: a wild stock never gets more than 15%
    assert stops.distance(None, 10) == 0.10          # no ATR: STOP_PCT
    assert stops.distance(float("nan"), 8) == 0.08


def test_worked_example_bought_3400_atr_2pct():
    """The example shown when the rule was chosen: bought 3,400, ATR 2% of price."""
    t = stops.trail(None, 3400.0, None, 0.02, 10)
    assert t["stop"] == 3230.0 and t["two_r"] == 3740.0      # start stop -5%, 2R level +10%
    t = stops.trail(t, 3400.0, 3700.0, 0.02, 10)
    assert t["stop"] == 3515.0 and not t["two_r_hit"]       # 3,700 - 2.5 x 74
    t = stops.trail(t, 3400.0, 3750.0, 0.02, 10)
    assert t["two_r_hit"] and t["stop"] == 3562.5           # 2R reached; trail is already above break-even
    t = stops.trail(t, 3400.0, 4100.0, 0.02, 10)
    assert t["stop"] == 3895.0
    t = stops.trail(t, 3400.0, 3900.0, 0.02, 10)            # a lower close never lowers the stop
    assert t["stop"] == 3895.0 and t["high"] == 4100.0


def test_two_r_lifts_stop_to_break_even_when_the_trail_is_wider():
    t = stops.trail(None, 100.0, 110.0, 0.02, 10)            # 2R = 110, trail 110 x 0.95 = 104.5
    assert t["two_r_hit"] and t["stop"] == 104.5
    wide = stops.trail({**t, "stop": 90.0, "high": 100.0, "two_r_hit": True}, 100.0, None, 0.06, 10)
    assert wide["stop"] == 100.0                             # 15% trail would be 85; break-even holds


def test_intraday_price_never_moves_the_high_and_a_new_buy_resets():
    t = stops.trail(None, 100.0, None, 0.02, 10)
    assert t["high"] == 100.0 and t["stop"] == 95.0
    t2 = stops.trail({**t, "high": 130.0, "stop": 123.5}, 120.0, None, 0.02, 10)  # bought again at a new price
    assert t2["buy"] == 120.0 and t2["high"] == 120.0 and t2["stop"] == 114.0


def test_atr_pct_skips_split_bars():
    idx = pd.bdate_range("2026-09-01", periods=20)
    close = pd.Series(100.0, index=idx)
    df = pd.DataFrame({"High": close * 1.01, "Low": close * 0.99, "Close": close})
    assert px.atr_pct(df) == 0.02
    df.iloc[10] = [55.0, 49.0, 50.0]                         # an unadjusted 1:2 split
    df.iloc[11:] = df.iloc[11:] / 2
    assert px.atr_pct(df) == 0.02
    assert px.atr_pct(df.iloc[:5]) is None                   # too little history
    assert px.atr_pct(df[["Close"]]) is None


def test_build_rows_uses_atr_per_stock():
    alloc = {"steady": {"A": 10000.0, "B": 10000.0}, "gods_plan": {}}
    rows = {r["symbol"]: r for r in pk.build_rows(alloc, {"A": 200.0, "B": 100.0}, 10, {"A": 0.03})}
    assert rows["A"]["stop"] == 185.0 and rows["A"]["two_r"] == 230.0 and rows["A"]["risk_pct"] == 7.5
    assert rows["A"]["atr_pct"] == 3.0
    assert rows["B"]["stop"] == 90.0 and rows["B"]["two_r"] == 120.0 and rows["B"]["atr_pct"] is None


def test_check_holdings_trails_and_forgets_sold_stocks():
    trails = {"OLD": {"buy": 1.0}}
    held = {"A": {"qty": 10, "buy": 100.0}}
    rows = al.check_holdings(held, {"A": 112.0}, {"A"}, 10, trails, {"A": 112.0}, {"A": 0.02})
    assert "OLD" not in trails and trails["A"]["high"] == 112.0
    r = rows[0]
    assert r["stop"] == 106.4 and r["two_r_hit"] and r["trailing"] and r["status"] == "HELD"
    # next day, intraday dip to the stop: STOP HIT on the stored stop
    rows = al.check_holdings(held, {"A": 106.0}, {"A"}, 10, trails, None, {"A": 0.02})
    assert rows[0]["status"] == "STOP HIT" and rows[0]["stop"] == 106.4
