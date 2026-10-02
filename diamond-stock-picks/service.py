"""
Always-on Railway service: the diamond_stock_engine's Steady and God's Plan picks, run on Dhan
prices, with a per-stock fundamentals check, entry reference price, quantity, stop, and alerts.

SCHEDULE
One run at startup (so /status is never empty after a deploy), then every weekday at RUN_TIME
IST (default 16:10, after the close). Prices are refreshed on every run. The pick LIST itself is
only re-screened on PICKS_REFRESH_WEEKDAY (default Monday, 0) or when there is no list yet:
the raw screen drifts about one stock a day, which is noise for a 90-day rebalance design.
Fundamentals (yfinance snapshot) are refreshed with the list. All prices, history and live
quotes come from Dhan; yfinance is only used for fundamentals and to verify split-like jumps.
While the market is open (Mon-Fri 09:15-15:30 IST) the last price is Dhan's live quote, and
every STOP_CHECK_MINUTES (default 15) a light check re-prices the list and your holdings and
raises STOP_HIT alerts intraday. It polls, so a stop can be crossed and recovered between two checks.

ALERTS (shown on the dashboard from /status, kept in STATE_DIR/state.json)
  STOP_HIT       a holding is at or below its trailing stop (stops.py: 2.5x ATR, 5-15%, below the highest close
                 since it was first seen, never lowered; STOP_PCT when a stock has no ATR)
  TWO_R          a holding closed at its 2R level (buy + 2x the starting risk): its stop is now at least the buy price
  NEW_PICK       stock entered the list at a weekly re-screen
  DROPPED_PICK   stock left the list (high severity if you hold it)
  FUNDAMENTALS   a listed stock is rated AVOID by the fundamentals check
  DATA_ERROR     Dhan returned no data (usually an expired DHAN_ACCESS_TOKEN), no NIFTY benchmark, or a re-screen
                 with no picks at all; the current list is kept, the run is retried every RETRY_MINUTES, and the
                 alert clears with the next good run

A re-screen never replaces the list with an empty one, and an empty list (from before that guard) is re-screened
on the next run rather than kept until the weekly day.

CONFIG (env): DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, CAPITAL (default 500000), STOP_ATR_MULT (2.5), STOP_PCT (10, the
stop when a stock has no ATR), HOLDINGS ("DIVISLAB:3@8570,SBIN:27@990"), STOP_CHECK_MINUTES (15), RETRY_MINUTES (30),
STATE_DIR (attach a Railway volume there to keep alerts and trailing highs across redeploys), RUN_TIME (HH:MM IST),
PICKS_REFRESH_WEEKDAY, PORT.

Not investment advice: this is the output of a momentum / low-beta screen. Backtests of the
engine are inflated by survivorship bias (today's index members applied to past years).
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import alerts as al
import fundamentals as fd
import keepalive
import ledger_hooks as lh
import picks as pk
import prices as px
import stops

IST = timezone(timedelta(hours=5, minutes=30))
CAPITAL = float(os.environ.get("CAPITAL", "500000"))
STOP_PCT = float(os.environ.get("STOP_PCT", "10"))
RUN_TIME = os.environ.get("RUN_TIME", "16:10")
REFRESH_WEEKDAY = int(os.environ.get("PICKS_REFRESH_WEEKDAY", "0"))
STATE_DIR = os.environ.get("STATE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state"))
STOP_CHECK_MINUTES = int(os.environ.get("STOP_CHECK_MINUTES", "15"))
RETRY_MINUTES = int(os.environ.get("RETRY_MINUTES", "30"))
DEFAULT_PORT = 8080

_lock = threading.Lock()
_running = threading.Lock()
_latest: dict = {"run_state": "starting", "computed_at": None}
_state = al.State(STATE_DIR)


def _now() -> datetime:
    return datetime.now(IST)


def _last_close(store: px.PriceStore) -> dict[str, float]:
    out = {}
    for col in store.close.columns:
        s = store.close[col].dropna()
        if len(s):
            out[col.replace(".NS", "")] = float(s.iloc[-1])
    return out


def _market_open(now: datetime) -> bool:
    """Mon-Fri 09:15-15:30 IST. No NSE holiday calendar: on a holiday the quote is just unchanged."""
    return now.weekday() < 5 and "09:15" <= now.strftime("%H:%M") <= "15:30"


def _decorate(rows: list[dict]) -> list[dict]:
    """Attach the latest fundamentals verdict to each pick row and sort cleanest-first."""
    fund = _state.data["fundamentals"]
    for r in rows:
        f = fund.get(r["symbol"], {})
        r.update(verdict=f.get("verdict", "n/a"), flags=f.get("flags", []), pe=f.get("pe"), pb=f.get("pb"),
                 de=None if f.get("financial") else f.get("de"), roe=f.get("roe"), sales_g=f.get("sales_g"),
                 earn_g=f.get("earn_g"), financial=bool(f.get("financial")))
    status = _state.data.get("pick_status") or {}
    for r in rows:                     # since the stock joined the list (picks.pick_status), plus today's live price
        ps = status.get(r["symbol"]) or {}
        r.update(status=ps.get("status"), status_at=ps.get("at"), list_price=ps.get("ref_price"),
                 list_date=ps.get("ref_date"), trail_stop=ps.get("stop"),
                 since_list_pct=(round((r["price"] / ps["ref_price"] - 1) * 100, 2)
                                 if ps.get("ref_price") and r.get("price") == r.get("price") else None))
        if ps.get("status") in ("OPEN", "2R REACHED") and ps.get("stop") and r.get("price") and r["price"] <= ps["stop"]:
            r["status"] = "BELOW STOP (live)"
    order = {"OK": 0, "WATCH": 1, "AVOID": 2}
    rows.sort(key=lambda r: (order.get(r["verdict"], 3), -r["invest"]))
    return rows


def _holdings_and_alerts(last_close: dict[str, float], day: str, closes: dict[str, float] | None = None) -> list[dict]:
    """`closes` are daily closes (the scheduled run): only they raise a holding's high and so its trailing stop."""
    st = _state.data
    held = al.parse_holdings(os.environ.get("HOLDINGS", ""))
    active = {s for v in st["active"].values() for s in v}
    trails = st.setdefault("trails", {})
    was_2r = {s for s, t in trails.items() if t.get("two_r_hit")}
    holdings = al.check_holdings(held, last_close, active, STOP_PCT, trails, closes, st.get("atr_pct"))
    for h in holdings:
        if h.get("two_r_hit") and h["symbol"] not in was_2r:
            _state.add_alert("TWO_R", h["symbol"], f"{h['symbol']} closed at its 2R level {h['two_r']} (bought {h['buy']}); "
                             f"stop raised to at least break-even, now {h['stop']}", day)
        if h["status"] == "STOP HIT":
            is_new = _state.add_alert("STOP_HIT", h["symbol"],
                                      f"{h['symbol']} at {h['last']} is at/below {'trailing ' if h.get('trailing') else ''}stop {h['stop']} (bought {h['buy']})", day, "high")
            if is_new:
                try:
                    lh.record([lh.stop_hit_record(h, day, _now())])
                except Exception as e:
                    print(f"ledger hook failed (ignored): {e}")
    return holdings


