"""Dhan-backed price layer for the diamond stock engine.

The engine's screener and strategies call `diamond.data.market.download_*`, which use
yfinance. `install()` swaps those for functions that read from a PriceStore filled from Dhan,
so the engine's own screening/allocation code runs unchanged on Dhan data.

Two Dhan quirks are handled here (both found by comparing with yfinance):
  * daily bars are stamped midnight IST but read as UTC, so every date is one day early
  * prices are NOT adjusted for splits / bonuses / demergers. A day where Dhan's move differs
    from yfinance's by more than 15% is treated as an unadjusted corporate action and the
    earlier prices are scaled. If yfinance cannot verify it, the stock is EXCLUDED from the
    screen and listed in `excluded` (never guessed). yfinance is used only for this check.
Dividends are not adjusted (Dhan gives raw prices), which slightly understates high-yield names.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

import dhan_client as dc

HISTORY_YEARS = 3.3  # screener needs ~2y of lookback plus room
JUMP = 0.15


@dataclass
class PriceStore:
    close: pd.DataFrame
    volume: pd.DataFrame
    fetched_at: str
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)

    @property
    def last_date(self) -> pd.Timestamp:
        return self.close.index.max()


def _ist_fix(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.index = (out.index + pd.Timedelta(hours=5, minutes=30)).normalize()
    return out[~out.index.duplicated(keep="last")]


def _fetch_one(symbol: str) -> tuple[str, pd.DataFrame | None]:
    for attempt in range(5):
        df = dc.get_equity_history(symbol, years=HISTORY_YEARS)
        if df is not None and len(df):
            return symbol, _ist_fix(df)
        time.sleep(1.5 * (attempt + 1))  # Dhan answers 429 with no body; back off and retry
    return symbol, None


def _fix_splits(close: pd.DataFrame, warnings: list[str]) -> set[str]:
    """Scale pre-jump prices where Dhan shows a corporate-action jump that yfinance does not.

    Returns tickers that could not be verified (to be excluded from the screen).
    """
    suspects = [t for t in close.columns if t != "^NSEI" and (close[t].pct_change().abs() > JUMP).any()]
    if not suspects:
        return set()
    try:
        import yfinance as yf
    except Exception:
        warnings.append(f"yfinance unavailable: {len(suspects)} names with large moves excluded: {suspects[:8]}")
        return set(suspects)
    unverified: set[str] = set()
    try:  # one batched call; columns are already NSE tickers such as "BEL.NS"
        ys = yf.download(suspects, period="4y", auto_adjust=True, progress=False, group_by="column")["Close"]
        if not hasattr(ys, "columns"):
            ys = ys.to_frame(suspects[0])
        ys.index = pd.to_datetime(ys.index).tz_localize(None).normalize()
    except Exception:
        ys = pd.DataFrame()
    for t in suspects:
        if t not in ys.columns or ys[t].dropna().shape[0] < 100:
            unverified.add(t)
            continue
        rd = close[t].pct_change()
        ry = ys[t].pct_change().reindex(rd.index)
        for day in rd.index[(rd.abs() > JUMP)]:
            yr = ry.get(day)
            if yr is None or pd.isna(yr):
                unverified.add(t)
                continue
            if abs(rd[day] - yr) > JUMP:
                factor = (1 + rd[day]) / (1 + yr)
                close.loc[close.index < day, t] = close.loc[close.index < day, t] * factor
    if unverified:
        warnings.append(f"large moves could not be verified, excluded from screen: {sorted(unverified)[:12]}")
    return unverified


def fetch_all(symbols: list[str], workers: int = 2) -> PriceStore:
    """Fetch daily history for bare NSE symbols (no .NS) + the Nifty index from Dhan."""
    # Load the instrument list once, single-threaded: on a fresh machine two workers would both
    # download and write the 35 MB scrip-master file at the same time and one reads it half-written.
    dc._equity_ids()
    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(_fetch_one, symbols))
    ok = {f"{s}.NS": df for s, df in results if df is not None}
    missing = [f"{s}.NS" for s, df in results if df is None]
    if not ok:
        raise RuntimeError("Dhan returned no price history (token expired or Dhan unreachable)")
    close = pd.DataFrame({t: df["Close"] for t, df in ok.items()}).sort_index()
    volume = pd.DataFrame({t: df["Volume"] for t, df in ok.items()}).sort_index()
    nifty = dc.get_index_history("NIFTY", years=HISTORY_YEARS)
    if nifty is not None and len(nifty):
        close["^NSEI"] = _ist_fix(nifty)["Close"]
    warnings: list[str] = []
    excluded = sorted(_fix_splits(close, warnings))
    close = close.drop(columns=excluded, errors="ignore")
    volume = volume.drop(columns=excluded, errors="ignore")
    return PriceStore(close, volume, datetime.now().isoformat(timespec="seconds"), missing, warnings, excluded)


def live_prices(symbols: list[str]) -> dict[str, float]:
    """Current last-traded price per bare NSE symbol from Dhan's batch quote endpoint (1 call)."""
    ids = dc._equity_ids()
    by_id = {int(ids[s]): s for s in symbols if s in ids}
    quotes = dc.get_batch_quote(list(by_id), "NSE_EQ")
    return {by_id[i]: q["price"] for i, q in quotes.items() if i in by_id and q.get("price", 0) > 0}


def install(store: PriceStore) -> None:
    """Point the engine's data layer at this store."""
    from diamond.data import market

    def _slice(df: pd.DataFrame, tickers: list[str], start, end) -> pd.DataFrame:
        cols = [t for t in tickers if t in df.columns]
        return df.loc[(df.index >= start) & (df.index < end), cols].dropna(how="all")

    def daterange(tickers, start, end, retries=3, use_cache=True):
        return _slice(store.close, tickers, start, end)

    def ohlcv(tickers, start, end, *a, **k):
        return _slice(store.close, tickers, start, end), _slice(store.volume, tickers, start, end)

    def prices(tickers, period_days=None, retries=3, use_cache=True, end_date=None):
        period_days = period_days or 504
        end = pd.Timestamp(end_date) if end_date else store.last_date + pd.Timedelta(days=1)
        return _slice(store.close, tickers, end - pd.Timedelta(days=period_days), end)

    def single(ticker, period_days=None):
        return prices([ticker], period_days).iloc[:, 0].dropna()

    market.download_prices_daterange = daterange
    market.download_ohlcv_daterange = ohlcv
    market.download_prices = prices
    market.download_single = single
