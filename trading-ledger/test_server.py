"""Offline tests for the ledger API (in-memory store, real HTTP server on an ephemeral port)."""
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import server  # noqa: E402

KEY = "k" * 32
IST = timezone(timedelta(hours=5, minutes=30))


class FakeStore:
    def __init__(self):
        self.rows = []
        self.up = True
        self.fail_insert = False

    def ping(self):
        return self.up

    def insert_signals(self, recs):
        if self.fail_insert:
            raise RuntimeError("db down")
        ids = []
        for r in recs:
            if any(x["dedupe_key"] == r["dedupe_key"] for x in self.rows):
                ids.append(None)
            else:
                self.rows.append({**r, "id": len(self.rows) + 1, "ts_signal": r["ts_signal"].isoformat(), "ts_received": "x"})
                ids.append(len(self.rows))
        return ids

    def query_signals(self, filters, since, until, limit):
        out = [r for r in self.rows if all(r.get(k) == v for k, v in filters.items() if k in server._FILTERS and v)]
        return out[::-1][:limit]

    def summary(self, days, include_test=False):
        rows = [r for r in self.rows if include_test or r["source"] != "selftest"]
        return [{"day": "2026-09-21", "source": "s", "strategy": "t", "kind": "signal", "action": "BUY CE", "count": len(rows)}]


