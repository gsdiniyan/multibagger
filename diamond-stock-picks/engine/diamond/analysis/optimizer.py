"""Portfolio optimization - Monte Carlo and inverse-volatility weighting.

Pure functions operating on return data. No side effects.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from diamond.config import get_config

logger = logging.getLogger(__name__)


def monte_carlo_optimize(
    returns_df: pd.DataFrame,
    num_simulations: int = 100_000,
    max_weight: float | None = None,
) -> dict[str, float]:
    """Find optimal portfolio weights via Monte Carlo Sharpe maximization.

    Args:
        returns_df: DataFrame of daily returns, one column per ticker.
        num_simulations: Number of random portfolios to simulate.
        max_weight: Maximum weight per position (e.g., 0.10 for 10%).

    Returns:
        {ticker: weight} dict where weights sum to 1.0.
    """
    cfg = get_config()
    rf = cfg.screener.risk_free_rate

    if max_weight is None:
        max_weight = cfg.portfolio.max_position_weight

    # Clean data
    returns_df = returns_df.dropna(axis=1, how="all").dropna()
    tickers = list(returns_df.columns)
    n = len(tickers)

    if n == 0:
        return {}
    if n == 1:
        return {tickers[0]: 1.0}
    if len(returns_df) < 60:
        logger.warning(f"Insufficient data ({len(returns_df)} days), using equal weights")
        return {t: 1.0 / n for t in tickers}

    mean_returns = np.asarray(returns_df.mean())
    cov_matrix = np.asarray(returns_df.cov())

    # Handle NaN in covariance matrix
    if np.any(np.isnan(cov_matrix)):
        cov_matrix = np.nan_to_num(cov_matrix, nan=0.0)

    # Generate random weights
    rng = np.random.default_rng(42)
    weights = rng.random((num_simulations, n))

    # Apply max weight constraint via iterative clipping
    if max_weight < 1.0:
        for _ in range(50):
            weights = np.clip(weights, 0, max_weight)
            row_sums = weights.sum(axis=1, keepdims=True)
            row_sums[row_sums == 0] = 1  # Prevent div-by-zero
            weights = weights / row_sums

            if np.all(weights <= max_weight + 1e-6):
                break

    # Normalize
    row_sums = weights.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0] = 1
    weights = weights / row_sums

    # Vectorized portfolio metrics
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        port_returns = weights @ mean_returns * 252
        # Einstein summation for portfolio volatility: sqrt(w' * cov * w)
        port_vol = np.sqrt(np.einsum("ij,jk,ik->i", weights, cov_matrix, weights) * 252)
        port_vol = np.maximum(port_vol, 1e-10)  # Prevent div-by-zero

        sharpe = (port_returns - rf) / port_vol
        # Replace any NaN/Inf sharpe values with -Inf so they won't be selected
        sharpe = np.nan_to_num(sharpe, nan=-np.inf, posinf=-np.inf, neginf=-np.inf)

    # Best portfolio
    best_idx = np.argmax(sharpe)
    best_weights = weights[best_idx]

    result = {t: round(float(w), 6) for t, w in zip(tickers, best_weights) if w > 0.001}

    # Renormalize after filtering dust
    total = sum(result.values())
    if total > 0:
        result = {t: w / total for t, w in result.items()}

    logger.info(
        f"Monte Carlo: {num_simulations} sims, {n} assets, "
        f"best Sharpe={sharpe[best_idx]:.3f}, return={port_returns[best_idx]:.1%}, "
        f"vol={port_vol[best_idx]:.1%}"
    )

    return result


def inverse_volatility_weights(
    returns_df: pd.DataFrame,
    max_weight: float | None = None,
) -> dict[str, float]:
    """Calculate inverse-volatility weighted allocation.

    Lower volatility stocks get higher weights. Simple, robust, and
    historically effective for defensive portfolios.

    Args:
        returns_df: DataFrame of daily returns, one column per ticker.
        max_weight: Maximum weight per position.

    Returns:
        {ticker: weight} dict where weights sum to 1.0.
    """
    cfg = get_config()
    if max_weight is None:
        max_weight = cfg.portfolio.max_position_weight

    returns_df = returns_df.dropna(axis=1, how="all").dropna()
    tickers = list(returns_df.columns)

    if not tickers:
        return {}
    if len(tickers) == 1:
        return {tickers[0]: 1.0}

    # Annualized volatility
    vols = returns_df.std() * np.sqrt(252)
    vols = vols.replace(0, np.nan).dropna()

    if vols.empty:
        return {t: 1.0 / len(tickers) for t in tickers}

    # Inverse volatility
    inv_vol = 1.0 / vols
    weights = inv_vol / inv_vol.sum()

    # Apply max weight cap and renormalize
    if max_weight < 1.0:
        for _ in range(20):
            weights = weights.clip(upper=max_weight)
            total = weights.sum()
            if total > 0:
                weights = weights / total
            if weights.max() <= max_weight + 1e-6:
                break

    return {t: round(float(w), 6) for t, w in weights.items() if w > 0.001}


def equal_weights(tickers: list[str]) -> dict[str, float]:
    """Simple equal-weight allocation."""
    if not tickers:
        return {}
    w = 1.0 / len(tickers)
    return {t: w for t in tickers}


def market_cap_weights(market_caps: dict[str, float]) -> dict[str, float]:
    """Market-cap weighted allocation.

    Args:
        market_caps: {ticker: market_cap_value}

    Returns:
        {ticker: weight} dict where weights sum to 1.0.
    """
    caps = {t: c for t, c in market_caps.items() if c and c > 0}
    if not caps:
        return {}

    total = sum(caps.values())
    return {t: c / total for t, c in caps.items()}
