"""
Fire-and-forget client for the trading ledger. One file, copied into each service (needs only `requests`).

THE RULE: recording a signal must never slow down, block, or break the system that produced it.
  * record() returns immediately and NEVER raises, whatever it is given.
  * Sending happens on a background thread. If the ledger is down, slow, or rejects the key, the records
    wait in a small local file (the "spool") and are sent later; a restart does not lose them.
  * If LEDGER_URL or LEDGER_API_KEY is not set the client is disabled and record() does nothing,
    so a service can ship this code before the ledger is switched on for it.

USAGE
    from ledger_client import LedgerClient
    ledger = LedgerClient(service="options-scanner")          # reads LEDGER_URL / LEDGER_API_KEY / STATE_DIR
    ledger.record({"source": "options-scanner", "strategy": "directional_options", "symbol": "KAYNES",
                   "action": "BUY PE", "dedupe_key": "...", "ts_signal": now_ist.isoformat(), ...})

The record format is documented in server.py. `dedupe_key` makes a retry safe: the ledger keeps the first
copy and answers "duplicate" to any later one.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from pathlib import Path

import requests

NUMERIC = ("entry_price", "stop", "target1", "target2", "score")


def _clean(value):
    """NaN / Infinity are not valid JSON and the ledger rejects them: turn them into null, recursively."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


class LedgerClient:
    def __init__(self, service: str = "service", url: str | None = None, key: str | None = None,
                 spool_dir: str | None = None, timeout: float = 8.0, batch_size: int = 100,
                 max_pending: int = 5000, flush_seconds: float = 2.0, backoff_max: float = 300.0, backoff_min: float = 30.0):
        self.service = service
        self.url = (url if url is not None else os.environ.get("LEDGER_URL", "")).rstrip("/")
        self.key = key if key is not None else os.environ.get("LEDGER_API_KEY", "")
        self.enabled = bool(self.url and self.key)
        self.timeout, self.batch_size, self.max_pending = timeout, batch_size, max_pending
        self.flush_seconds, self.backoff_max, self.backoff_min = flush_seconds, backoff_max, backoff_min
        base = spool_dir or os.environ.get("STATE_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
        self.spool_path = Path(base) / f"ledger_spool_{service}.jsonl"
        self._pending: deque = deque()
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()   # only one sender at a time: two senders would delete each other's unsent records
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._stop = False
        self._backoff = 0.0
        self.sent = self.dropped = self.rejected = 0
        self.last_error = ""
        if self.enabled:
            self._load_spool()

    # ------------------------------------------------------------------ public
    def record(self, rec: dict) -> None:
        """Queue one record. Never raises, never blocks on the network."""
        try:
            if not self.enabled:
                return
            rec = _clean(dict(rec))
            json.dumps(rec, default=str, allow_nan=False)      # a record that cannot be serialized would fail every send and block the queue
            with self._lock:
                self._pending.append(rec)
                while len(self._pending) > self.max_pending:
                    self._pending.popleft()
                    self.dropped += 1
                self._append_spool(rec)
            self._ensure_thread()
            self._wake.set()
        except Exception as e:                    # a bug here must never reach the caller
            self.last_error = f"record failed: {type(e).__name__}: {e}"

    def record_many(self, recs) -> None:
        for r in recs:
            self.record(r)

    def flush(self, timeout: float = 30.0) -> bool:
        """Try to send everything now (used by tests and at shutdown). True if nothing is left pending."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not self._pending:
                    return True
            if not self._send_once():
                time.sleep(0.05)
        with self._lock:
            return not self._pending

    def stats(self) -> dict:
        with self._lock:
            return {"enabled": self.enabled, "pending": len(self._pending), "sent": self.sent,
                    "dropped": self.dropped, "rejected": self.rejected, "last_error": self.last_error}

    def close(self) -> None:
        self._stop = True
        self._wake.set()

    # ------------------------------------------------------------------ spool file
    def _load_spool(self) -> None:
        try:
            if self.spool_path.exists():
                for line in self.spool_path.read_text(encoding="utf-8").splitlines():
                    try:
                        self._pending.append(json.loads(line))
                    except ValueError:
                        continue                  # a half-written line from a crash: skip it
        except Exception as e:
            self.last_error = f"spool load failed: {type(e).__name__}: {e}"

    def _append_spool(self, rec: dict) -> None:
        try:
            self.spool_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.spool_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str, allow_nan=False) + "\n")
        except Exception as e:
            self.last_error = f"spool write failed: {type(e).__name__}: {e}"   # still queued in memory

    def _rewrite_spool(self) -> None:
        """Called with the lock held: replace the file with what is still pending (atomic)."""
        try:
            self.spool_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.spool_path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(r, default=str, allow_nan=False) + "\n" for r in self._pending), encoding="utf-8")
            os.replace(tmp, self.spool_path)
        except Exception as e:
            self.last_error = f"spool rewrite failed: {type(e).__name__}: {e}"

    # ------------------------------------------------------------------ sending
    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name=f"ledger-{self.service}", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop:
            self._wake.wait(timeout=self.flush_seconds)
            self._wake.clear()
            try:
                if self._backoff:
                    time.sleep(self._backoff)
                while not self._stop and self._send_once():
                    pass
            except Exception as e:                # the sender thread must never die
                self.last_error = f"sender error: {type(e).__name__}: {e}"

    def _post(self, batch: list) -> requests.Response:
        return requests.post(self.url + "/v1/signals", data=json.dumps(batch, default=str, allow_nan=False),
                             headers={"X-Ledger-Key": self.key, "Content-Type": "application/json"}, timeout=self.timeout)

    def _send_once(self) -> bool:
        with self._send_lock:
            return self._send_locked()

    def _send_locked(self) -> bool:
        """Send one batch. True if progress was made (so the caller may immediately try the next batch)."""
        with self._lock:
            batch = list(self._pending)[: self.batch_size]
        if not batch:
            return False
        try:
            resp = self._post(batch)
        except Exception as e:                    # network down / timeout: keep everything, back off
            self.last_error = f"send failed: {type(e).__name__}"
            self._backoff = min(max(self._backoff * 2, self.backoff_min), self.backoff_max)
            return False
        code = resp.status_code
        if code == 200:
            self._done(len(batch))
            self._backoff = 0.0
            self.sent += len(batch)
            return True
        if code in (400, 413, 422):               # something in the batch is malformed: find it, keep the rest
            return self._isolate(batch)
        self.last_error = f"ledger answered {code}"          # 401/403 (bad key), 5xx, 429: keep records, back off
        self._backoff = min(max(self._backoff * 2, self.backoff_min), self.backoff_max)
        return False

    def _isolate(self, batch: list) -> bool:
        progressed = False
        for rec in batch:
            try:
                resp = self._post([rec])
            except Exception as e:
                self.last_error = f"send failed: {type(e).__name__}"
                return progressed
            if resp.status_code == 200:
                self._done(1)
                self.sent += 1
                progressed = True
            elif resp.status_code in (400, 413, 422):
                self.last_error = f"dropped a record the ledger refused: {resp.text[:160]}"
                self._done(1)
                self.rejected += 1
                progressed = True
            else:
                self.last_error = f"ledger answered {resp.status_code}"
                return progressed
        return progressed

    def _done(self, n: int) -> None:
        with self._lock:
            for _ in range(min(n, len(self._pending))):
                self._pending.popleft()
            self._rewrite_spool()
