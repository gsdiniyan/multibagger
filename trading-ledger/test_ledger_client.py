"""Offline tests for ledger_client: a fake ledger HTTP server that can fail in every way that matters."""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ledger_client import LedgerClient  # noqa: E402

KEY = "k" * 32


class Fake:
    """Behaviour is switched from the test: status to answer with, per-record rejection, delay."""

    def __init__(self):
        self.status = 200
        self.delay = 0.0
        self.bad_symbols = set()
        self.received = []          # every record the "ledger" accepted
        self.requests = []          # (headers dict, batch)


@pytest.fixture()
def ledger():
    fake = Fake()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            fake.requests.append((dict(self.headers), body))
            if fake.delay:
                time.sleep(fake.delay)
            code = fake.status
            if code == 200 and any(r.get("symbol") in fake.bad_symbols for r in body):
                code = 400                                     # a batch with a malformed record is refused whole, like the real API
            if code == 200:
                fake.received.extend(body)
            data = json.dumps({"ok": code == 200}).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fake.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield fake
    srv.shutdown()
    srv.server_close()


def client(ledger, tmp_path, **kw):
    kw.setdefault("timeout", 2.0)
    kw.setdefault("backoff_min", 0.01)
    kw.setdefault("backoff_max", 0.05)
    kw.setdefault("flush_seconds", 0.05)
    return LedgerClient(service="t", url=ledger.url, key=KEY, spool_dir=str(tmp_path), **kw)


def rec(sym="A", n=1):
    return {"source": "s", "strategy": "x", "symbol": sym, "action": "BUY CE", "dedupe_key": f"k:{sym}:{n}",
            "ts_signal": "2026-09-21T10:00:00+05:30", "features": {"pcr": 0.5}}


def test_disabled_client_does_nothing_and_never_raises(tmp_path):
    c = LedgerClient(service="t", url="", key="", spool_dir=str(tmp_path))
    c.record(rec())
    c.record(None)                                     # even nonsense
    assert c.enabled is False and c.stats()["pending"] == 0 and not (tmp_path / "ledger_spool_t.jsonl").exists()


def test_sends_a_record_with_the_key_header(ledger, tmp_path):
    c = client(ledger, tmp_path)
    c.record(rec("KAYNES"))
    assert c.flush(10)
    headers, batch = ledger.requests[0]
    assert headers["X-Ledger-Key"] == KEY and batch[0]["symbol"] == "KAYNES"
    assert c.stats()["sent"] == 1 and c.stats()["pending"] == 0


def test_background_thread_delivers_without_flush(ledger, tmp_path):
    c = client(ledger, tmp_path)
    c.record(rec("BG"))
    deadline = time.time() + 5
    while time.time() < deadline and not ledger.received:
        time.sleep(0.05)
    assert [r["symbol"] for r in ledger.received] == ["BG"]


def test_record_returns_immediately_even_when_the_ledger_hangs(ledger, tmp_path):
    ledger.delay = 3.0
    c = client(ledger, tmp_path, timeout=0.5)
    t0 = time.time()
    for i in range(20):
        c.record(rec("SLOW", i))
    assert time.time() - t0 < 0.5                      # the caller was never held up by the network
    assert c.stats()["pending"] == 20


def test_ledger_down_keeps_records_then_delivers_when_it_returns(ledger, tmp_path):
    ledger.status = 503
    c = client(ledger, tmp_path)
    c.record(rec("A"))
    c.record(rec("B"))
    assert c.flush(0.5) is False and c.stats()["pending"] == 2 and "503" in c.stats()["last_error"]
    ledger.status = 200
    assert c.flush(10) and sorted(r["symbol"] for r in ledger.received) == ["A", "B"]


def test_unreachable_ledger_is_survived(tmp_path):
    c = LedgerClient(service="t", url="http://127.0.0.1:9", key=KEY, spool_dir=str(tmp_path), timeout=0.5, backoff_min=0.01, backoff_max=0.05)
    c.record(rec("A"))
    assert c.flush(0.6) is False and c.stats()["pending"] == 1 and "send failed" in c.stats()["last_error"]


def test_a_restart_does_not_lose_unsent_records(ledger, tmp_path):
    ledger.status = 503
    c1 = client(ledger, tmp_path)
    c1.record(rec("KEEP1"))
    c1.record(rec("KEEP2"))
    c1.close()
    ledger.status = 200
    c2 = client(ledger, tmp_path)                       # a fresh process reading the same spool file
    assert c2.stats()["pending"] == 2
    assert c2.flush(10) and {r["symbol"] for r in ledger.received} == {"KEEP1", "KEEP2"}   # a set: the ledger dedupes, an in-flight retry may repeat one
    assert (tmp_path / "ledger_spool_t.jsonl").read_text() == ""       # spool emptied after delivery


