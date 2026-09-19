"""Market data fetcher wrapping yfinance with retry logic and file cache.

Handles yfinance v2 MultiIndex columns transparently.
Also provides NSE market hours utilities.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

from diamond.config import get_config
from diamond.exceptions import EmptyDataError, PriceDownloadError

logger = logging.getLogger(__name__)

# --- NSE Market Hours ---

IST = ZoneInfo("Asia/Kolkata")

# NSE trading hours
NSE_OPEN_HOUR, NSE_OPEN_MINUTE = 9, 15
NSE_CLOSE_HOUR, NSE_CLOSE_MINUTE = 15, 30

# NSE holidays for 2026
# Source: https://www.nseindia.com/
NSE_HOLIDAYS_2026: set[date] = {
    date(2026, 1, 26),  # Republic Day
    date(2026, 2, 17),  # Mahashivratri (tentative)
    date(2026, 3, 10),  # Holi
    date(2026, 3, 30),  # Id-ul-Fitr (Eid, tentative)
    date(2026, 3, 31),  # Id-ul-Fitr (Eid, tentative)
    date(2026, 4, 2),  # Ram Navami
    date(2026, 4, 3),  # Good Friday
    date(2026, 4, 14),  # Dr. Ambedkar Jayanti
    date(2026, 5, 1),  # Maharashtra Day
    date(2026, 5, 25),  # Buddha Purnima
    date(2026, 6, 6),  # Eid-ul-Adha (Bakri Id, tentative)
    date(2026, 7, 6),  # Muharram
    date(2026, 8, 15),  # Independence Day
    date(2026, 8, 18),  # Janmashtami (tentative)
    date(2026, 9, 4),  # Milad-un-Nabi (tentative)
    date(2026, 10, 2),  # Mahatma Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 9),  # Diwali (Laxmi Puja)
    date(2026, 11, 10),  # Diwali (Balipratipada)
    date(2026, 11, 30),  # Guru Nanak Jayanti
    date(2026, 12, 25),  # Christmas
}


def is_market_open(now: datetime | None = None) -> bool:
    """Check if NSE is currently open for trading.

    Args:
        now: Override current time (for testing). Must be timezone-aware or naive
             (will be treated as IST).

    Returns:
        True if market is open right now.
    """
    if now is None:
        now = datetime.now(IST)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)

    # Weekend check (Monday=0 ... Sunday=6)
    if now.weekday() >= 5:
        return False

    # Holiday check
    if now.date() in NSE_HOLIDAYS_2026:
        return False

    # Trading hours check
    market_open = now.replace(hour=NSE_OPEN_HOUR, minute=NSE_OPEN_MINUTE, second=0, microsecond=0)
    market_close = now.replace(hour=NSE_CLOSE_HOUR, minute=NSE_CLOSE_MINUTE, second=0, microsecond=0)

    return market_open <= now <= market_close


def next_market_open(now: datetime | None = None) -> datetime:
    """Return the next datetime when NSE opens.

    If market is currently open, returns the *next* opening (tomorrow or later).

    Args:
        now: Override current time (for testing).

    Returns:
        Timezone-aware datetime in IST of next market open.
    """
    if now is None:
        now = datetime.now(IST)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)

    # Start from today's open time
    candidate = now.replace(hour=NSE_OPEN_HOUR, minute=NSE_OPEN_MINUTE, second=0, microsecond=0)

    # If we're past today's open, start from tomorrow
    if now >= candidate:
        candidate += timedelta(days=1)

    # Skip weekends and holidays
    max_search = 30  # safety limit
    for _ in range(max_search):
        if candidate.weekday() < 5 and candidate.date() not in NSE_HOLIDAYS_2026:
            return candidate
        candidate += timedelta(days=1)

    # Fallback (should never reach here)
    return candidate


def _cache_path(tickers: list[str], period_days: int) -> Path:
    """Generate a deterministic cache file path for a set of tickers."""
    key = hashlib.md5(f"{sorted(tickers)}_{period_days}".encode()).hexdigest()[:12]
    return get_config().cache_dir / "prices" / f"{key}.csv"


def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten yfinance v2 MultiIndex columns to single level.

    yf.download() returns columns like ('Close', 'RELIANCE.NS') in v2.
    This flattens to just 'Close' for single ticker, or keeps ticker suffix for multi.
    """
    if isinstance(df.columns, pd.MultiIndex) and df.columns.nlevels == 2:
        # Single ticker: drop ticker level
        tickers = df.columns.get_level_values(1).unique()
        if len(tickers) == 1:
            df.columns = df.columns.get_level_values(0)
        else:
            # Multi-ticker: join levels
            df.columns = [f"{col[0]}_{col[1]}" for col in df.columns]
    return df


