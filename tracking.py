"""
Stops, the 2R milestone and a status for each of the screener's top picks, the same rules Diamond Stock Picks uses
(stops.py, copied from diamond-stock-picks/):

  stop / risk  2.5x the stock's 14-day ATR below the latest close (5-15%; STOP_PCT when there is no ATR): where a stop
               would sit for someone buying now
  2R level     latest close + 2x that risk: a milestone, not a sell level
  status       since the stock joined the list, from the close of that day, by the trailing rule (highest close since
               then less the same distance, never lowered): OPEN, 2R REACHED (stop now at least that day's close) or
               STOPPED (a close at or below the trailing stop as it stood the day before; final, with the date)
  move         latest close against that day's close

A stock's join date is kept in pick_since.json (on the service's volume) for as long as it stays in the top list, so a
daily re-rank does not reset it; one that drops out and comes back starts again. Prices are Dhan daily closes (its bars
are stamped midnight IST read as UTC, so every date is shifted onto the right day first). Run after each scan; never
raises for one stock: a stock without history is left out.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

import stops

ATR_BARS = 14
ATR_SKIP = 0.25        # a bar whose range is over 25% of the close is a split / bonus in unadjusted prices
STOP_PCT = float(os.environ.get("STOP_PCT", "10"))


def ist_daily(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.index = (pd.DatetimeIndex(out.index) + pd.Timedelta(hours=5, minutes=30)).normalize()
    return out[~out.index.duplicated(keep="last")]


def atr_pct(df: pd.DataFrame, bars: int = ATR_BARS) -> Optional[float]:
    """Average true range of the last `bars` bars as a fraction of each bar's close (0.02 = 2%)."""
    if df is None or not {"High", "Low", "Close"} <= set(df.columns):
        return None
    d = df[["High", "Low", "Close"]].dropna()
    prev = d["Close"].shift(1)
    tr = pd.concat([d["High"] - d["Low"], (d["High"] - prev).abs(), (d["Low"] - prev).abs()], axis=1).max(axis=1)
    ratio = (tr / d["Close"]).iloc[1:]
    ratio = ratio[(ratio > 0) & (ratio <= ATR_SKIP)].tail(bars)
    return round(float(ratio.mean()), 5) if len(ratio) >= bars // 2 else None


def status_since(close: pd.Series, since: str, atr: Optional[float], stop_pct: float = STOP_PCT) -> Optional[dict]:
    """OPEN / 2R REACHED / STOPPED since `since` (YYYY-MM-DD), from the first close on or after it."""
    s = close.dropna()
    s = s[s.index >= pd.Timestamp(since)]
    if s.empty:
        return None
    ref_date, ref = s.index[0], float(s.iloc[0])
    t = stops.trail(None, ref, None, atr, stop_pct)
    status, at = None, None
    for d, c in s.iloc[1:].items():
        if c <= t["stop"]:
            status, at = "STOPPED", d.date().isoformat()
            break
        was = t["two_r_hit"]
        t = stops.trail(t, ref, float(c), atr, stop_pct)
        if t["two_r_hit"] and not was:
            at = d.date().isoformat()
    return {"status": status or ("2R REACHED" if t["two_r_hit"] else "OPEN"), "status_at": at,
            "list_price": round(ref, 2), "list_date": ref_date.date().isoformat(), "trail_stop": t["stop"]}


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def update_since(path: Path, symbols, today: date) -> tuple[dict, list, list]:
    """(since, added, dropped): keeps each symbol's join date while it stays in the list."""
    try:
        since = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        since = {}
    current = [str(s) for s in symbols if s]
    added = [s for s in current if s not in since]
    dropped = [s for s in since if s not in current]
    for s in added:
        since[s] = today.isoformat()
    for s in dropped:
        del since[s]
    _save_json(path, since)
    return since, added, dropped


def track(symbols, today: date, out_dir: Path, history: Callable, sleep: Callable = time.sleep) -> dict:
    """Run after a scan: {"day", "picks": {SYMBOL: levels + status}, "added", "dropped"}, also saved as tracking.json.
    A stock joining today is dated to the latest trading day with a close (a scan on a holiday or a weekend would
    otherwise date it to a day with no price)."""
    current = [str(s) for s in symbols if s]
    hists: dict = {}
    for sym in current:
        hist = None
        for attempt in range(3):
            try:
                hist = history(sym, years=1.5)
            except Exception:
                hist = None
            if hist is not None and len(hist):
                break
            sleep(1.5 * (attempt + 1))
        sleep(0.4)                                            # Dhan's data API: stay well under its rate limit
        if hist is not None and len(hist):
            hists[sym] = ist_daily(hist)
    data_day = max((h.index[-1].date() for h in hists.values()), default=today)
    since, added, dropped = update_since(out_dir / "pick_since.json", current, data_day)
    picks: dict = {}
    for sym, hist in hists.items():
        a = atr_pct(hist)
        last = float(hist["Close"].dropna().iloc[-1])
        row = {"atr_pct_raw": a, "last_close": round(last, 2), **stops.entry_levels(round(last, 2), a, STOP_PCT)}
        st = status_since(hist["Close"], since[sym], a) if sym in since else None
        if st:
            row.update(st, since_list_pct=round((last / st["list_price"] - 1) * 100, 2) if st["list_price"] else None)
        picks[sym] = row
    out = {"day": data_day.isoformat(), "picks": picks, "added": added, "dropped": dropped}
    _save_json(out_dir / "tracking.json", out)
    return out


def load(out_dir: Path) -> dict:
    try:
        return json.loads((out_dir / "tracking.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
