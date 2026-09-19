"""God's Plan v2 - quality growth + defensive + value strategy.

Balanced aggressive strategy targeting 18-22% CAGR with <30% max drawdown.

55% Quality Growth: High-alpha trending stocks, inverse-volatility weighted
25% Defensive Anchor: Low-beta quality stocks for downside protection
20% Opportunistic Value: High CAGR/Volatility ratio + positive alpha

Target: 15-18 stocks, quarterly rebalancing, 12% max position weight.
No stock duplication across components.
"""

from __future__ import annotations

import logging

import pandas as pd

from diamond.analysis import optimizer
from diamond.config import get_config
from diamond.data import market
from diamond.strategies.constraints import apply_sector_cap, cap_sector_weights

logger = logging.getLogger(__name__)

# Component allocations
QUALITY_GROWTH_ALLOC = 0.55
DEFENSIVE_ALLOC = 0.25
VALUE_ALLOC = 0.20

# Stock counts per component
QUALITY_GROWTH_COUNT = 10
DEFENSIVE_COUNT = 5
VALUE_COUNT = 3


def _get_screened_universe(force: bool = False, end_date: str | None = None) -> pd.DataFrame:
    """Load or run the screener."""
    from diamond.analysis.screener import screen

    return screen(force=force, end_date=end_date)


def _fetch_returns(tickers: list[str], days: int = 365, end_date: str | None = None) -> pd.DataFrame:
    """Fetch price data and compute returns for optimizer input (as of end_date when given)."""
    try:
        prices = market.download_prices(tickers, period_days=days, end_date=end_date)
        return prices.pct_change().dropna()
    except Exception as e:
        logger.warning(f"Failed to fetch returns: {e}")
        return pd.DataFrame()