def test_a_half_written_spool_line_is_skipped_not_fatal(ledger, tmp_path):
    (tmp_path / "ledger_spool_t.jsonl").write_text(json.dumps(rec("OK")) + "\n" + '{"symbol": "TRUNC' + "\n")
    c = client(ledger, tmp_path)
    assert c.stats()["pending"] == 1 and c.flush(10) and ledger.received[0]["symbol"] == "OK"


def test_one_malformed_record_does_not_block_the_others(ledger, tmp_path):
    ledger.bad_symbols = {"BAD"}
    c = client(ledger, tmp_path)
    c.record_many([rec("G1"), rec("BAD"), rec("G2")])
    assert c.flush(10)
    assert sorted(r["symbol"] for r in ledger.received) == ["G1", "G2"]
    s = c.stats()
    assert s["sent"] == 2 and s["rejected"] == 1 and s["pending"] == 0 and "refused" in s["last_error"]


def test_a_wrong_key_keeps_records_and_does_not_drop_them(ledger, tmp_path):
    ledger.status = 401
    c = client(ledger, tmp_path)
    c.record(rec("A"))
    assert c.flush(0.4) is False and c.stats()["pending"] == 1 and c.stats()["rejected"] == 0 and "401" in c.stats()["last_error"]
    ledger.status = 200                                 # key fixed in Railway: everything queued is delivered
    assert c.flush(10) and ledger.received[0]["symbol"] == "A"


def test_nan_and_infinity_become_null_instead_of_breaking_the_send(ledger, tmp_path):
    c = client(ledger, tmp_path)
    r = rec("NAN")
    r["score"] = float("nan")
    r["features"] = {"delta": float("inf"), "deep": [1, float("-inf"), {"x": float("nan")}]}
    c.record(r)
    assert c.flush(10)
    got = ledger.received[0]
    assert got["score"] is None and got["features"]["delta"] is None and got["features"]["deep"] == [1, None, {"x": None}]


def test_unserializable_input_is_dropped_quietly_and_never_blocks_the_queue(ledger, tmp_path):
    c = client(ledger, tmp_path)
    loop = {}
    loop["self"] = loop
    c.record({**rec("LOOP"), "features": loop})         # circular reference
    c.record(rec("FINE"))
    assert c.flush(10) and [r["symbol"] for r in ledger.received] == ["FINE"]
    assert "record failed" in c.stats()["last_error"]


@pytest.mark.parametrize("bad", [None, 5, "text", [], object()])
def test_record_never_raises_for_any_input(ledger, tmp_path, bad):
    client(ledger, tmp_path).record(bad)


def test_the_queue_is_capped_and_drops_the_oldest(ledger, tmp_path):
    ledger.status = 503
    c = client(ledger, tmp_path, max_pending=5)
    for i in range(12):
        c.record(rec("Q", i))
    s = c.stats()
    assert s["pending"] == 5 and s["dropped"] == 7
    assert [r["dedupe_key"] for r in c._pending] == [f"k:Q:{i}" for i in range(7, 12)]      # the 7 oldest are the ones dropped
    ledger.status = 200
    assert c.flush(10) and {"k:Q:11"} <= {r["dedupe_key"] for r in ledger.received} and c.stats()["pending"] == 0


def test_many_threads_recording_at_once_lose_nothing(ledger, tmp_path):
    c = client(ledger, tmp_path)

    def work(t):
        for i in range(30):
            c.record(rec(f"T{t}", i))

    ts = [threading.Thread(target=work, args=(t,)) for t in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert c.flush(20)
    assert len({r["dedupe_key"] for r in ledger.received}) == 300 and c.stats()["pending"] == 0


def test_flush_racing_the_background_sender_never_deletes_unsent_records(ledger, tmp_path):
    c = client(ledger, tmp_path, batch_size=10)
    for i in range(200):
        c.record(rec("R", i))
    threads = [threading.Thread(target=lambda: c.flush(15)) for _ in range(4)]      # 4 manual flushes + the background thread
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert c.flush(15)
    assert len({r["dedupe_key"] for r in ledger.received}) == 200                    # every record arrived, none was lost
