"""Offline tests: no Dhan, no yfinance, no network. Synthetic prices drive the real engine code."""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import alerts as al
import picks as pk
import prices as px

IST = timezone(timedelta(hours=5, minutes=30))


# ---------- alerts ----------

def test_parse_holdings_skips_malformed():
    h = al.parse_holdings("DIVISLAB:3@8570, sbin.ns:27@990.5, junk, X:1")
    assert h == {"DIVISLAB": {"qty": 3, "buy": 8570.0}, "SBIN": {"qty": 27, "buy": 990.5}}


def test_diff_lists():
    added, dropped = al.diff_lists({"steady": ["A", "B"], "gods_plan": ["B", "C"]}, {"steady": ["B", "D"], "gods_plan": ["C"]})
    assert added == ["D"] and dropped == ["A"]


def test_check_holdings_statuses():
    held = {"A": {"qty": 10, "buy": 100.0}, "B": {"qty": 5, "buy": 200.0}, "C": {"qty": 1, "buy": 50.0}, "Z": {"qty": 1, "buy": 1.0}}
    rows = {r["symbol"]: r for r in al.check_holdings(held, {"A": 89.9, "B": 210.0, "C": 60.0}, {"B"}, 10)}
    assert rows["A"]["status"] == "STOP HIT" and rows["A"]["stop"] == 90.0
    assert rows["B"]["status"] == "HELD" and rows["B"]["pnl_rs"] == 50
    assert rows["C"]["status"] == "NOT ON LIST"
    assert rows["Z"]["status"] == "NO PRICE"


def test_state_dedupes_and_persists(tmp_path):
    st = al.State(str(tmp_path))
    assert st.add_alert("STOP_HIT", "A", "m", "2026-09-18", "high") is True
    assert st.add_alert("STOP_HIT", "A", "m", "2026-09-18", "high") is False  # same day: no duplicate
    assert st.add_alert("STOP_HIT", "A", "m", "2026-09-19", "high") is True
    st.save()
    assert len(al.State(str(tmp_path)).data["alerts"]) == 2


# ---------- prices ----------

def test_ist_fix_moves_bar_to_the_right_day():
    idx = pd.DatetimeIndex(["2026-09-17 18:30:00"])  # Dhan: midnight 18-Sep IST read as UTC
    out = px._ist_fix(pd.DataFrame({"Close": [1.0]}, index=idx))
    assert out.index[0] == pd.Timestamp("2026-09-18")


def _jump_frame():
    idx = pd.bdate_range("2025-01-01", periods=300)
    p = np.full(300, 100.0)
    p[150:] = 50.0  # an unadjusted 1:2 split on day 150
    return pd.DataFrame({"AAA.NS": p, "^NSEI": np.linspace(100, 110, 300)}, index=idx)


def test_split_excluded_when_yfinance_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "yfinance", None)  # import fails
    df, warn = _jump_frame(), []
    assert px._fix_splits(df, warn) == {"AAA.NS"} and warn


def test_split_adjusted_when_yfinance_shows_no_jump(monkeypatch):
    df = _jump_frame()
    asked = []
    fake = types.ModuleType("yfinance")

    def download(tickers, **k):
        asked.append(list(tickers))
        flat = pd.DataFrame({tk: np.full(300, 50.0) for tk in tickers}, index=df.index)  # adjusted: flat
        return pd.concat({"Close": flat}, axis=1)

    fake.download = download
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    warn: list[str] = []
    assert px._fix_splits(df, warn) == set()
    assert asked == [["AAA.NS"]]  # exact NSE ticker, one batched call (a doubled ".NS.NS" excluded 48 stocks live)
    assert df["AAA.NS"].nunique() == 1  # history scaled to the post-split basis


def test_split_real_gap_kept_when_yfinance_agrees(monkeypatch):
    df = _jump_frame()
    fake = types.ModuleType("yfinance")
    fake.download = lambda tickers, **k: pd.concat({"Close": pd.DataFrame({tk: df[tk].values for tk in tickers}, index=df.index)}, axis=1)
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    before = df["AAA.NS"].copy()
    assert px._fix_splits(df, []) == set()
    assert df["AAA.NS"].equals(before)  # yfinance shows the same move, so it is a real move: untouched


def test_install_serves_engine_from_store():
    from diamond.data import market

    idx = pd.bdate_range("2026-01-01", periods=10)
    store = px.PriceStore(pd.DataFrame({"X.NS": range(10, 20)}, index=idx, dtype=float), pd.DataFrame(), "now")
    px.install(store)
    got = market.download_prices_daterange(["X.NS", "NOPE.NS"], "2026-01-05", "2026-01-09")
    assert list(got.columns) == ["X.NS"] and got.index.max() < pd.Timestamp("2026-01-09")  # end exclusive
    assert market.download_prices(["X.NS"], period_days=3, end_date="2026-01-09").shape[0] <= 3


# ---------- end to end (real engine on synthetic data) ----------