class GodsPlanStrategy:
    """Quality growth + defensive + value strategy."""

    name = "gods_plan"

    def screen(self, universe_df: pd.DataFrame, end_date: str | None = None) -> pd.DataFrame:
        """Return screened universe for stock selection during allocate()."""
        self._end_date = end_date  # remembered so allocate() weights use the same as-of date
        return _get_screened_universe(end_date=end_date)

    def allocate(self, candidates: pd.DataFrame, capital: float) -> dict[str, float]:
        """Allocate capital across three components.

        55% -> Quality Growth (high-alpha trending, inverse-vol weighted)
        25% -> Defensive Anchor (low-beta quality, inverse-vol weighted)
        20% -> Opportunistic Value (low P/B + positive alpha)
        """
        if candidates.empty:
            return {}

        cfg = get_config()
        gp = cfg.gods_plan

        quality_capital = capital * QUALITY_GROWTH_ALLOC
        defensive_capital = capital * DEFENSIVE_ALLOC
        value_capital = capital * VALUE_ALLOC

        # --- 1. Quality Growth (55%) ---
        quality_alloc = self._select_quality_growth(candidates, quality_capital, gp)
        logger.info(f"Quality Growth: {len(quality_alloc)} stocks, {sum(quality_alloc.values()):,.0f} INR")

        # --- 2. Defensive Anchor (25%) ---
        used_tickers = set(quality_alloc.keys())
        defensive_alloc = self._select_defensive(candidates, defensive_capital, gp, used_tickers)
        logger.info(f"Defensive: {len(defensive_alloc)} stocks, {sum(defensive_alloc.values()):,.0f} INR")

        # --- 3. Opportunistic Value (20%) ---
        used_tickers = used_tickers | set(defensive_alloc.keys())
        value_alloc = self._select_value(candidates, value_capital, gp, used_tickers)
        logger.info(f"Value: {len(value_alloc)} stocks, {sum(value_alloc.values()):,.0f} INR")

        # --- Merge allocations ---
        merged: dict[str, float] = {}
        for alloc in [quality_alloc, defensive_alloc, value_alloc]:
            for t, amount in alloc.items():
                merged[t] = merged.get(t, 0) + amount

        total_stocks = len(merged)
        total_allocated = sum(merged.values())
        logger.info(f"God's Plan: {total_stocks} stocks, {total_allocated:,.0f} / {capital:,.0f} INR allocated")
        return merged

    def _select_quality_growth(
        self,
        candidates: pd.DataFrame,
        capital: float,
        gp,
    ) -> dict[str, float]:
        """High-alpha trending stocks, inverse-volatility weighted.

        Filters for:
          - Alpha > threshold (high risk-adjusted returns)
          - CAGR > threshold (proven growth)
          - Hurst > 0.5 (trending, not mean-reverting)
          - Beta in range (not too defensive, not too wild)
        """
        if candidates.empty:
            return {}

        # Apply filters progressively — relax if needed
        filtered = candidates[(candidates["Alpha"] > gp.core_min_alpha) & (candidates["CAGR"] > gp.core_min_cagr)]

        if "Hurst" in filtered.columns:
            trending = pd.DataFrame(filtered[filtered["Hurst"] > gp.core_min_hurst])
            if len(trending) >= QUALITY_GROWTH_COUNT:
                filtered = trending

        if "Beta" in filtered.columns:
            beta_filtered = pd.DataFrame(
                filtered[(filtered["Beta"] >= gp.core_min_beta) & (filtered["Beta"] <= gp.core_max_beta)]
            )
            if len(beta_filtered) >= QUALITY_GROWTH_COUNT:
                filtered = beta_filtered

        if len(filtered) < QUALITY_GROWTH_COUNT:
            filtered = candidates.nlargest(QUALITY_GROWTH_COUNT * 2, "Alpha")

        # Sort by alpha, apply sector cap
        filtered = filtered.sort_values("Alpha", ascending=False)  # type: ignore[call-overload]
        tickers = apply_sector_cap(filtered["Ticker"].tolist())[:QUALITY_GROWTH_COUNT]

        if not tickers:
            return {}

        # Inverse-volatility weighting (safer than Monte Carlo for concentrated portfolios)
        returns_df = _fetch_returns(tickers, end_date=getattr(self, "_end_date", None))
        if returns_df.empty:
            weights = optimizer.equal_weights(tickers)
        else:
            weights = optimizer.inverse_volatility_weights(
                returns_df,
                max_weight=gp.max_position_weight,
            )

        weights = cap_sector_weights(weights)
        return {t: w * capital for t, w in weights.items()}

    def _select_defensive(
        self,
        candidates: pd.DataFrame,
        capital: float,
        gp,
        exclude: set[str],
    ) -> dict[str, float]:
        """Low-beta quality stocks for downside protection.

        Filters for:
          - Beta < 0.8 (significantly less volatile than market)
          - Alpha > 0 (still outperforming)
          - CAGR > 12% (meaningful growth)
          - Sorted by Alpha/Volatility efficiency
        """
        if candidates.empty:
            return {}

        pool = pd.DataFrame(candidates[~candidates["Ticker"].isin(list(exclude))])

        filtered = pd.DataFrame(pool[(pool["Beta"] < 0.80) & (pool["Alpha"] > 0) & (pool["CAGR"] > 0.12)])

        if len(filtered) < DEFENSIVE_COUNT:
            filtered = pd.DataFrame(pool[pool["Beta"] < 1.0].nlargest(DEFENSIVE_COUNT * 2, "Alpha"))  # type: ignore[call-overload]

        if filtered.empty:
            return {}

        # Sort by efficiency
        filtered = filtered.copy()
        if "Volatility" in filtered.columns:
            filtered["_eff"] = filtered["Alpha"] / filtered["Volatility"].clip(lower=0.01)
        else:
            filtered["_eff"] = filtered["Alpha"]
        filtered = filtered.sort_values("_eff", ascending=False)  # type: ignore[call-overload]

        tickers = apply_sector_cap(filtered["Ticker"].tolist())[:DEFENSIVE_COUNT]
        if not tickers:
            return {}

        # Inverse-volatility weighting
        returns_df = _fetch_returns(tickers, end_date=getattr(self, "_end_date", None))
        if returns_df.empty:
            weights = optimizer.equal_weights(tickers)
        else:
            weights = optimizer.inverse_volatility_weights(
                returns_df,
                max_weight=gp.max_position_weight,
            )

        weights = cap_sector_weights(weights)
        return {t: w * capital for t, w in weights.items()}

    def _select_value(
        self,
        candidates: pd.DataFrame,
        capital: float,
        gp,
        exclude: set[str],
    ) -> dict[str, float]:
        """Value stocks: high CAGR/Volatility ratio with positive alpha.

        Uses CAGR/Volatility as a value proxy instead of P/B ratios, because:
        - P/B requires live API calls (not available historically in backtests)
        - CAGR/Volatility captures "growth at reasonable risk" — the essence of value
        - All inputs come from the screener, so fully reproducible in backtests
        """
        if candidates.empty:
            return {}

        pool = pd.DataFrame(candidates[candidates["Alpha"] > 0])
        pool = pd.DataFrame(pool[~pool["Ticker"].isin(list(exclude))])

        if len(pool) < VALUE_COUNT * 2:
            pool = pd.DataFrame(
                candidates[~candidates["Ticker"].isin(list(exclude))].nlargest(VALUE_COUNT * 4, "Alpha")  # type: ignore[arg-type]
            )

        if pool.empty or "Volatility" not in pool.columns:
            return {}

        # Compute value score: CAGR / Volatility (growth per unit risk)
        pool = pool.copy()
        pool["_value_score"] = pool["CAGR"] / pool["Volatility"].clip(lower=0.01)
        pool = pool.sort_values("_value_score", ascending=False)  # type: ignore[call-overload]

        tickers = apply_sector_cap(pool["Ticker"].tolist())[:VALUE_COUNT]
        if not tickers:
            return {}

        # Quality-proportional weights using alpha
        alphas = {}
        for t in tickers:
            row = pool[pool["Ticker"] == t]
            alphas[t] = float(pd.Series(row["Alpha"]).iloc[0]) if len(row) > 0 else 0

        min_alpha = min(alphas.values())
        shifted = {t: a - min_alpha + 1.0 for t, a in alphas.items()}
        total = sum(shifted.values())
        weights = {t: v / total for t, v in shifted.items()}

        weights = cap_sector_weights(weights)
        return {t: w * capital for t, w in weights.items()}

    def should_rebalance(
        self,
        current_weights: dict[str, float],
        target_weights: dict[str, float],
        days_since_last: int,
    ) -> bool:
        """Rebalance quarterly (90 days)."""
        cfg = get_config()
        return days_since_last >= cfg.gods_plan.rebalance_days
