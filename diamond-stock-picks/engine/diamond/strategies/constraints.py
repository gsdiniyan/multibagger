"""Sector constraint enforcement for portfolio construction.

Shared across all active strategies to cap sector concentration.
"""

from __future__ import annotations

from diamond.config import get_config
from diamond.data.universe import get_sector


def apply_sector_cap(
    candidates: list[str],
    max_per_sector: int | None = None,
) -> list[str]:
    """Filter candidates to enforce max stocks per sector.

    Args:
        candidates: Ordered list of tickers (best first).
        max_per_sector: Max stocks allowed per sector.

    Returns:
        Filtered list respecting sector caps.
    """
    if max_per_sector is None:
        max_per_sector = get_config().portfolio.max_stocks_per_sector

    sector_counts: dict[str, int] = {}
    result = []

    for ticker in candidates:
        sector = get_sector(ticker)
        count = sector_counts.get(sector, 0)
        if count < max_per_sector:
            result.append(ticker)
            sector_counts[sector] = count + 1

    return result


def cap_sector_weights(
    weights: dict[str, float],
    max_sector_pct: float | None = None,
) -> dict[str, float]:
    """Cap aggregate sector weight and redistribute excess.

    Args:
        weights: {ticker: weight} where weights sum to ~1.0.
        max_sector_pct: Max sector weight as fraction (e.g., 0.25).

    Returns:
        Adjusted weights with sector caps applied, summing to 1.0.
    """
    if max_sector_pct is None:
        max_sector_pct = get_config().portfolio.max_sector_exposure

    # Calculate sector totals
    sector_weights: dict[str, float] = {}
    for ticker, w in weights.items():
        sector = get_sector(ticker)
        sector_weights[sector] = sector_weights.get(sector, 0) + w

    # Iteratively cap sectors and redistribute excess to uncapped sectors
    adjusted = dict(weights)
    for _ in range(10):  # Converges in 2-3 iterations
        sector_totals: dict[str, float] = {}
        for ticker, w in adjusted.items():
            sector = get_sector(ticker)
            sector_totals[sector] = sector_totals.get(sector, 0) + w

        excess = 0.0
        uncapped_total = 0.0
        for _sector, total in sector_totals.items():
            if total > max_sector_pct:
                excess += total - max_sector_pct
            else:
                uncapped_total += total

        if excess < 1e-9:
            break

        # Scale down overweight sectors, scale up underweight sectors
        for ticker in adjusted:
            sector = get_sector(ticker)
            stotal = sector_totals[sector]
            if stotal > max_sector_pct:
                adjusted[ticker] *= max_sector_pct / stotal
            elif uncapped_total > 0:
                adjusted[ticker] *= (uncapped_total + excess) / uncapped_total

    # Renormalize to 1.0
    total = sum(adjusted.values())
    if total > 0:
        adjusted = {t: w / total for t, w in adjusted.items()}

    return adjusted
