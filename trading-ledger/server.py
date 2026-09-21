"""
Trading ledger API: an append-only record of every signal the trading systems produce.

WHY: the scanners overwrite their results each cycle and Railway keeps only 7 days of logs, so nothing
measures whether a signal was right. Every system posts here (see ledger_client.py); a later labeler
adds what actually happened; the dashboard and review agent read it back.

APPEND-ONLY: there is no update or delete endpoint. A record is identified by `dedupe_key`; posting the
same key again is a harmless duplicate (so a client may safely retry).

ENDPOINTS (all except /health need the header  X-Ledger-Key: <LEDGER_API_KEY>)
  GET  /health                 liveness + database ping (no data, no key needed)
  POST /v1/signals             one record or a list of up to 200; -> {received, inserted, duplicates, ids}
  GET  /v1/signals?source=&strategy=&symbol=&kind=&action=&since=&until=&limit=   newest first, limit <= 1000
  GET  /v1/summary?days=7      counts per IST day / source / strategy / kind / action (spot a silent system);
                               rows with source='selftest' (deploy checks) are hidden unless include_test=1

RECORD FIELDS
  required: source, strategy, symbol, action, dedupe_key, ts_signal (ISO-8601 WITH a timezone offset)
  optional: kind (signal | rejected | event, default signal), direction (BULLISH | BEARISH),
            rule_version, entry_price, stop, target1, target2, score, features{}, payload{}
  `features` = every input the rule used, so a result can be reproduced and a gate can be evaluated;
  `payload` = anything else worth keeping. A rejected candidate (kind=rejected) is recorded too: it is what
  lets us measure whether a gate helped.

CONFIG (env): DATABASE_URL (Railway Postgres), LEDGER_API_KEY (>= 24 chars), PORT.
Not investment advice: this stores what the scanners said, nothing more.
"""
from __future__ import annotations

import hmac
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "1.0.0"
MAX_BODY = 512 * 1024
MAX_BATCH = 200
MAX_JSON = 64 * 1024
MAX_QUERY_LIMIT = 1000
KINDS = {"signal", "rejected", "event"}
DIRECTIONS = {"BULLISH", "BEARISH"}
STR_LIMITS = {"source": 64, "strategy": 64, "symbol": 40, "action": 40, "dedupe_key": 240, "rule_version": 80}
NUM_FIELDS = ("entry_price", "stop", "target1", "target2", "score")
REQUIRED = ("source", "strategy", "symbol", "action", "dedupe_key", "ts_signal")
ALLOWED = set(REQUIRED) | set(NUM_FIELDS) | {"kind", "direction", "rule_version", "features", "payload"}


class ValidationError(ValueError):
    """The record is malformed. Retrying the same record can never succeed, so the client drops it."""


def _clean_str(name: str, value, required: bool):
    if value is None:
        if required:
            raise ValidationError(f"{name} is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-empty string")
    value = value.strip()
    if len(value) > STR_LIMITS[name]:
        raise ValidationError(f"{name} is longer than {STR_LIMITS[name]} characters")
    return value


def _clean_num(name: str, value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} must be a number")
    if not math.isfinite(value):
        raise ValidationError(f"{name} must be finite (no NaN / Infinity)")
    return float(value)


