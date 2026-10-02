"""tracking.py (stops, 2R, status since joining the list) and its use in service.py. Offline: fake Dhan history."""
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import service  # noqa: E402
import tracking  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def hist(closes, start="2026-09-01"):
    """Dhan-shaped daily bars: stamped 18:30 UTC the day before (midnight IST read as UTC), High/Low +-1%."""
    days = pd.bdate_range(start, periods=len(closes))
    idx = days - pd.Timedelta(hours=5, minutes=30)
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": 1000})


def test_dhan_dates_are_moved_onto_the_right_day():
    h = tracking.ist_daily(hist([100, 101]))
    assert h.index[0] == pd.Timestamp("2026-09-01") and h.index[1] == pd.Timestamp("2026-09-02")


def test_status_since_joining():
    s = pd.Series([100, 104, 111, 108], index=pd.bdate_range("2026-09-28", periods=4), dtype=float)
    st = tracking.status_since(s, "2026-09-28", 0.02)
    assert st["status"] == "2R REACHED" and st["status_at"] == "2026-09-30" and st["trail_stop"] == 105.45
    down = pd.Series([100, 98, 94.9, 120], index=pd.bdate_range("2026-09-28", periods=4), dtype=float)
    assert tracking.status_since(down, "2026-09-28", 0.02)["status"] == "STOPPED"
    assert tracking.status_since(s, "2026-10-30", 0.02) is None


def test_join_dates_survive_a_re_rank(tmp_path):
    path = tmp_path / "pick_since.json"
    since, added, dropped = tracking.update_since(path, ["A", "B"], date(2026, 9, 28))
    assert added == ["A", "B"] and not dropped
    since, added, dropped = tracking.update_since(path, ["B", "C"], date(2026, 9, 29))
    assert since == {"B": "2026-09-28", "C": "2026-09-29"} and added == ["C"] and dropped == ["A"]


def test_track_on_a_holiday_dates_new_picks_to_the_last_close(tmp_path):
    closes = [100 + i * 0.1 for i in range(25)]              # last bar Mon 2026-10-05 (25 business days from 09-01)
    fake = {"AAA": hist(closes), "BBB": None}
    res = tracking.track(["AAA", "BBB"], date(2026, 10, 10), tmp_path, lambda s, years: fake[s], sleep=lambda x: None)
    assert res["day"] == "2026-10-05" and res["added"] == ["AAA", "BBB"]
    a = res["picks"]["AAA"]
    assert a["status"] == "OPEN" and a["list_date"] == "2026-10-05" and a["since_list_pct"] == 0.0
    assert a["stop"] < a["last_close"] < a["two_r"] and a["risk_pct"] == 5.0          # quiet stock: the 5% floor
    assert "BBB" not in res["picks"]                                                  # no history: left out
    assert json.loads((tmp_path / "tracking.json").read_text())["picks"]["AAA"]["status"] == "OPEN"


def test_status_json_carries_the_tracking(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(service, "PREVIOUS_PICKS_PATH", tmp_path / "previous_final.csv")
    monkeypatch.setattr(service, "RUN_STATE_PATH", tmp_path / "run_state.json")
    pd.DataFrame([{"symbol": "AAA", "multibagger_score": 9.0, "current_price": 102.0},
                  {"symbol": "ZZZ", "multibagger_score": 8.0, "current_price": 50.0}]).to_csv(tmp_path / "final.csv", index=False)
    (tmp_path / "tracking.json").write_text(json.dumps({"day": "2026-10-01", "picks": {"AAA": {
        "stop": 96.9, "two_r": 112.2, "risk_pct": 5.0, "atr_pct": 1.8, "status": "OPEN", "status_at": None,
        "list_price": 100.0, "list_date": "2026-09-30", "trail_stop": 95.0, "since_list_pct": 2.0, "last_close": 102.0}}}))
    st = service._build_status()
    a, z = st["top_picks"]
    assert a["status"] == "OPEN" and a["since_list_pct"] == 2.0 and a["two_r"] == 112.2 and z["status"] is None
    assert st["tracking_as_of"] == "2026-10-01" and "ATR" in st["stop_rule"]


def test_ledger_records_entries_and_exits():
    when = datetime(2026, 10, 5, 18, 10, tzinfo=IST)
    recs = service._ledger_records([{"symbol": "AAA", "multibagger_score": 9.0, "current_price": 102.0}],
                                   {"day": "2026-10-05", "added": ["AAA"], "dropped": ["OLD"],
                                    "picks": {"AAA": {"last_close": 102.0, "stop": 96.9, "two_r": 112.2}}}, when)
    assert [(r["symbol"], r["kind"], r["action"]) for r in recs] == [("AAA", "signal", "BUY"), ("OLD", "event", "DROPPED")]
    assert recs[0]["entry_price"] == 102.0 and recs[0]["stop"] == 96.9 and recs[0]["dedupe_key"] == "multibagger-screener:2026-10-05:AAA:entered"