def _quick_check() -> None:
    """Intraday: re-price the list + holdings from Dhan live quotes and raise stop alerts."""
    if not _running.acquire(blocking=False):
        return
    try:
        with _lock:
            ready = _latest.get("run_state") == "ready"
        if not ready:
            return
        st = _state.data
        held = al.parse_holdings(os.environ.get("HOLDINGS", ""))
        symbols = sorted({s for a in st["alloc"].values() for s in a} | set(held))
        live = px.live_prices(symbols)
        if not live:
            return
        day = _now().date().isoformat()
        rows = _decorate(pk.build_rows(st["alloc"], live, STOP_PCT, st.get("atr_pct")))
        holdings = _holdings_and_alerts(live, day)
        _state.save()
        with _lock:
            _latest.update(picks=rows, holdings=holdings, alerts=st["alerts"][:50], live_at=_now().isoformat(timespec="seconds"))
    except Exception:
        traceback.print_exc()
    finally:
        _running.release()


def _run(reason: str) -> None:
    if not _running.acquire(blocking=False):
        return  # a run is already in progress
    started = _now()
    try:
        with _lock:
            _latest["run_state"] = "computing"
        _run_locked(reason, started)
    except Exception as e:  # never let one bad run kill the loop
        traceback.print_exc()
        day = started.date().isoformat()
        _state.add_alert("DATA_ERROR", "-", f"{type(e).__name__}: {e}", day, "high")
        _state.save()
        with _lock:
            _latest.update(run_state="error", error=f"{type(e).__name__}: {e}", computed_at=started.isoformat(),
                           alerts=_state.data["alerts"][:50])
    finally:
        _running.release()


