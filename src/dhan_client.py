"""
Minimal read-only Dhan REST client for live NSE daily price history.

Self-contained (no dhanhq SDK dependency, no python-dotenv) so it drops
into this project without touching requirements.txt beyond `requests`,
which is already listed. Loads DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN from the
.env file next to this project (never hardcoded, never logged).

Scrip master column names verified live on 2026-09-14 (EXCH_ID, INSTRUMENT,
SERIES, SECURITY_ID, UNDERLYING_SYMBOL, SYMBOL_NAME) -- this is Dhan's
CURRENT schema. Older Dhan integrations elsewhere use different column
names (SEM_EXM_EXCH_ID etc.) from a prior schema version; do not mix them.
"""
from __future__ import annotations

import os
import time
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import pandas as pd
import requests

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
_MASTER_CACHE_PATH = _PROJECT_ROOT / "output" / "dhan_scrip_master.csv"
_MASTER_CACHE_TTL_HOURS = 24

SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
DHAN_BASE_URL = "https://api.dhan.co/v2"


def _load_env_file() -> None:
    """Populate os.environ from .env, without overwriting anything already set."""
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
            "DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set -- fill in .env at the project root."
        )
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "access-token": token,
        "client-id": client_id,
    }


@lru_cache(maxsize=1)
def _security_id_map() -> dict[str, str]:
    """NSE equity trading symbol (bare, e.g. 'TATASTEEL') -> Dhan security_id."""
    use_cache = (
        _MASTER_CACHE_PATH.exists()
        and (time.time() - _MASTER_CACHE_PATH.stat().st_mtime) < _MASTER_CACHE_TTL_HOURS * 3600
    )
    if use_cache:
        df = pd.read_csv(_MASTER_CACHE_PATH, low_memory=False)
    else:
        r = requests.get(SCRIP_MASTER_URL, timeout=60)
        r.raise_for_status()
        _MASTER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MASTER_CACHE_PATH.write_bytes(r.content)
        df = pd.read_csv(_MASTER_CACHE_PATH, low_memory=False)

    eq = df[
        (df["EXCH_ID"] == "NSE")
        & (df["INSTRUMENT"] == "EQUITY")
        & (df["SERIES"] == "EQ")
    ]
    return dict(zip(eq["UNDERLYING_SYMBOL"].astype(str).str.upper(), eq["SECURITY_ID"].astype(str)))


def get_daily_history(symbol: str, years: float = 3.0) -> pd.DataFrame | None:
    """
    Daily OHLCV for a bare NSE symbol (no .NS suffix) via Dhan's historical
    data API. Returns a DataFrame indexed by date (ascending) with columns
    Open/High/Low/Close/Volume, or None if the symbol isn't found in the
    scrip master or the fetch/response is bad.
    """
    security_id = _security_id_map().get(symbol.upper())
    if security_id is None:
        return None

    to_date = date.today()
    from_date = to_date - timedelta(days=int(years * 365) + 10)
    payload = {
        "securityId": security_id,
        "exchangeSegment": "NSE_EQ",
        "instrument": "EQUITY",
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
        "Open": data["open"],
        "High": data["high"],
        "Low": data["low"],
        "Close": data["close"],
        "Volume": data["volume"],
    }).dropna(subset=["Date"])
    df = df.set_index("Date").sort_index()
    # Dhan's historical endpoint can return an exact-duplicate row for a
    # given timestamp (confirmed live for a recently-listed stock, TMPV,
    # around its Oct-2025 listing date) -- dedupe defensively.
    return df[~df.index.duplicated(keep="first")]