def download_prices(
    tickers: list[str],
    period_days: int | None = None,
    retries: int = 3,
    use_cache: bool = True,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Download adjusted close prices for tickers.

    Args:
        tickers: List of Yahoo Finance tickers (e.g., ['RELIANCE.NS', 'TCS.NS'])
        period_days: Number of calendar days of history. Defaults to config lookback.
        retries: Number of retry attempts on failure.
        use_cache: Whether to use file cache.
        end_date: If set (YYYY-MM-DD), return only the ``period_days`` ending at this
            date instead of ending today. Used by backtests to prevent look-ahead.

    Returns:
        DataFrame with DatetimeIndex and one column per ticker (adjusted close).
    """
    cfg = get_config()
    if period_days is None:
        period_days = cfg.screener.lookback_days

    if end_date is not None:
        from datetime import datetime, timedelta

        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        start = (end_dt - timedelta(days=period_days)).strftime("%Y-%m-%d")
        return download_prices_daterange(tickers, start=start, end=end_date, retries=retries, use_cache=use_cache)

    # Check cache
    cache_file = _cache_path(tickers, period_days)
    if use_cache and cache_file.exists():
        age_seconds = time.time() - cache_file.stat().st_mtime
        if age_seconds < cfg.cache.prices_ttl:
            df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
            return df

    # Download with retries
    period_str = f"{period_days}d"
    last_error = None

    for attempt in range(retries):
        try:
            raw = yf.download(
                tickers,
                period=period_str,
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            if raw is None or raw.empty:
                raise EmptyDataError(tickers)

            df = _flatten_columns(raw)

            # Extract Close columns only
            if len(tickers) == 1:
                if "Close" in df.columns:
                    df = pd.DataFrame(df[["Close"]]).rename(columns={"Close": tickers[0]})
                else:
                    df = df.iloc[:, :1]
                    df.columns = pd.Index([tickers[0]])
            else:
                close_cols = [c for c in df.columns if str(c).startswith("Close_")]
                if close_cols:
                    df = df[close_cols]
                    df.columns = pd.Index([str(c).replace("Close_", "") for c in close_cols])

            df = pd.DataFrame(df.dropna(how="all"))

            # Validate for suspicious price jumps
            validate_prices(df)

            # Cache result
            if use_cache:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                df.to_csv(cache_file)

            return df

        except Exception as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(2**attempt)

    raise PriceDownloadError(tickers, str(last_error))


def download_prices_daterange(
    tickers: list[str],
    start: str,
    end: str,
    retries: int = 3,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Download adjusted close prices for a specific date range.

    Args:
        tickers: List of Yahoo Finance tickers.
        start: Start date (YYYY-MM-DD).
        end: End date (YYYY-MM-DD).
        retries: Number of retry attempts on failure.
        use_cache: Whether to use file cache.

    Returns:
        DataFrame with DatetimeIndex and one column per ticker (adjusted close).
    """
    cfg = get_config()

    # Cache key includes date range
    cache_key = hashlib.md5(f"{sorted(tickers)}_{start}_{end}".encode()).hexdigest()[:12]
    cache_file = cfg.cache_dir / "prices" / f"range_{cache_key}.csv"

    if use_cache and cache_file.exists():
        age_seconds = time.time() - cache_file.stat().st_mtime
        if age_seconds < cfg.cache.prices_ttl:
            df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
            return df

    last_error = None

    for attempt in range(retries):
        try:
            raw = yf.download(
                tickers,
                start=start,
                end=end,
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            if raw is None or raw.empty:
                raise EmptyDataError(tickers, f"{start} to {end}")

            df = _flatten_columns(raw)

            # Extract Close columns only
            if len(tickers) == 1:
                if "Close" in df.columns:
                    df = pd.DataFrame(df[["Close"]]).rename(columns={"Close": tickers[0]})
                else:
                    df = df.iloc[:, :1]
                    df.columns = pd.Index([tickers[0]])
            else:
                close_cols = [c for c in df.columns if str(c).startswith("Close_")]
                if close_cols:
                    df = df[close_cols]
                    df.columns = pd.Index([str(c).replace("Close_", "") for c in close_cols])

            df = pd.DataFrame(df.dropna(how="all"))

            # Validate for suspicious price jumps
            validate_prices(df)

            if use_cache:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                df.to_csv(cache_file)

            return df

        except Exception as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(2**attempt)

    raise PriceDownloadError(tickers, f"{start}-{end}: {last_error}")


def _extract_column(
    flat: pd.DataFrame,
    prefix: str,
    tickers: list[str],
) -> pd.DataFrame:
    """Extract a single data column (Close, Volume, etc.) from flattened yfinance output."""
    if len(tickers) == 1:
        if prefix in flat.columns:
            return pd.DataFrame(flat[[prefix]].dropna(how="all")).rename(columns={prefix: tickers[0]})
        return pd.DataFrame()
    cols = [c for c in flat.columns if str(c).startswith(f"{prefix}_")]
    if cols:
        result = flat[cols].copy()
        result.columns = pd.Index([str(c).replace(f"{prefix}_", "") for c in cols])
        return pd.DataFrame(result.dropna(how="all"))
    return pd.DataFrame()


def download_ohlcv_daterange(
    tickers: list[str],
    start: str,
    end: str,
    retries: int = 3,
    use_cache: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Download close prices AND volume for a date range.

    Returns:
        (prices_df, volume_df) — both with DatetimeIndex and one column per ticker.
    """
    cfg = get_config()

    cache_key = hashlib.md5(f"{sorted(tickers)}_{start}_{end}_ohlcv".encode()).hexdigest()[:12]
    cache_price = cfg.cache_dir / "prices" / f"range_{cache_key}_close.csv"
    cache_vol = cfg.cache_dir / "prices" / f"range_{cache_key}_volume.csv"

    if use_cache and cache_price.exists() and cache_vol.exists():
        age_seconds = time.time() - cache_price.stat().st_mtime
        if age_seconds < cfg.cache.prices_ttl:
            prices = pd.read_csv(cache_price, index_col=0, parse_dates=True)
            volume = pd.read_csv(cache_vol, index_col=0, parse_dates=True)
            return prices, volume

    last_error = None
    for attempt in range(retries):
        try:
            raw = yf.download(
                tickers,
                start=start,
                end=end,
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            if raw is None or raw.empty:
                raise EmptyDataError(tickers, f"{start} to {end}")

            flat = _flatten_columns(raw)
            prices = _extract_column(flat, "Close", tickers)
            volume = _extract_column(flat, "Volume", tickers)

            if use_cache:
                cache_price.parent.mkdir(parents=True, exist_ok=True)
                prices.to_csv(cache_price)
                volume.to_csv(cache_vol)

            return prices, volume

        except Exception as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(2**attempt)

    raise PriceDownloadError(tickers, f"OHLCV {start}-{end}: {last_error}")


def download_single(ticker: str, period_days: int | None = None) -> pd.Series:
    """Download adjusted close for a single ticker. Returns Series."""
    df = download_prices([ticker], period_days=period_days)
    return df.iloc[:, 0].dropna()


def get_market_cap(ticker: str) -> float | None:
    """Fetch current market cap for a ticker. Returns None on failure."""
    try:
        info = yf.Ticker(ticker).info
        return info.get("marketCap")
    except Exception:
        return None


def get_stock_info(ticker: str, retries: int = 2) -> dict:
    """Fetch stock info dict with retry logic."""
    for attempt in range(retries):
        try:
            return yf.Ticker(ticker).info or {}
        except Exception:
            if attempt < retries - 1:
                time.sleep(1)
    return {}


def calculate_returns(prices: pd.Series) -> pd.Series:
    """Calculate daily log returns from a price series."""
    return np.log(prices / prices.shift(1)).dropna()


# --- Price Validation ---


def validate_prices(prices: pd.DataFrame) -> pd.DataFrame:
    """Flag price outliers that are >3 standard deviations from the 20-day rolling mean.

    Logs a WARNING for each flagged price but does not remove them (fail-open).

    Args:
        prices: DataFrame with DatetimeIndex and one column per ticker.

    Returns:
        The same DataFrame, unmodified. Outliers are logged only.
    """
    for col in prices.columns:
        series = prices[col].dropna()
        if len(series) < 21:
            continue
        rolling_mean = series.rolling(window=20).mean()
        rolling_std = series.rolling(window=20).std()
        deviation = (series - rolling_mean).abs()
        outliers = deviation > 3 * rolling_std
        # Skip NaN rows from rolling window warmup
        outliers = outliers.fillna(False)
        if outliers.any():
            date_idx = series.index[outliers]  # type: ignore[arg-type]
            for dt in list(date_idx):  # type: ignore[arg-type]
                logger.warning(
                    "Price outlier detected: %s on %s = %.2f (rolling mean=%.2f, 3*std=%.2f)",
                    col,
                    dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt),  # type: ignore[union-attr]
                    series[dt],
                    rolling_mean[dt],
                    3 * rolling_std[dt],
                )
    return prices


def sanity_check_price(ticker: str, price: float, last_known: float) -> bool:
    """Check if a price move is plausible (not a data error).

    Returns False if price moved >50% in a single day from last_known,
    which almost certainly indicates bad data rather than a real move.

    Args:
        ticker: Ticker symbol (for logging).
        price: The new price to check.
        last_known: The previous known price.

    Returns:
        True if the price looks reasonable, False if it's a likely data error.
    """
    if last_known <= 0:
        return True  # No basis for comparison
    change_pct = abs(price - last_known) / last_known
    if change_pct > 0.50:
        logger.warning(
            "Price sanity check failed: %s moved %.1f%% in one day (%.2f -> %.2f) — likely data error",
            ticker,
            change_pct * 100,
            last_known,
            price,
        )
        return False
    return True
