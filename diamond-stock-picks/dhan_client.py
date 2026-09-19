"""
Minimal read-only Dhan REST client for live NSE daily price history --
equities AND indices (NIFTY, INDIA VIX, BANKNIFTY, etc.).

Self-contained, same pattern as Project Options/Multibagger/src/dhan_client.py
(built and verified separately for that project). Loads DHAN_CLIENT_ID /
DHAN_ACCESS_TOKEN from the .env file next to this script.

Scrip master column names and index security IDs verified live on
2026-09-14: EXCH_ID, INSTRUMENT (EQUITY / INDEX), SEGMENT, SECURITY_ID,
UNDERLYING_SYMBOL, SYMBOL_NAME. Indices use exchangeSegment "IDX_I" for the
historical-data call; equities use "NSE_EQ" -- confirmed against the real
API for both NIFTY (security_id 13) and INDIA VIX (security_id 21).
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import pandas as pd
import requests

_PROJECT_ROOT = Path(__file__).resolve().parent
_ENV_PATH = _PROJECT_ROOT / ".env"
_MASTER_CACHE_PATH = _PROJECT_ROOT / "dhan_scrip_master.csv"
_MASTER_CACHE_TTL_HOURS = 24

SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
DHAN_BASE_URL = "https://api.dhan.co/v2"
IST = timezone(timedelta(hours=5, minutes=30))


def _load_env_file() -> None:
    if not _ENV_PATH.exists():
        return
    with open(_ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if k and v and k not in os.environ:
                os.environ[k] = v


_load_env_file()


def _headers() -> dict:
    client_id = os.environ.get("DHAN_CLIENT_ID")
    token = os.environ.get("DHAN_ACCESS_TOKEN")
    if not client_id or not token:
        raise RuntimeError(
            "DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set -- fill in .env next to this script."
        )
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "access-token": token,
        "client-id": client_id,
    }


@lru_cache(maxsize=1)
def _master() -> pd.DataFrame:
    use_cache = (
        _MASTER_CACHE_PATH.exists()
        and (time.time() - _MASTER_CACHE_PATH.stat().st_mtime) < _MASTER_CACHE_TTL_HOURS * 3600
    )
    if use_cache:
        return pd.read_csv(_MASTER_CACHE_PATH, low_memory=False)
    r = requests.get(SCRIP_MASTER_URL, timeout=60)
    r.raise_for_status()
    _MASTER_CACHE_PATH.write_bytes(r.content)
    return pd.read_csv(_MASTER_CACHE_PATH, low_memory=False)


@lru_cache(maxsize=1)
def _equity_ids() -> dict[str, str]:
    df = _master()
    eq = df[(df["EXCH_ID"] == "NSE") & (df["INSTRUMENT"] == "EQUITY") & (df["SERIES"] == "EQ")]
    return dict(zip(eq["UNDERLYING_SYMBOL"].astype(str).str.upper(), eq["SECURITY_ID"].astype(str)))


@lru_cache(maxsize=1)
def _index_ids() -> dict[str, str]:
    """NSE and BSE index security ids by name (BSE carries SENSEX). Built
    BSE first, NSE second, so an NSE index wins if a name ever exists on
    both. Indices use exchange segment "IDX_I" for history and option-chain
    calls whichever exchange they belong to."""
    df = _master()
    out: dict[str, str] = {}
    for exch in ("BSE", "NSE"):
        idx = df[(df["EXCH_ID"] == exch) & (df["INSTRUMENT"] == "INDEX")]
        out.update(zip(idx["UNDERLYING_SYMBOL"].astype(str).str.upper(), idx["SECURITY_ID"].astype(str)))
    return out


def index_lot_size(symbol: str) -> int | None:
    """Current lot size of an index's options, from the scrip master (NSE or
    BSE): the row with the nearest future expiry. None if not found."""
    df = _master()
    rows = df[(df["INSTRUMENT"] == "OPTIDX") & (df["UNDERLYING_SYMBOL"].astype(str).str.upper() == symbol.upper())].copy()
    rows["_exp"] = pd.to_datetime(rows["SM_EXPIRY_DATE"], errors="coerce")
    rows = rows.dropna(subset=["_exp"])
    rows = rows[rows["_exp"].dt.date >= date.today()].sort_values("_exp")
    return int(rows.iloc[0]["LOT_SIZE"]) if not rows.empty else None


def _fetch_historical(security_id: str, exchange_segment: str, instrument: str, years: float) -> pd.DataFrame | None:
    to_date = date.today()
    from_date = to_date - timedelta(days=int(years * 365) + 10)
    payload = {
        "securityId": security_id,
        "exchangeSegment": exchange_segment,
        "instrument": instrument,
        "expiryCode": 0,
        "fromDate": from_date.strftime("%Y-%m-%d"),
        "toDate": to_date.strftime("%Y-%m-%d"),
    }
    try:
        r = requests.post(f"{DHAN_BASE_URL}/charts/historical", json=payload, headers=_headers(), timeout=25)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    if not isinstance(data, dict) or not data.get("open"):
        return None
    df = pd.DataFrame({
        "Date": pd.to_datetime(data["timestamp"], unit="s", errors="coerce"),
        "Open": data["open"], "High": data["high"], "Low": data["low"],
        "Close": data["close"], "Volume": data["volume"],
    }).dropna(subset=["Date"])
    df = df.set_index("Date").sort_index()
    # Dhan's historical endpoint can return an exact-duplicate row for a
    # given timestamp (confirmed live for TMPV around its Oct-2025 listing
    # date -- a genuine data-quality glitch, not a parsing bug here).
    # Downstream code assumes a unique DatetimeIndex, so dedupe defensively.
    return df[~df.index.duplicated(keep="first")]


def get_equity_history(symbol: str, years: float = 3.0) -> pd.DataFrame | None:
    """Daily OHLCV for a bare NSE equity symbol (e.g. 'RELIANCE')."""
    security_id = _equity_ids().get(symbol.upper())
    if security_id is None:
        return None
    return _fetch_historical(security_id, "NSE_EQ", "EQUITY", years)


def get_index_history(symbol: str, years: float = 3.0) -> pd.DataFrame | None:
    """Daily OHLCV for an NSE index (e.g. 'NIFTY', 'INDIA VIX', 'BANKNIFTY')."""
    security_id = _index_ids().get(symbol.upper())
    if security_id is None:
        return None
    return _fetch_historical(security_id, "IDX_I", "INDEX", years)


# ----------------------------------------------------------------------
# Option chain helpers -- indices AND individual stocks
# ----------------------------------------------------------------------
def resolve_option_underlying(underlying: str) -> tuple[int, str]:
    """
    Returns (security_id, exchange_segment) for an option-chain call,
    whichever kind of underlying this is.

    Indices (NIFTY/BANKNIFTY/FINNIFTY/...) use their own INDEX security_id
    with segment "IDX_I", and list BOTH weekly and monthly expiries.
    Individual stocks use their EQUITY security_id with segment "NSE_FNO"
    -- confirmed live against RELIANCE (security_id 2885): its option
    chain lists only 3 expiries, all monthly (2026-09-29, 2026-10-27,
    2026-11-23) -- stocks never get a weekly series.
    """
    idx_id = _index_ids().get(underlying.upper())
    if idx_id is not None:
        return int(idx_id), "IDX_I"
    eq_id = _equity_ids().get(underlying.upper())
    if eq_id is not None:
        return int(eq_id), "NSE_FNO"
    raise ValueError(f"{underlying}: not found as an NSE index or F&O-eligible equity in Dhan's scrip master.")


def get_expiry_list(underlying: str) -> list[str]:
    """All listed expiry dates (YYYY-MM-DD strings, ascending) for an
    underlying's option chain -- works for both indices and individual
    stocks (see resolve_option_underlying). Indices list weekly + monthly
    expiries; stocks list monthly only."""
    security_id, segment = resolve_option_underlying(underlying)
    payload = {"UnderlyingScrip": security_id, "UnderlyingSeg": segment}
    r = requests.post(f"{DHAN_BASE_URL}/optionchain/expirylist", json=payload, headers=_headers(), timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(f"expirylist call failed: {data}")
    return data["data"]


def get_monthly_expiry(underlying: str, months_ahead: int = 0) -> str:
    """
    The NSE monthly expiry for an index is not a separate listing -- it's
    simply the LAST weekly expiry that falls within a given calendar month
    (confirmed against Dhan's live expiry list: dates land on the same
    weekday every week, e.g. every Tuesday for NIFTY, with each month's
    final Tuesday serving double duty as that month's monthly contract).

    Groups the live expiry list by (year, month), takes the max date in
    each group, and returns the `months_ahead`-th one from today (0 = the
    current/nearest upcoming monthly expiry).
    """
    expiries = get_expiry_list(underlying)
    by_month: dict[tuple[int, int], str] = {}
    for e in expiries:
        d = datetime.strptime(e, "%Y-%m-%d").date()
        key = (d.year, d.month)
        if key not in by_month or e > by_month[key]:
            by_month[key] = e

    monthly_expiries = sorted(by_month.values())
    upcoming = [e for e in monthly_expiries if datetime.strptime(e, "%Y-%m-%d").date() >= date.today()]
    if not upcoming:
        raise RuntimeError(f"No upcoming monthly expiry found for {underlying} in {monthly_expiries}")
    if months_ahead >= len(upcoming):
        raise ValueError(f"Only {len(upcoming)} upcoming monthly expiries available, months_ahead={months_ahead} out of range.")
    return upcoming[months_ahead]


# ----------------------------------------------------------------------
# Futures OI -- for directional_options_scanner.py's OI+price quadrant
# read. Same endpoint/shape as Project Mangalmurti's proven
# DhanClient.get_intraday_oi(), ported to this folder's function-based
# style rather than imported cross-project (separate .env/auth setup).
# ----------------------------------------------------------------------

_TEST_SYMBOL_RE = None  # set below, compiled once


def _build_futures_table(instrument: str) -> pd.DataFrame:
    global _TEST_SYMBOL_RE
    import re
    if _TEST_SYMBOL_RE is None:
        _TEST_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9&-]*$")

    df = _master()
    fut = df[(df["EXCH_ID"] == "NSE") & (df["INSTRUMENT"] == instrument)].copy()
    fut = fut[fut["UNDERLYING_SYMBOL"].astype(str).str.upper().str.match(_TEST_SYMBOL_RE)]
    fut["SM_EXPIRY_DATE"] = pd.to_datetime(fut["SM_EXPIRY_DATE"], errors="coerce")
    fut = fut.dropna(subset=["SM_EXPIRY_DATE"])
    fut = fut[fut["SM_EXPIRY_DATE"].dt.date >= date.today()]
    fut = fut.sort_values("SM_EXPIRY_DATE").drop_duplicates("UNDERLYING_SYMBOL", keep="first")
    return fut.rename(columns={
        "UNDERLYING_SYMBOL": "symbol", "SECURITY_ID": "futures_security_id",
        "SM_EXPIRY_DATE": "expiry", "LOT_SIZE": "lot_size",
    })[["symbol", "futures_security_id", "expiry", "lot_size"]].reset_index(drop=True)


@lru_cache(maxsize=1)
def get_futures_universe() -> pd.DataFrame:
    """One row per tradeable underlying with a live near-month futures
    contract -- real NSE stocks (FUTSTK) plus NIFTY/BANKNIFTY (FUTIDX):
    symbol, the FUTURES contract's OWN security_id (NOT the equity/index
    security_id -- only the futures/options contract itself carries OI),
    which instrument type it is, nearest expiry, lot size.

    Dhan's scrip master carries dummy/test FUTSTK entries (symbols like
    "011NSETEST") alongside real ones -- filtered out here by requiring
    the symbol to be alphabetic (real NSE tickers are, test ones start
    with digits). FUTIDX limited to NIFTY/BANKNIFTY -- the other index
    futures (FINNIFTY, MIDCPNIFTY, NIFTYNXT50) exist in the scrip master
    but aren't part of this scanner's requested scope."""
    stocks = _build_futures_table("FUTSTK")
    stocks["instrument"] = "FUTSTK"
    idx = _build_futures_table("FUTIDX")
    idx = idx[idx["symbol"].isin(["NIFTY", "BANKNIFTY"])].copy()
    idx["instrument"] = "FUTIDX"
    return pd.concat([idx, stocks], ignore_index=True)


def get_intraday_oi_and_price(symbol: str, start: datetime, end: datetime
                               ) -> list[tuple[datetime, float, int]]:
    """(timestamp, close, open_interest) series for a symbol's own
    near-month FUTURES contract over [start, end] -- works for both
    stocks (FUTSTK) and NIFTY/BANKNIFTY (FUTIDX), resolved via
    get_futures_universe(). ONE call gets both price and OI (the
    intraday candle endpoint returns full OHLC regardless of the oi
    flag; oi=True just adds an extra open_interest array to the same
    response) -- deliberately not two separate calls, since this
    scanner's whole point is running across the full F&O universe and
    every avoided call matters at that scale.

    Returns [] if Dhan has no series for this window (honest empty
    result, not a guess -- e.g. before the market opens)."""
    universe = get_futures_universe()
    row = universe[universe["symbol"].str.upper() == symbol.upper()]
    if row.empty:
        return []
    security_id = row.iloc[0]["futures_security_id"]
    instrument = row.iloc[0]["instrument"]

    payload = {
        "securityId": str(security_id), "exchangeSegment": "NSE_FNO", "instrument": instrument,
        "expiryCode": 0, "oi": True,
        "fromDate": start.strftime("%Y-%m-%d %H:%M:%S"), "toDate": end.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        r = requests.post(f"{DHAN_BASE_URL}/charts/intraday", json=payload, headers=_headers(), timeout=25)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    timestamps = data.get("timestamp", [])
    closes = data.get("close", [])
    open_interest = data.get("open_interest", [])
    if not timestamps or not open_interest or not closes:
        return []
    # Dhan's epoch values represent true IST market moments -- converting
    # without a tz argument would silently shift every stamp by 5:30
    # depending on this machine's own local timezone (same bug already
    # documented and fixed for get_5min_candles elsewhere in this project
    # family).
    n = min(len(timestamps), len(closes), len(open_interest))
    series = [(datetime.fromtimestamp(timestamps[i], tz=IST).replace(tzinfo=None),
               float(closes[i]), int(open_interest[i])) for i in range(n)]

    # Dhan's fromDate/toDate are NOT reliably respected -- confirmed live
    # 2026-09-15 requesting 09:15-09:30 for NIFTY and getting the full
    # day back through 15:39 instead. Without this filter, callers taking
    # series[-1] as "the 9:30 snapshot" would silently get the LAST
    # candle of the whole day instead. Same fact already documented for
    # get_5min_candles elsewhere in this project family; enforced
    # client-side here for the same reason.
    return [pt for pt in series if start <= pt[0] <= end]


def get_batch_quote(security_ids: list[int], exchange_segment: str = "NSE_FNO"
                     ) -> dict[int, dict]:
    """CURRENT (right now, not historical) price + OI for MANY securities
    in ONE call, via Dhan's real /marketfeed/quote endpoint -- confirmed
    live 2026-09-16 against Dhan's own API docs (dhanhq.co): up to 1000
    instruments per request, rate limit 1 request/second.

    NOT a substitute for get_intraday_oi_and_price() -- that fetches a
    SPECIFIC HISTORICAL WINDOW (e.g. the 9:15-9:30 opening range), which
    this endpoint cannot do; it only returns whatever is live at the
    moment of the call. Use this only where "current, whenever this
    happens to run" is what's actually wanted -- e.g. a recheck comparing
    against an already-recorded baseline, never the baseline itself.

    Returns {security_id: {"price": float, "oi": int}}, missing any ID
    Dhan didn't return data for (honest partial result, not a guess).
    """
    if not security_ids:
        return {}
    payload = {exchange_segment: [int(s) for s in security_ids]}
    try:
        r = requests.post(f"{DHAN_BASE_URL}/marketfeed/quote", json=payload, headers=_headers(), timeout=25)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return {}

    out: dict[int, dict] = {}
    for sid_str, leg in (data.get("data", {}).get(exchange_segment, {}) or {}).items():
        try:
            out[int(sid_str)] = {"price": float(leg.get("last_price") or 0.0), "oi": int(leg.get("oi") or 0)}
        except (TypeError, ValueError):
            continue
    return out
