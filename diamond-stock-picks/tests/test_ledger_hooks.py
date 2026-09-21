"""Offline tests for the ledger hooks: what a real run records, and that the ledger can never hurt a run."""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import alerts as al
import ledger_hooks as lh
from test_service import _synthetic_store, svc  # noqa: F401  (svc is a fixture)

IST = timezone(timedelta(hours=5, minutes=30))
MONDAY = datetime(2026, 9, 21, 16, 10, tzinfo=IST)


class Sink:
    """Stands in for the LedgerClient and keeps what would have been sent."""

    def __init__(self):
        self.recs = []

    def record_many(self, recs):
        self.recs.extend(recs)


@pytest.fixture()
def sink(monkeypatch):
    s = Sink()
    monkeypatch.setattr(lh, "_ledger", s)
    return s


def _run(svc, monkeypatch, tmp_path, seed=1, when=MONDAY, holdings=""):
    monkeypatch.setattr(svc, "_state", al.State(str(tmp_path)))
    monkeypatch.setenv("HOLDINGS", holdings)
    monkeypatch.setattr(svc.px, "fetch_all", lambda syms: _synthetic_store(seed))
    svc._run_locked("test", when)


def test_a_rescreen_records_every_stock_on_every_list(svc, sink, monkeypatch, tmp_path):
    _run(svc, monkeypatch, tmp_path)
    active = svc._state.data["active"]
    expected = sum(len(v) for v in active.values())
    assert expected >= 12 and len(sink.recs) == expected
    rows = {r["symbol"]: r for r in svc._latest["picks"]}
    for r in sink.recs:
        assert r["source"] == "diamond-stock-picks" and r["kind"] == "signal" and r["action"] == "BUY" and r["direction"] == "BULLISH"
        assert r["strategy"] in ("diamond_stock_steady", "diamond_stock_gods_plan")
        row = rows[r["symbol"]]
        assert r["entry_price"] == row["price"] and r["stop"] == row["stop"] == round(row["price"] * 0.9, 2)
        assert r["dedupe_key"].endswith(f":{r['features']['strategy']}:{r['symbol']}:list") and r["features"]["data_as_of"] == "2026-09-18"
        assert "verdict" in r["features"] and r["ts_signal"] == MONDAY.isoformat()


def test_a_stock_on_both_lists_is_recorded_once_per_list(svc, sink, monkeypatch, tmp_path):
    _run(svc, monkeypatch, tmp_path)
    both = [r["symbol"] for r in svc._latest["picks"] if r["lists"] == "Both"]
    for s in both:
        assert sorted(x["strategy"] for x in sink.recs if x["symbol"] == s) == ["diamond_stock_gods_plan", "diamond_stock_steady"]


def test_records_pass_the_real_ledger_validation(svc, sink, monkeypatch, tmp_path):
    candidates = [r"E:\DRIVE D\Trading Market\Project Options\trading-ledger",
                  os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "trading-ledger")]
    found = next((d for d in candidates if os.path.exists(os.path.join(d, "server.py"))), None)
    if not found:
        pytest.skip("trading-ledger/server.py not available")
    sys.path.insert(0, found)
    import server
    _run(svc, monkeypatch, tmp_path)
    sink.recs.append(lh.dropped_records(["ZZZ"], {}, "2026-09-18", MONDAY)[0])
    sink.recs.append(lh.stop_hit_record({"symbol": "AAA", "buy": 100.0, "stop": 90.0, "last": 85.0, "qty": 3}, "2026-09-21", MONDAY))
    assert len(sink.recs) > 12
    for r in sink.recs:
        server.validate_signal(json.loads(json.dumps(r, default=str, allow_nan=False)))


def test_midweek_runs_record_nothing_new(svc, sink, monkeypatch, tmp_path):
    _run(svc, monkeypatch, tmp_path)
    n = len(sink.recs)
    monkeypatch.setattr(svc.px, "fetch_all", lambda syms: _synthetic_store(5))
    svc._run_locked("test", datetime(2026, 9, 23, 16, 10, tzinfo=IST))
    assert len(sink.recs) == n                     # no re-screen midweek, so no new list records