@pytest.fixture()
def api():
    store = FakeStore()
    srv = server.make_server(store, KEY, 0, "127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield store, base
    srv.shutdown()
    srv.server_close()


def rec(**kw):
    base = {"source": "options-scanner", "strategy": "directional_options", "symbol": "KAYNES", "action": "BUY PE",
            "dedupe_key": "options-scanner:2026-09-21:KAYNES:PE", "ts_signal": datetime(2026, 9, 21, 10, 30, tzinfo=IST).isoformat(),
            "kind": "signal", "direction": "BEARISH", "rule_version": "v1", "entry_price": 3515.0, "stop": 3470.0, "score": 72.5,
            "features": {"price_move_pct": -1.5, "pcr": 0.63}, "payload": {"expiry": "2026-09-29"}}
    base.update(kw)
    return base


H = {"X-Ledger-Key": KEY}


def post(base, body, headers=H):
    return requests.post(base + "/v1/signals", data=json.dumps(body), headers={**headers, "Content-Type": "application/json"}, timeout=10)


# ---------------- validation ----------------
@pytest.mark.parametrize("field", ["source", "strategy", "symbol", "action", "dedupe_key", "ts_signal"])
def test_every_required_field_is_required(api, field):
    _, base = api
    r = rec()
    del r[field]
    resp = post(base, r)
    assert resp.status_code == 400 and field in resp.json()["error"]


@pytest.mark.parametrize("patch,needle", [
    ({"ts_signal": "2026-09-21T10:30:00"}, "timezone"),                       # naive time is ambiguous
    ({"ts_signal": "yesterday"}, "ISO-8601"),
    ({"ts_signal": (datetime.now(timezone.utc) + timedelta(days=9)).isoformat()}, "future"),
    ({"kind": "guess"}, "kind"), ({"direction": "SIDEWAYS"}, "direction"),
    ({"entry_price": float("nan")}, "finite"), ({"score": "high"}, "number"), ({"stop": True}, "number"),
    ({"features": [1, 2]}, "object"), ({"payload": "x"}, "object"),
    ({"symbol": "  "}, "symbol"), ({"symbol": "X" * 41}, "longer"), ({"surprise": 1}, "unknown field"),
    ({"features": {"blob": "x" * 70000}}, "larger"),
])
def test_bad_records_are_rejected_with_the_reason(api, patch, needle):
    store, base = api
    body = json.dumps(rec(**patch), allow_nan=True)                 # let NaN through the client side to test the server
    resp = requests.post(base + "/v1/signals", data=body, headers={**H, "Content-Type": "application/json"}, timeout=10)
    assert resp.status_code == 400 and needle in resp.json()["error"]
    assert store.rows == []


def test_one_bad_record_rejects_the_whole_batch_and_stores_nothing(api):
    store, base = api
    resp = post(base, [rec(), rec(dedupe_key="b", symbol="")])
    assert resp.status_code == 400 and store.rows == []


def test_minimal_record_is_accepted_with_defaults(api):
    store, base = api
    resp = post(base, {k: rec()[k] for k in ("source", "strategy", "symbol", "action", "dedupe_key", "ts_signal")})
    assert resp.status_code == 200 and resp.json()["inserted"] == 1
    assert store.rows[0]["kind"] == "signal" and store.rows[0]["features"] == {} and store.rows[0]["entry_price"] is None


# ---------------- storing ----------------
def test_insert_returns_ids_and_stores_the_fields(api):
    store, base = api
    resp = post(base, rec())
    assert resp.json() == {"received": 1, "inserted": 1, "duplicates": 0, "ids": [1]}
    row = store.rows[0]
    assert row["symbol"] == "KAYNES" and row["direction"] == "BEARISH" and row["features"]["pcr"] == 0.63 and row["entry_price"] == 3515.0


def test_the_same_key_twice_is_a_harmless_duplicate(api):
    store, base = api
    post(base, rec())
    again = post(base, rec(score=99.0))                              # different content, same key: first one wins
    assert again.status_code == 200 and again.json()["duplicates"] == 1 and again.json()["ids"] == [None]
    assert len(store.rows) == 1 and store.rows[0]["score"] == 72.5


def test_batches_report_inserted_and_duplicates_separately(api):
    store, base = api
    post(base, rec(dedupe_key="a"))
    resp = post(base, [rec(dedupe_key="a"), rec(dedupe_key="b"), rec(dedupe_key="c")])
    assert resp.json()["received"] == 3 and resp.json()["inserted"] == 2 and resp.json()["duplicates"] == 1


def test_batch_size_and_body_size_limits(api):
    _, base = api
    assert post(base, [rec(dedupe_key=str(i)) for i in range(server.MAX_BATCH + 1)]).status_code == 400
    assert post(base, []).status_code == 400
    big = requests.post(base + "/v1/signals", data=b"x" * (server.MAX_BODY + 10), headers=H, timeout=10)
    assert big.status_code == 413
    import socket                                                     # an absurd claimed length is refused without waiting for a body
    host, port = base.replace("http://", "").split(":")
    with socket.create_connection((host, int(port)), timeout=10) as s:
        crlf = chr(13) + chr(10)
        request = crlf.join(["POST /v1/signals HTTP/1.1", "Host: x", f"X-Ledger-Key: {KEY}", "Content-Length: 50000000", "", ""])
        s.sendall(request.encode())
        assert b" 413 " in s.recv(200)


def test_garbage_bodies_are_400_not_500(api):
    _, base = api
    for data in (b"not json", b"\xff\xfe", b"123", b'"str"'):
        assert requests.post(base + "/v1/signals", data=data, headers=H, timeout=10).status_code == 400


def test_a_database_failure_is_a_503_so_the_client_retries(api):
    store, base = api
    store.fail_insert = True
    resp = post(base, rec())
    assert resp.status_code == 503 and "unavailable" in resp.json()["error"]


# ---------------- auth ----------------
def test_no_key_or_wrong_key_is_401_everywhere_except_health(api):
    _, base = api
    for headers in ({}, {"X-Ledger-Key": "wrong"}, {"X-Ledger-Key": KEY[:-1]}, {"X-Ledger-Key": ""}):
        assert requests.post(base + "/v1/signals", data=json.dumps(rec()), headers=headers, timeout=10).status_code == 401
        assert requests.get(base + "/v1/signals", headers=headers, timeout=10).status_code == 401
        assert requests.get(base + "/v1/summary", headers=headers, timeout=10).status_code == 401
    assert requests.get(base + "/health", timeout=10).status_code == 200


def test_health_reports_database_state_without_leaking_anything(api):
    store, base = api
    assert requests.get(base + "/health", timeout=10).json() == {"ok": True, "version": server.VERSION}
    store.up = False
    r = requests.get(base + "/health", timeout=10)
    assert r.status_code == 503 and r.json()["ok"] is False


def test_unknown_paths_are_404(api):
    _, base = api
    assert requests.get(base + "/nope", headers=H, timeout=10).status_code == 404
    assert requests.post(base + "/v1/other", data="{}", headers=H, timeout=10).status_code == 404
    assert requests.delete(base + "/v1/signals", headers=H, timeout=10).status_code in (404, 501)   # no delete exists


# ---------------- reading ----------------
def test_query_filters_and_limit(api):
    store, base = api
    post(base, [rec(dedupe_key="1", symbol="A"), rec(dedupe_key="2", symbol="B", kind="rejected", action="NO TRADE"), rec(dedupe_key="3", symbol="A")])
    all_ = requests.get(base + "/v1/signals", headers=H, timeout=10).json()["signals"]
    assert len(all_) == 3
    only_a = requests.get(base + "/v1/signals?symbol=A", headers=H, timeout=10).json()["signals"]
    assert [s["symbol"] for s in only_a] == ["A", "A"]
    rej = requests.get(base + "/v1/signals?kind=rejected", headers=H, timeout=10).json()["signals"]
    assert len(rej) == 1 and rej[0]["action"] == "NO TRADE"
    assert len(requests.get(base + "/v1/signals?limit=1", headers=H, timeout=10).json()["signals"]) == 1


@pytest.mark.parametrize("qs", ["limit=0", "limit=99999", "limit=abc", "since=nonsense", "until=2026-01-01T00:00:00"])
def test_bad_query_parameters_are_400(api, qs):
    _, base = api
    assert requests.get(base + "/v1/signals?" + qs, headers=H, timeout=10).status_code == 400


def test_summary(api):
    _, base = api
    post(base, rec())
    r = requests.get(base + "/v1/summary?days=3", headers=H, timeout=10)
    assert r.status_code == 200 and r.json()["rows"][0]["count"] == 1
    assert requests.get(base + "/v1/summary?days=0", headers=H, timeout=10).status_code == 400


def test_the_server_refuses_to_start_without_a_real_key(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("LEDGER_API_KEY", "short")
    with pytest.raises(SystemExit):
        server.main()
    monkeypatch.setenv("LEDGER_API_KEY", "")
    with pytest.raises(SystemExit):
        server.main()


def test_summary_hides_selftest_rows_unless_asked(api):
    _, base = api
    post(base, rec(dedupe_key="real"))
    post(base, rec(dedupe_key="t1", source="selftest"))
    assert requests.get(base + "/v1/summary?days=3", headers=H, timeout=10).json()["rows"][0]["count"] == 1
    assert requests.get(base + "/v1/summary?days=3&include_test=1", headers=H, timeout=10).json()["rows"][0]["count"] == 2
