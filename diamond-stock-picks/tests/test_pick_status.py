"""picks.pick_status: each listed stock's status since it joined the list (trailing stop as in stops.py)."""
import pandas as pd

import picks as pk


def frame(prices: dict):
    idx = pd.bdate_range("2026-09-28", periods=max(len(v) for v in prices.values()))
    return pd.DataFrame({f"{k}.NS": pd.Series(v, index=idx[:len(v)]) for k, v in prices.items()})


SINCE = {"UP": "2026-09-28", "DOWN": "2026-09-28", "FLAT": "2026-09-28", "LATE": "2026-09-30", "NONE": "2026-09-28"}


def test_statuses():
    close = frame({"UP": [100, 104, 111, 108], "DOWN": [100, 98, 94.9, 120], "FLAT": [100, 101, 99, 100],
                   "LATE": [50, 50, 60, 61]})
    st = pk.pick_status(close, SINCE, {"UP": 0.02, "DOWN": 0.02, "FLAT": 0.02, "LATE": 0.02}, 10)
    # 2.5 x 2% ATR = 5% stop, 2R = +10%
    assert st["UP"]["status"] == "2R REACHED" and st["UP"]["at"] == "2026-09-30" and st["UP"]["stop"] == 105.45
    assert st["DOWN"]["status"] == "STOPPED" and st["DOWN"]["at"] == "2026-09-30"      # 94.9 <= 95; the later rally does not undo it
    assert st["FLAT"]["status"] == "OPEN" and st["FLAT"]["ref_price"] == 100.0 and st["FLAT"]["at"] is None
    assert st["LATE"]["ref_price"] == 60.0 and st["LATE"]["ref_date"] == "2026-09-30"    # its own start date
    assert "NONE" not in st                                                            # no prices: left out


def test_no_atr_uses_stop_pct():
    st = pk.pick_status(frame({"X": [100, 90.5, 89.9]}), {"X": "2026-09-28"}, {}, 10)
    assert st["X"]["status"] == "STOPPED" and st["X"]["at"] == "2026-09-30"           # 10% stop at 90


def test_service_rows_carry_status_and_move(monkeypatch, tmp_path):
    import alerts as al
    import service
    monkeypatch.setattr(service, "_state", al.State(str(tmp_path)))
    service._state.data["pick_status"] = {"A": {"status": "OPEN", "at": None, "ref_price": 100.0, "ref_date": "2026-09-30",
                                                "stop": 95.0, "high": 100.0, "two_r": 110.0},
                                          "B": {"status": "2R REACHED", "at": "2026-10-01", "ref_price": 50.0,
                                                "ref_date": "2026-09-30", "stop": 52.0, "high": 56.0, "two_r": 55.0}}
    rows = service._decorate([{"symbol": "A", "price": 103.5, "invest": 1}, {"symbol": "B", "price": 51.0, "invest": 1},
                              {"symbol": "C", "price": 10.0, "invest": 1}])
    by = {r["symbol"]: r for r in rows}
    assert by["A"]["status"] == "OPEN" and by["A"]["since_list_pct"] == 3.5
    assert by["B"]["status"] == "BELOW STOP (live)" and by["B"]["since_list_pct"] == 2.0
    assert by["C"]["status"] is None and by["C"]["since_list_pct"] is None
