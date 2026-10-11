"""service._live_prices: the dashboard's Current column (a live quote in market hours, cached a minute)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import service  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
OPEN, NIGHT, SAT = datetime(2026, 10, 9, 11, 0, tzinfo=IST), datetime(2026, 10, 9, 20, 0, tzinfo=IST), datetime(2026, 10, 10, 11, 0, tzinfo=IST)


def _reset(monkeypatch):
    monkeypatch.setattr(service, "_live", {"at": None, "fetched": 0.0, "prices": {}})


def test_market_hours_fetch_once_a_minute(monkeypatch):
    _reset(monkeypatch)
    calls = []
    fetch = lambda syms: calls.append(syms) or {"AAA": {"ltp": 105.0, "prev_close": 100.0}}
    r = service._live_prices(["AAA"], OPEN, fetch)
    assert r["prices"]["AAA"]["ltp"] == 105.0 and r["at"].startswith("2026-10-09T11:00")
    service._live_prices(["AAA"], OPEN, fetch)
    assert len(calls) == 1                                              # cached
    service._live_prices(["AAA", "BBB"], OPEN, fetch)
    assert len(calls) == 2                                              # a new name is fetched at once


def test_outside_market_hours_nothing_is_fetched(monkeypatch):
    _reset(monkeypatch)
    boom = lambda syms: (_ for _ in ()).throw(AssertionError("fetched"))
    assert service._live_prices(["AAA"], NIGHT, boom) == {"at": None, "prices": {}}
    assert service._live_prices(["AAA"], SAT, boom)["prices"] == {}


def test_a_failed_quote_never_raises(monkeypatch):
    _reset(monkeypatch)
    r = service._live_prices(["AAA"], OPEN, lambda syms: (_ for _ in ()).throw(OSError("down")))
    assert r == {"at": None, "prices": {}}


def test_status_carries_current_price_and_day_change(tmp_path, monkeypatch):
    import pandas as pd
    import json
    monkeypatch.setattr(service, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(service, "PREVIOUS_PICKS_PATH", tmp_path / "previous_final.csv")
    monkeypatch.setattr(service, "RUN_STATE_PATH", tmp_path / "run_state.json")
    pd.DataFrame([{"symbol": "AAA", "multibagger_score": 9.0, "current_price": 102.0},
                  {"symbol": "ZZZ", "multibagger_score": 8.0, "current_price": 50.0}]).to_csv(tmp_path / "final.csv", index=False)
    (tmp_path / "tracking.json").write_text(json.dumps({"day": "2026-10-09", "picks": {"ZZZ": {"last_close": 51.0}}}))
    monkeypatch.setattr(service, "_live_prices", lambda syms: {"at": "2026-10-09T11:00:00+05:30",
                                                               "prices": {"AAA": {"ltp": 104.04, "prev_close": 102.0}}})
    st = service._build_status()
    a, z = st["top_picks"]
    assert a["now_price"] == 104.04 and a["day_chg_pct"] == 2.0
    assert z["now_price"] == 51.0 and z["day_chg_pct"] is None          # no quote: the last tracked close
    assert st["live_at"] == "2026-10-09T11:00:00+05:30"