def _synthetic_store(seed: int) -> px.PriceStore:
    syms = pk.universe_symbols()
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-09-18", periods=800)
    drift = rng.normal(0.0004, 0.0004, len(syms))
    ret = rng.normal(drift, 0.017, (len(idx), len(syms)))
    close = pd.DataFrame(100 * np.exp(ret.cumsum(0)), index=idx, columns=[f"{s}.NS" for s in syms])
    close["^NSEI"] = 100 * np.exp(rng.normal(0.0003, 0.009, len(idx)).cumsum())
    return px.PriceStore(close, close * 0 + 1e6, "now")


@pytest.fixture()
def svc(monkeypatch):
    import service

    monkeypatch.setattr(service.fd, "check", lambda syms: {s: {"symbol": s, "verdict": "OK", "flags": [], "financial": False} for s in syms})
    monkeypatch.setattr(service.px, "live_prices", lambda syms: {})
    return service


def test_full_run_then_rescreen_raises_alerts(svc, monkeypatch, tmp_path):
    monkeypatch.setattr(svc, "_state", al.State(str(tmp_path)))
    monkeypatch.setenv("HOLDINGS", "")
    monkeypatch.setattr(svc.px, "fetch_all", lambda syms: _synthetic_store(1))
    monday = datetime(2026, 9, 21, 16, 10, tzinfo=IST)  # a Monday (re-screen day)
    svc._run_locked("test", monday)
    out = svc._latest
    assert out["run_state"] == "ready" and out["data_as_of"] == "2026-09-18"
    rows = out["picks"]
    assert 12 <= len(rows) <= 40 and all(r["price"] > 0 and r["stop"] == round(r["price"] * 0.9, 2) for r in rows)
    assert not [a for a in out["alerts"] if a["kind"] == "NEW_PICK"]  # first list: nothing to compare with

    # hold one pick with a buy price that puts it below its stop, then re-screen on different data
    victim = rows[0]["symbol"]
    monkeypatch.setenv("HOLDINGS", f"{victim}:10@{rows[0]['price'] * 2}")
    monkeypatch.setattr(svc.px, "fetch_all", lambda syms: _synthetic_store(2))
    svc._state.data["list_date"] = "2026-09-11"  # last weekly re-screen was a week ago
    svc._run_locked("test", datetime(2026, 9, 28, 16, 10, tzinfo=IST))
    kinds = {a["kind"] for a in svc._latest["alerts"]}
    assert "STOP_HIT" in kinds and ("NEW_PICK" in kinds or "DROPPED_PICK" in kinds)
    assert svc._latest["holdings"][0]["status"] in ("STOP HIT",)


def test_no_rescreen_midweek(svc, monkeypatch, tmp_path):
    monkeypatch.setattr(svc, "_state", al.State(str(tmp_path)))
    monkeypatch.setenv("HOLDINGS", "")
    monkeypatch.setattr(svc.px, "fetch_all", lambda syms: _synthetic_store(1))
    svc._run_locked("test", datetime(2026, 9, 21, 16, 10, tzinfo=IST))
    first = dict(svc._state.data["active"])
    monkeypatch.setattr(svc.px, "fetch_all", lambda syms: _synthetic_store(5))  # very different data
    svc._run_locked("test", datetime(2026, 9, 23, 16, 10, tzinfo=IST))  # Wednesday
    assert svc._state.data["active"] == first  # list only changes on the weekly re-screen day


def test_data_failure_is_an_alert_not_a_crash(svc, monkeypatch, tmp_path):
    monkeypatch.setattr(svc, "_state", al.State(str(tmp_path)))

    def boom(syms):
        raise RuntimeError("Dhan returned no price history (token expired or Dhan unreachable)")

    monkeypatch.setattr(svc.px, "fetch_all", boom)
    svc._run("test")
    assert svc._latest["run_state"] == "error"
    assert any(a["kind"] == "DATA_ERROR" for a in svc._latest["alerts"])


def test_fetch_all_loads_instrument_list_before_threads(monkeypatch):
    order = []
    monkeypatch.setattr(px.dc, "_equity_ids", lambda: order.append("master") or {})
    idx = pd.DatetimeIndex(["2026-09-17 18:30:00", "2026-09-18 18:30:00"])
    frame = pd.DataFrame({"Close": [100.0, 101.0], "Volume": [1, 1]}, index=idx)

    def fake_history(sym, years=3.3):
        order.append("history")
        return frame

    monkeypatch.setattr(px.dc, "get_equity_history", fake_history)
    monkeypatch.setattr(px.dc, "get_index_history", lambda *a, **k: frame)
    store = px.fetch_all(["AAA", "BBB", "CCC"], workers=2)
    assert order[0] == "master" and order.count("history") == 3
    assert store.last_date == pd.Timestamp("2026-09-19") and list(store.close.columns)[:3] == ["AAA.NS", "BBB.NS", "CCC.NS"]
