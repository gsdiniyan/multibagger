"""Indian market transaction cost model.

Pure functions - no side effects. All costs in INR.
Covers: brokerage, GST, STT, exchange fees, SEBI charges, stamp duty, slippage.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class CostBreakdown:
    brokerage: float
    gst: float  # 18% of brokerage
    stt: float  # Securities Transaction Tax
    exchange_fees: float
    sebi_charges: float
    stamp_duty: float
    slippage: float  # Estimated market impact + bid-ask spread
    total: float

    def __repr__(self) -> str:
        return f"CostBreakdown(total={self.total:.2f})"


def estimate_slippage(amount: float, avg_daily_value: float | None = None) -> float:
    """Estimate slippage based on trade size relative to liquidity.

    Uses a square-root market impact model: impact = k * sqrt(trade_size / ADV).
    Falls back to a flat 0.05% when ADV is unknown.

    Args:
        amount: Trade value in INR.
        avg_daily_value: Average daily traded value in INR (volume * price).
            None means unknown — uses flat estimate.

    Returns:
        Estimated slippage in INR.
    """
    if amount <= 0:
        return 0.0

    if avg_daily_value is None or avg_daily_value <= 0:
        return amount * 0.0005  # flat 0.05% fallback

    participation = amount / avg_daily_value

    # Square-root impact model with scaling constant k=0.1
    # Capped at 2% to avoid absurd estimates for very illiquid stocks
    impact_pct = min(0.1 * math.sqrt(participation), 0.02)

    # Floor at 0.02% (minimum bid-ask spread for liquid large-caps)
    impact_pct = max(impact_pct, 0.0002)

    return amount * impact_pct


def calculate_costs(
    action: str,
    amount: float,
    avg_daily_value: float | None = None,
) -> CostBreakdown:
    """Calculate comprehensive Indian market transaction costs.

    Args:
        action: 'BUY' or 'SELL'
        amount: Trade value in INR (shares * price)
        avg_daily_value: Average daily traded value in INR for the stock.
            When provided, slippage scales with trade size vs liquidity.
            When None, uses a flat 0.05% estimate.

    Returns:
        CostBreakdown with itemized costs.

    Cost structure (as of 2024):
        - Brokerage: 0.05% (capped at 20 INR per order for discount brokers)
        - GST: 18% of brokerage
        - STT: 0.1% on buy (delivery), 0.1% on sell (delivery)
        - Exchange fees: 0.00345%
        - SEBI charges: 0.001%
        - Stamp duty: 0.015% buy, 0.003% sell
        - Slippage: volume-aware sqrt impact model (flat 0.05% fallback)
    """
    if amount <= 0:
        return CostBreakdown(0, 0, 0, 0, 0, 0, 0, 0)

    # Brokerage: 0.05% capped at 20 INR
    brokerage = min(amount * 0.0005, 20.0)

    # GST on brokerage: 18%
    gst = brokerage * 0.18

    # STT: 0.1% on delivery trades (both buy and sell)
    stt = amount * 0.001

    # Exchange transaction fees: 0.00345%
    exchange_fees = amount * 0.0000345

    # SEBI charges: 0.001%
    sebi_charges = amount * 0.00001

    # Stamp duty: differs for buy vs sell
    if action == "BUY":
        stamp_duty = amount * 0.00015  # 0.015%
    else:
        stamp_duty = amount * 0.00003  # 0.003%

    # Volume-aware slippage
    slippage = estimate_slippage(amount, avg_daily_value)

    total = brokerage + gst + stt + exchange_fees + sebi_charges + stamp_duty + slippage

    return CostBreakdown(
        brokerage=round(brokerage, 2),
        gst=round(gst, 2),
        stt=round(stt, 2),
        exchange_fees=round(exchange_fees, 2),
        sebi_charges=round(sebi_charges, 2),
        stamp_duty=round(stamp_duty, 2),
        slippage=round(slippage, 2),
        total=round(total, 2),
    )


def round_trip_cost_pct(amount: float) -> float:
    """Calculate round-trip cost as percentage (buy + sell)."""
    buy = calculate_costs("BUY", amount)
    sell = calculate_costs("SELL", amount)
    return (buy.total + sell.total) / amount * 100 if amount > 0 else 0.0


def annual_cost_estimate(
    portfolio_value: float,
    turnover_pct: float = 0.50,
    rebalances_per_year: int = 4,
) -> float:
    """Estimate annual transaction costs.

    Args:
        portfolio_value: Total portfolio value in INR
        turnover_pct: Annual portfolio turnover as fraction (0.50 = 50%)
        rebalances_per_year: Number of rebalancing events

    Returns:
        Estimated annual cost in INR
    """
    traded_value = portfolio_value * turnover_pct
    avg_trade = traded_value / (rebalances_per_year * 2)  # Buy + sell per rebalance
    total = 0.0
    for _ in range(rebalances_per_year):
        total += calculate_costs("SELL", avg_trade).total
        total += calculate_costs("BUY", avg_trade).total
    return round(total, 2)