def test_rescreen_records_dropped_and_stop_hit_events(svc, sink, monkeypatch, tmp_path):
    _run(svc, monkeypatch, tmp_path)
    first = svc._latest["picks"][0]
    sink.recs.clear()
    monkeypatch.setenv("HOLDINGS", f"{first['symbol']}:10@{first['price'] * 2}")
    monkeypatch.setattr(svc.px, "fetch_all", lambda syms: _synthetic_store(2))
    svc._state.data["list_date"] = "2026-09-11"
    svc._run_locked("test", datetime(2026, 9, 28, 16, 10, tzinfo=IST))
    actions = [r["action"] for r in sink.recs]
    assert "STOP_HIT" in actions and actions.count("BUY") >= 12
    hit = next(r for r in sink.recs if r["action"] == "STOP_HIT")
    assert hit["symbol"] == first["symbol"] and hit["kind"] == "event" and hit["entry_price"] == first["price"] * 2
    assert all(r["kind"] == "event" for r in sink.recs if r["action"] == "DROPPED")
    assert len({r["dedupe_key"] for r in sink.recs}) == len(sink.recs)


def test_stop_hit_is_recorded_once_even_if_checked_every_15_minutes(svc, sink, monkeypatch, tmp_path):
    _run(svc, monkeypatch, tmp_path)
    first = svc._latest["picks"][0]
    monkeypatch.setenv("HOLDINGS", f"{first['symbol']}:10@{first['price'] * 2}")
    sink.recs.clear()
    for _ in range(4):                              # four intraday checks on the same day
        svc._holdings_and_alerts({first["symbol"]: first["price"]}, "2026-09-21")
    assert [r["action"] for r in sink.recs] == ["STOP_HIT"]


def test_a_broken_ledger_cannot_break_a_run(svc, monkeypatch, tmp_path):
    class Boom:
        def record_many(self, recs):
            raise RuntimeError("ledger exploded")

    monkeypatch.setattr(lh, "_ledger", Boom())
    _run(svc, monkeypatch, tmp_path)
    assert svc._latest["run_state"] == "ready" and svc._latest["picks"]


def test_a_missing_client_module_is_tolerated(svc, monkeypatch, tmp_path):
    monkeypatch.setattr(lh, "_ledger", None)
    monkeypatch.setattr(lh, "LedgerClient", None)
    _run(svc, monkeypatch, tmp_path)
    assert svc._latest["run_state"] == "ready"


def test_hooks_survive_garbage():
    assert lh.pick_records(None, None, "d", MONDAY, 10) == []
    assert lh.pick_records([None, {}, {"symbol": "A", "price": 0}], {"steady": ["A", "B", None]}, "d", MONDAY, 10) == []
    assert lh.dropped_records(None, None, "d", MONDAY) == []
    lh.record(None)
    lh.record([{"bad": object()}])


def test_disabled_ledger_and_status_shape(svc, monkeypatch, tmp_path):
    monkeypatch.delenv("LEDGER_URL", raising=False)
    monkeypatch.delenv("LEDGER_API_KEY", raising=False)
    monkeypatch.setattr(lh, "_ledger", None)
    _run(svc, monkeypatch, tmp_path)
    assert lh.status()["enabled"] is False


def test_end_to_end_records_reach_a_ledger(svc, monkeypatch, tmp_path):
    got = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            got.extend(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("LEDGER_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setenv("LEDGER_API_KEY", "k" * 32)
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(lh, "_ledger", None)
    _run(svc, monkeypatch, tmp_path)
    assert lh._ledger.flush(15)
    srv.shutdown()
    assert len(got) == sum(len(v) for v in svc._state.data["active"].values())
    assert lh.status()["sent"] == len(got)
    json.dumps(lh.status())