def _clean_json(name: str, value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be an object")
    try:
        text = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as e:
        raise ValidationError(f"{name} is not valid JSON ({e})")
    if len(text) > MAX_JSON:
        raise ValidationError(f"{name} is larger than {MAX_JSON} bytes")
    return value


def parse_ts(value) -> datetime:
    if not isinstance(value, str):
        raise ValidationError("ts_signal must be an ISO-8601 string")
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("ts_signal is not a valid ISO-8601 timestamp")
    if dt.tzinfo is None:
        raise ValidationError("ts_signal must include a timezone offset (a naive time is ambiguous)")
    if dt > datetime.now(timezone.utc) + timedelta(days=2):
        raise ValidationError("ts_signal is in the future")
    return dt


def validate_signal(obj) -> dict:
    """A clean record ready to store, or ValidationError."""
    if not isinstance(obj, dict):
        raise ValidationError("a record must be a JSON object")
    unknown = sorted(set(obj) - ALLOWED)
    if unknown:
        raise ValidationError(f"unknown field(s): {', '.join(unknown)}")
    rec = {k: _clean_str(k, obj.get(k), True) for k in ("source", "strategy", "symbol", "action", "dedupe_key")}
    rec["ts_signal"] = parse_ts(obj.get("ts_signal"))
    rec["rule_version"] = _clean_str("rule_version", obj.get("rule_version"), False)
    kind = obj.get("kind", "signal")
    if kind not in KINDS:
        raise ValidationError(f"kind must be one of {sorted(KINDS)}")
    rec["kind"] = kind
    direction = obj.get("direction")
    if direction is not None and direction not in DIRECTIONS:
        raise ValidationError(f"direction must be one of {sorted(DIRECTIONS)}")
    rec["direction"] = direction
    for f in NUM_FIELDS:
        rec[f] = _clean_num(f, obj.get(f))
    rec["features"] = _clean_json("features", obj.get("features"))
    rec["payload"] = _clean_json("payload", obj.get("payload"))
    return rec


# ------------------------------------------------------------------------------------------------ storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id           BIGSERIAL PRIMARY KEY,
    dedupe_key   TEXT NOT NULL UNIQUE,
    ts_signal    TIMESTAMPTZ NOT NULL,
    ts_received  TIMESTAMPTZ NOT NULL DEFAULT now(),
    source       TEXT NOT NULL,
    strategy     TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    action       TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'signal',
    direction    TEXT,
    rule_version TEXT,
    entry_price  DOUBLE PRECISION,
    stop         DOUBLE PRECISION,
    target1      DOUBLE PRECISION,
    target2      DOUBLE PRECISION,
    score        DOUBLE PRECISION,
    features     JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload      JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS signals_ts_idx  ON signals (ts_signal DESC);
CREATE INDEX IF NOT EXISTS signals_sym_idx ON signals (symbol, ts_signal DESC);
CREATE INDEX IF NOT EXISTS signals_src_idx ON signals (source, strategy, ts_signal DESC);
CREATE TABLE IF NOT EXISTS outcomes (
    id          BIGSERIAL PRIMARY KEY,
    signal_id   BIGINT NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
    horizon     TEXT NOT NULL,
    method      TEXT NOT NULL,
    labeled_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ret_pct     DOUBLE PRECISION,
    hit_target  BOOLEAN,
    hit_stop    BOOLEAN,
    mfe_pct     DOUBLE PRECISION,
    mae_pct     DOUBLE PRECISION,
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (signal_id, horizon, method)
);
"""

_COLS = ("dedupe_key", "ts_signal", "source", "strategy", "symbol", "action", "kind", "direction", "rule_version",
         "entry_price", "stop", "target1", "target2", "score", "features", "payload")
_SELECT = "id, " + ", ".join(_COLS) + ", ts_received"
_FILTERS = {"source": "source", "strategy": "strategy", "symbol": "symbol", "kind": "kind", "action": "action"}


class PgStore:
    """Postgres storage. One short connection per call: the volume is a few hundred writes a day."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    def _conn(self):
        import psycopg

        return psycopg.connect(self.dsn, connect_timeout=5)

    def migrate(self) -> None:
        with self._conn() as c:
            c.execute(SCHEMA)

    def ping(self) -> bool:
        try:
            with self._conn() as c:
                c.execute("SELECT 1")
            return True
        except Exception:
            return False

    def insert_signals(self, recs: list[dict]) -> list:
        from psycopg.types.json import Jsonb

        ids: list = []
        sql = (f"INSERT INTO signals ({', '.join(_COLS)}) VALUES ({', '.join(['%s'] * len(_COLS))}) "
               "ON CONFLICT (dedupe_key) DO NOTHING RETURNING id")
        with self._conn() as c:                      # one transaction: a batch is all-or-nothing
            for r in recs:
                vals = [Jsonb(r[k]) if k in ("features", "payload") else r[k] for k in _COLS]
                row = c.execute(sql, vals).fetchone()
                ids.append(row[0] if row else None)
        return ids

    def query_signals(self, filters: dict, since, until, limit: int) -> list[dict]:
        where, args = [], []
        for key, col in _FILTERS.items():
            if filters.get(key):
                where.append(f"{col} = %s")
                args.append(filters[key])
        if since:
            where.append("ts_signal >= %s")
            args.append(since)
        if until:
            where.append("ts_signal < %s")
            args.append(until)
        sql = f"SELECT {_SELECT} FROM signals" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts_signal DESC, id DESC LIMIT %s"
        with self._conn() as c:
            rows = c.execute(sql, args + [limit]).fetchall()
        names = ["id", *_COLS, "ts_received"]
        out = []
        for row in rows:
            d = dict(zip(names, row))
            d["ts_signal"] = d["ts_signal"].isoformat()
            d["ts_received"] = d["ts_received"].isoformat()
            out.append(d)
        return out

    def summary(self, days: int, include_test: bool = False) -> list[dict]:
        sql = ("SELECT (ts_signal AT TIME ZONE 'Asia/Kolkata')::date AS day, source, strategy, kind, action, count(*) "
               "FROM signals WHERE ts_signal >= now() - make_interval(days => %s) AND (%s OR source <> 'selftest') "
               "GROUP BY 1, 2, 3, 4, 5 ORDER BY 1 DESC, 2, 3, 4, 5")
        with self._conn() as c:
            rows = c.execute(sql, [days, include_test]).fetchall()
        return [{"day": str(r[0]), "source": r[1], "strategy": r[2], "kind": r[3], "action": r[4], "count": r[5]} for r in rows]


# ------------------------------------------------------------------------------------------------ HTTP
def make_handler(store, api_key: str):
    class Handler(BaseHTTPRequestHandler):
        timeout = 20
        server_version = "ledger/" + VERSION

        def log_message(self, fmt, *args):      # never log request lines: they can carry query strings
            pass

        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self) -> bool:
            given = self.headers.get("X-Ledger-Key", "")
            return hmac.compare_digest(given.encode("utf-8"), api_key.encode("utf-8"))

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/health":
                ok = store.ping()
                return self._send(200 if ok else 503, {"ok": ok, "version": VERSION})
            if not self._authorised():
                return self._send(401, {"error": "missing or wrong X-Ledger-Key"})
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/v1/signals":
                    limit = int(q.get("limit", 100))
                    if not 1 <= limit <= MAX_QUERY_LIMIT:
                        raise ValidationError(f"limit must be 1..{MAX_QUERY_LIMIT}")
                    since = parse_ts(q["since"]) if q.get("since") else None
                    until = parse_ts(q["until"]) if q.get("until") else None
                    return self._send(200, {"signals": store.query_signals(q, since, until, limit)})
                if url.path == "/v1/summary":
                    days = int(q.get("days", 7))
                    if not 1 <= days <= 400:
                        raise ValidationError("days must be 1..400")
                    return self._send(200, {"days": days, "rows": store.summary(days, q.get("include_test") == "1")})
            except (ValidationError, ValueError) as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(503, {"error": f"storage unavailable ({type(e).__name__})"})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if urlparse(self.path).path != "/v1/signals":
                return self._send(404, {"error": "not found"})
            if not self._authorised():
                return self._send(401, {"error": "missing or wrong X-Ledger-Key"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._send(400, {"error": "bad Content-Length"})
            if length > MAX_BODY:
                if length <= 4 * MAX_BODY:          # read and discard a moderately oversized body so the client gets a clean 413
                    try:
                        self.rfile.read(length)
                    except Exception:
                        pass
                self.close_connection = True         # an absurd claimed size is answered at once, never read
                return self._send(413, {"error": f"body must be 1..{MAX_BODY} bytes"})
            if length <= 0:
                return self._send(400, {"error": f"body must be 1..{MAX_BODY} bytes"})
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._send(400, {"error": "body is not valid JSON"})
            items = body if isinstance(body, list) else [body]
            if not items or len(items) > MAX_BATCH:
                return self._send(400, {"error": f"send 1..{MAX_BATCH} records per request"})
            try:
                recs = [validate_signal(it) for it in items]
            except ValidationError as e:
                return self._send(400, {"error": str(e)})       # nothing is stored if any record in the batch is bad
            try:
                ids = store.insert_signals(recs)
            except Exception as e:
                return self._send(503, {"error": f"storage unavailable ({type(e).__name__})"})   # 5xx: the client retries
            inserted = sum(i is not None for i in ids)
            return self._send(200, {"received": len(recs), "inserted": inserted, "duplicates": len(recs) - inserted, "ids": ids})

    return Handler


class Server(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


def make_server(store, api_key: str, port: int, host: str = "0.0.0.0") -> Server:
    return Server((host, port), make_handler(store, api_key))


def main() -> None:
    dsn = os.environ.get("DATABASE_URL", "")
    key = os.environ.get("LEDGER_API_KEY", "")
    if not dsn:
        sys.exit("DATABASE_URL is not set")
    if len(key) < 24:
        sys.exit("LEDGER_API_KEY must be set and at least 24 characters (refusing to start without a real key)")
    store = PgStore(dsn)
    for attempt in range(1, 21):                       # the database may still be starting right after a deploy
        try:
            store.migrate()
            break
        except Exception as e:
            print(f"database not ready ({type(e).__name__}); retry {attempt}/20", flush=True)
            time.sleep(3)
    else:
        sys.exit("database never became reachable")
    port = int(os.environ.get("PORT", "8080"))
    print(f"trading-ledger {VERSION} listening on :{port}", flush=True)
    make_server(store, key, port).serve_forever()


if __name__ == "__main__":
    main()