def _run_locked(reason: str, started: datetime) -> None:
    symbols = pk.universe_symbols()
    store = px.fetch_all(symbols)
    px.install(store)
    last_close = _last_close(store)
    daily_close = dict(last_close)
    data_day = store.last_date.date().isoformat()
    st = _state.data
    st["atr_pct"] = {**st.get("atr_pct", {}), **store.atr_pct}
    price_source = "dhan daily close"
    # a data error is a transient state: it clears itself once a run succeeds
    st["alerts"] = [a for a in st["alerts"] if a["kind"] != "DATA_ERROR"]

    prev = st.get("active") or {}
    had_list = any(prev.values())
    # an empty list is never kept: re-screen on the next run rather than wait for the weekly day
    refresh = (not st.get("alloc")) or not had_list or (
        started.weekday() == REFRESH_WEEKDAY and st.get("list_date") != data_day)
    if refresh:
        alloc = pk.compute(store.last_date, CAPITAL)
        new_active = {n: sorted(alloc[n]) for n in alloc}
        if not any(new_active.values()):
            # a screen of the whole universe with nothing in any strategy is a data failure, not a verdict
            raise RuntimeError("the re-screen returned no picks for any strategy; kept the current list")
        held = al.parse_holdings(os.environ.get("HOLDINGS", ""))
        added: list = []
        dropped: list = []
        if had_list:
            added, dropped = al.diff_lists(prev, new_active)
        elif prev:
            # recovering from a saved empty list (2026-09-28's failed screen). Its "left the list" alerts were false,
            # but together they name exactly the list it wiped, so the new list is compared with that one: only real
            # changes are announced, not every pick "entering" again
            bad_day = st.get("list_date")
            is_false = lambda a: a["kind"] == "DROPPED_PICK" and a.get("day") == bad_day
            wiped = sorted({a["symbol"] for a in st["alerts"] if is_false(a)})
            st["alerts"] = [a for a in st["alerts"] if not is_false(a)]
            if wiped:
                added, dropped = al.diff_lists({"before": wiped}, new_active)
        for s in added:
            _state.add_alert("NEW_PICK", s, f"{s} entered the list", data_day)
        for s in dropped:
            mine = s in held
            _state.add_alert("DROPPED_PICK", s, f"{s} left the list" + (" (you hold it)" if mine else ""),
                             data_day, "high" if mine else "info")
        union = sorted({s for v in new_active.values() for s in v})
        try:
            fresh = fd.check(union)
        except Exception:
            fresh = {}
        st["fundamentals"] = {**st.get("fundamentals", {}), **fresh}
        for s in union:
            if st["fundamentals"].get(s, {}).get("verdict") == "AVOID":
                flags = "; ".join(st["fundamentals"][s].get("flags", []))
                _state.add_alert("FUNDAMENTALS", s, f"{s} rated AVOID by fundamentals check: {flags}", data_day, "warn")
        st.update(active=new_active, alloc=alloc, list_date=data_day)

    # each listed stock's own start date: kept while it stays on the list, so a weekly re-screen does not reset it
    since = st.setdefault("pick_since", {})
    on_list = {s for v in st["active"].values() for s in v}
    for s in on_list:
        since.setdefault(s, st.get("list_date") or data_day)
    for s in [s for s in since if s not in on_list]:
        del since[s]
    st["pick_status"] = pk.pick_status(store.close, since, st["atr_pct"], STOP_PCT)

    if _market_open(started):
        live = px.live_prices(sorted({s for a in st["alloc"].values() for s in a} | set(al.parse_holdings(os.environ.get("HOLDINGS", "")))))
        if live:
            last_close = {**last_close, **live}
            price_source = "dhan live quote"
    rows = _decorate(pk.build_rows(st["alloc"], last_close, STOP_PCT, st["atr_pct"]))
    holdings = _holdings_and_alerts(last_close, data_day, daily_close)
    _state.save()
    if refresh:                                # a fresh weekly list: record it (a failure here never affects the run)
        try:
            lh.record(lh.pick_records(rows, st["active"], data_day, started, STOP_PCT)
                      + lh.dropped_records(dropped, al.parse_holdings(os.environ.get("HOLDINGS", "")), data_day, started))
        except Exception as e:
            print(f"ledger hook failed (ignored): {e}")

    with _lock:
        _latest.clear()
        _latest.update(
            run_state="ready", reason=reason, computed_at=started.isoformat(), data_as_of=data_day,
            list_screened_on=st.get("list_date"), capital=CAPITAL, stop_pct=STOP_PCT,
            stop_rule=f"trailing: {stops.ATR_MULT}x ATR below the highest close, {stops.MIN_DIST:.0%}-{stops.MAX_DIST:.0%}, "
                      "never lowered; break-even once a close reaches the 2R level; no fixed target",
            price_source=price_source, universe_size=len(symbols), universe_with_prices=len(symbols) - len(store.missing),
            missing=store.missing, excluded=store.excluded,
            data_warnings=store.warnings[:20], counts={n: len(v) for n, v in st["active"].items()},
            picks=rows, holdings=holdings, alerts=st["alerts"][:50],
        )
    print(f"[{started.isoformat()}] run ({reason}) done: as_of={data_day} refresh={refresh} picks={len(rows)} "
          f"alerts={len(st['alerts'])}", flush=True)


