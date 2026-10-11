"""service._market_traded_today and the scheduler: no scan on a closed day, no ledger records from a closed-day start."""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import service  # noqa: E402
from src import dhan_client as dc  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
FRI, SAT, HOLIDAY = date(2026, 10, 9), date(2026, 10, 10), date(2026, 10, 2)


class _Resp:
    def __init__(self, stamps):
        self.stamps = stamps

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": {"NSE_EQ": {str(i): {"last_trade_time": s} for i, s in enumerate(self.stamps)}}}


def _check(monkeypatch, day, stamps=None, boom=False):
    import requests
    monkeypatch.setattr(dc, "_security_id_map", lambda: {"RELIANCE": "1", "HDFCBANK": "2", "INFY": "3"})
    monkeypatch.setattr(dc, "_headers", lambda: {})
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    calls = []

    def post(url, json, headers, timeout):
        calls.append(json)
        if boom:
            raise OSError("down")
        return _Resp(stamps)
    monkeypatch.setattr(requests, "post", post)
    return service._market_traded_today(day), calls


def test_weekend_is_closed_without_asking(monkeypatch):
    assert _check(monkeypatch, SAT, ["10/10/2026 09:20:00"]) == (False, [])


def test_holiday_shows_the_last_sessions_trades_and_is_closed(monkeypatch):
    ok, calls = _check(monkeypatch, HOLIDAY, ["01/10/2026 15:59:59", "01/10/2026 15:59:58"])
    assert ok is False and calls == [{"NSE_EQ": [1, 2, 3]}]


def test_a_trade_today_is_open(monkeypatch):
    assert _check(monkeypatch, FRI, ["08/10/2026 15:59:59", "09/10/2026 15:59:00"])[0] is True


def test_a_failed_quote_is_retried_then_a_weekday_counts_as_open(monkeypatch):
    ok, calls = _check(monkeypatch, FRI, boom=True)
    assert ok is True and len(calls) == 3


class _Stop(Exception):
    pass


def _drive(monkeypatch, traded: list):
    """Run the real scheduler: the startup run, then one 18:00 slot, then stop. Returns the record flags of each scan."""
    scans, answers = [], iter(traded)
    monkeypatch.setattr(service, "_market_traded_today", lambda today: next(answers))
    monkeypatch.setattr(service, "_run_scan", lambda record=True: scans.append(record))
    sleeps = iter([None])
    monkeypatch.setattr(service.time, "sleep", lambda s: next(sleeps))
    with pytest.raises(StopIteration):
        service._scheduler_loop()
    return scans


def test_startup_on_a_closed_day_shows_picks_but_records_nothing_and_the_1800_slot_is_skipped(monkeypatch):
    assert _drive(monkeypatch, [False, False]) == [False]


def test_a_trading_day_records_both(monkeypatch):
    assert _drive(monkeypatch, [True, True]) == [True, True]


def test_run_scan_without_record_never_tracks(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(service, "PREVIOUS_PICKS_PATH", tmp_path / "previous_final.csv")
    monkeypatch.setattr(service, "RUN_STATE_PATH", tmp_path / "run_state.json")

    class P:
        returncode, stdout, stderr = 0, "done", ""
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: P())
    tracked = []
    monkeypatch.setattr(service, "_track_and_record", lambda when: tracked.append(when))
    service._run_scan(record=False)
    assert tracked == []
    service._run_scan()
    assert len(tracked) == 1
