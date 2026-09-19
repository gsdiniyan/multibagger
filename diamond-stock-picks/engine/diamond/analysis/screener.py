"""Market screener - calculates Alpha, Beta, CAGR, Volatility, Hurst for all stocks.

Screens the NSE 500 universe against the Nifty 50 benchmark.
Results cached to CSV with configurable TTL (default 24h).
"""

from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd

from diamond.config import get_config
from diamond.data import market, universe

logger = logging.getLogger(__name__)

ANN_FACTOR = 252  # Trading days per year


def _hurst_exponent(prices: np.ndarray, max_lag: int = 100) -> float:
    """Calculate Hurst exponent via rescaled range method.

    H > 0.5: trending (persistent)
    H = 0.5: random walk
    H < 0.5: mean-reverting (anti-persistent)
    """
    if len(prices) < max_lag + 10:
        return 0.5  # Default: random walk

    lags = range(2, min(max_lag, len(prices) // 2))
    tau = []
    valid_lags = []

    for lag in lags:
        diff = prices[lag:] - prices[:-lag]
        std_val = np.std(diff)
        if std_val > 0:
            tau.append(np.sqrt(std_val))
            valid_lags.append(lag)

    if len(valid_lags) < 3:
        return 0.5

    try:
        poly = np.polyfit(np.log(valid_lags), np.log(tau), 1)
        return float(np.clip(poly[0] * 2.0, 0.0, 1.0))
    except (ValueError, np.linalg.LinAlgError):
        return 0.5


def _compute_metrics(
    stock_prices: pd.Series,
    market_returns: pd.Series,
    market_ann_return: float,
    rf: float,
) -> dict:
    """Compute all screening metrics for a single stock."""
    stock_returns = stock_prices.pct_change().dropna()

    # Align dates
    aligned = pd.DataFrame({"stock": stock_returns, "market": market_returns}).dropna()
    if len(aligned) < 60:
        return {}

    s_ret = aligned["stock"]
    m_ret = aligned["market"]

    # Beta: cov(stock, market) / var(market)
    var_m = float(m_ret.var())
    cov_sm = float(pd.Series(s_ret).cov(pd.Series(m_ret)))
    beta = float(cov_sm / var_m) if var_m > 0 else 1.0

    # Alpha (CAPM): annualized stock return - (rf + beta * (market_return - rf))
    stock_ann_return = (1 + float(s_ret.mean())) ** ANN_FACTOR - 1
    alpha = stock_ann_return - (rf + beta * (market_ann_return - rf))

    # CAGR
    idx = stock_prices.index
    days = (idx[-1] - idx[0]).days  # type: ignore[union-attr]
    if days > 0 and stock_prices.iloc[0] > 0:
        cagr = (stock_prices.iloc[-1] / stock_prices.iloc[0]) ** (365.25 / days) - 1
    else:
        cagr = 0.0

    # Volatility (annualized)
    volatility = float(s_ret.std() * np.sqrt(ANN_FACTOR))

    # Hurst exponent
    hurst = _hurst_exponent(np.asarray(stock_prices))

    return {
        "Alpha": round(float(alpha), 4),
        "Beta": round(float(beta), 4),
        "CAGR": round(float(cagr), 4),
        "Volatility": round(float(volatility), 4),
        "Hurst": round(float(hurst), 4),
    }


def screen(
    tickers: list[str] | None = None,
    force: bool = False,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Screen stocks and return DataFrame with quantitative metrics.

    Args:
        tickers: Subset of tickers to screen. Defaults to full NSE 500.
        force: Bypass cache and re-screen.
        end_date: If set, only use price data up to this date (YYYY-MM-DD).
                  Used by backtesting to prevent look-ahead bias.

    Returns:
        DataFrame with columns: Ticker, Alpha, Beta, CAGR, Volatility, Hurst, Sector
    """
    cfg = get_config()

    # When end_date is set (backtest mode), skip cache entirely to avoid
    # cross-window contamination. Each window gets a fresh screen.
    if end_date is None:
        cache_file = cfg.data_dir / "universe_cache.csv"
        if not force and cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < cfg.cache.screener_ttl:
                logger.info(f"Using cached screener results ({age / 3600:.1f}h old)")
                df = pd.read_csv(cache_file)
                if tickers:
                    df = pd.DataFrame(df[df["Ticker"].isin(list(tickers))])
                return df

    # Resolve ticker list
    all_tickers = tickers or universe.get_screening_universe()
    benchmark = cfg.passive.index_ticker
    rf = cfg.screener.risk_free_rate
    lookback = cfg.screener.lookback_days

    # Calculate date range for data download
    if end_date is not None:
        from datetime import datetime, timedelta

        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        start_dt = end_dt - timedelta(days=int(lookback * 365.25 / 252))
        data_start = start_dt.strftime("%Y-%m-%d")
        data_end = end_date
        logger.info(f"Screening {len(all_tickers)} stocks as-of {end_date} ({data_start} -> {data_end})")
    else:
        data_start = None
        data_end = None
        logger.info(f"Screening {len(all_tickers)} stocks against {benchmark}")

    # Download benchmark
    try:
        if data_start and data_end:
            bench_df = market.download_prices_daterange([benchmark], start=data_start, end=data_end, use_cache=False)
            bench_prices = bench_df.iloc[:, 0].dropna()
        else:
            bench_prices = market.download_single(benchmark)
    except Exception as e:
        logger.error(f"Failed to download benchmark {benchmark}: {e}")
        return pd.DataFrame()

    bench_returns = bench_prices.pct_change().dropna()
    market_ann_return = float((1 + bench_returns.mean()) ** ANN_FACTOR - 1)

    # Screen each stock
    results = []
    failed = 0

    for i, ticker in enumerate(all_tickers):
        if (i + 1) % 50 == 0:
            logger.info(f"Progress: {i + 1}/{len(all_tickers)} stocks screened")

        try:
            if data_start and data_end:
                stock_df = market.download_prices_daterange(
                    [ticker],
                    start=data_start,
                    end=data_end,
                    use_cache=False,
                )
                stock_prices = stock_df.iloc[:, 0].dropna()
            else:
                stock_prices = market.download_single(ticker)

            if len(stock_prices) < 60:
                logger.debug(f"{ticker}: insufficient data ({len(stock_prices)} days)")
                failed += 1
                continue

            metrics = _compute_metrics(stock_prices, bench_returns, market_ann_return, rf)
            if not metrics:
                failed += 1
                continue

            metrics["Ticker"] = ticker
            metrics["Sector"] = universe.get_sector(ticker)
            results.append(metrics)

        except Exception as e:
            logger.debug(f"{ticker}: error - {e}")
            failed += 1

    if not results:
        logger.error("Screening produced no results")
        return pd.DataFrame()

    df = pd.DataFrame(results)
    # Reorder columns
    cols = ["Ticker", "Alpha", "Beta", "CAGR", "Volatility", "Hurst", "Sector"]
    df = df[[c for c in cols if c in df.columns]]
    df = df.sort_values("Alpha", ascending=False).reset_index(drop=True)  # type: ignore[call-overload]

    # Only cache when not in backtest mode
    if end_date is None:
        cache_file = cfg.data_dir / "universe_cache.csv"
        df.to_csv(cache_file, index=False)
        logger.info(f"Screened {len(df)} stocks ({failed} failed). Cached to {cache_file}")
    else:
        logger.info(f"Screened {len(df)} stocks ({failed} failed) as-of {end_date}")

    return df