def _loop() -> None:
    _run("startup")
    last_run_day = _now().date() if _now().strftime("%H:%M") >= RUN_TIME else None
    last_quick = last_retry = time.time()
    while True:
        time.sleep(30)
        now = _now()
        with _lock:
            failed = _latest.get("run_state") == "error"
        if now.weekday() < 5 and now.strftime("%H:%M") >= RUN_TIME and last_run_day != now.date():
            last_run_day = now.date()
            _run("scheduled")
        elif failed and time.time() - last_retry >= RETRY_MINUTES * 60:
            # a failed run (Dhan down, no benchmark) used to wait for the next day's RUN_TIME
            last_retry = time.time()
            _run("retry")
        elif _market_open(now) and time.time() - last_quick >= STOP_CHECK_MINUTES * 60:
            last_quick = time.time()
            _quick_check()


class _Handler(BaseHTTPRequestHandler):
    timeout = 20

    def do_GET(self) -> None:
        if self.path == "/status":
            with _lock:
                body = json.dumps({
                    "project": "diamond-stock-picks",
                    "strategies": ["steady", "gods_plan"],
                    "schedule": f"weekdays {RUN_TIME} IST; list re-screened weekday={REFRESH_WEEKDAY}",
                    "disclaimer": "Screen output, not investment advice. Backtests are survivorship-biased.",
                    "ledger": lh.status(),
                    **_latest,
                }, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args) -> None:
        pass


class _Server(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


def main() -> None:
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    keepalive.start()                                   # stays awake 08:50-16:45 IST weekdays when Railway sleep mode is on
    threading.Thread(target=_loop, daemon=True).start()
    print(f"diamond-stock-picks listening on :{port}", flush=True)
    _Server(("0.0.0.0", port), _Handler).serve_forever()


if __name__ == "__main__":
    main()
