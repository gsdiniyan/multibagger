"""Steady strategy - low-beta quality stocks, inverse-volatility weighted.

Prioritizes capital protection over growth:
  - Selects low-beta stocks with positive alpha and consistent growth
  - Inverse-volatility weighting (lower vol = higher weight)
  - Sector caps enforced to avoid concentration
  - 15-20 stocks, quarterly rebalancing

Target: 15-18% CAGR, <25% max drawdown, Sharpe > 0.8
"""

from __future__ import annotations

import logging

import pandas as pd

from diamond.analysis import optimizer
from diamond.config import get_config
from diamond.strategies.constraints import apply_sector_cap, cap_sector_weights

logger = logging.getLogger(__name__)

TARGET_SIZE = 18


def _get_screened_universe(end_date: str | None = None) -> pd.DataFrame:
    """Load or run the screener."""
    from diamond.analysis.screener import screen

    return screen(end_date=end_date)


class SteadyStrategy:
    name = "steady"

    def screen(self, universe_df: pd.DataFrame, end_date: str | None = None) -> pd.DataFrame:
        """Return screened universe for stock selection during allocate()."""
        self._end_date = end_date  # remembered so allocate() weights use the same as-of date
        return _get_screened_universe(end_date=end_date)

    def allocate(self, candidates: pd.DataFrame, capital: float) -> dict[str, float]:
        """Select low-beta quality stocks with inverse-volatility weighting.

        Selection criteria:
          1. Beta < 1.0 (less volatile than market)
          2. Alpha > 0 (outperforming risk-adjusted)
          3. CAGR > 10% (meaningful growth)
          4. Volatility < 30% (not too wild)
          5. Sorted by Alpha/Volatility efficiency ratio
        """
        if candidates.empty or "Beta" not in candidates.columns:
            return {}

        cfg = get_config()

        # Primary filter: low-beta quality
        filtered = candidates[(candidates["Beta"] < 1.0) & (candidates["Alpha"] > 0) & (candidates["CAGR"] > 0.10)]

        # If Volatility column exists, cap it
        if "Volatility" in filtered.columns:
            vol_filtered = filtered[filtered["Volatility"] < 0.30]
            if len(vol_filtered) >= TARGET_SIZE:
                filtered = vol_filtered

        # Fallback: relax filters if too few candidates
        if len(filtered) < TARGET_SIZE:
            filtered = pd.DataFrame(candidates[(candidates["Beta"] < 1.2) & (candidates["CAGR"] > 0.05)])

        if len(filtered) == 0:
            return {}

        # Rank by efficiency: Alpha / Volatility
        filtered = pd.DataFrame(filtered).copy()
        if "Volatility" in list(filtered.columns):
            filtered["_efficiency"] = filtered["Alpha"] / filtered["Volatility"].clip(lower=0.01)
        else:
            filtered["_efficiency"] = filtered["Alpha"]
        filtered = filtered.sort_values("_efficiency", ascending=False)  # type: ignore[call-overload]

        # Apply sector caps and take top N
        tickers = apply_sector_cap(filtered["Ticker"].tolist())[:TARGET_SIZE]
        if not tickers:
            return {}

        # Inverse-volatility weighting
        from diamond.data import market

        try:
            prices = market.download_prices(tickers, period_days=365, end_date=getattr(self, "_end_date", None))
            returns_df = prices.pct_change().dropna()
            weights = optimizer.inverse_volatility_weights(
                returns_df,
                max_weight=cfg.portfolio.max_position_weight,
            )
        except Exception:
            logger.warning("Price download failed, using equal weights")
            weights = optimizer.equal_weights(tickers)

        # Apply sector weight caps
        weights = cap_sector_weights(weights)

        allocation = {t: w * capital for t, w in weights.items()}
        logger.info(f"Steady allocation: {len(allocation)} stocks, top weight: {max(weights.values()):.1%}")
        return allocation

    def should_rebalance(
        self,
        current_weights: dict[str, float],
        target_weights: dict[str, float],
        days_since_last: int,
    ) -> bool:
        """Rebalance quarterly (every 90 days)."""
        cfg = get_config()
        return days_since_last >= cfg.rebalance.rebalance_frequency_days
