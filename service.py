"""
Always-on Railway wrapper around main.py's screener pipeline.

WHY A SEPARATE FILE, NOT main.py CHANGED: main.py is a CLI script
(argparse against real sys.argv, sys.exit(0) at the end) built to be run
by hand or from a GitHub Actions/Pages job. Rather than refactor it into
an importable function - risking the working pipeline for a shape it
was never written in - this runs it exactly as a human would, as a
subprocess, on a schedule, and reads back the CSVs it already writes.
main.py itself is untouched.

WHAT THIS SERVICE DOES
  1. Runs `python3 main.py --output-dir <OUTPUT_DIR> --top <SCAN_TOP_N>`
     once immediately on startup (so a fresh deploy has data right away,
     not a day-old blank dashboard card), then once a day at RUN_HOUR_IST
     (default 18:00 IST - after the 15:30 NSE close, technicals want a
     completed day's candle; fundamentals from screener.in barely move
     intraday anyway, so once a day is plenty and keeps scraping load
     low).
  2. Serves GET /status - JSON built from whatever main.py's own pipeline
     last wrote (final.csv if technicals succeeded, else scored.csv,
     else raw_data.csv's error breakdown) - for trading-dashboard's own
     card. Top SCAN_TOP_N by multibagger_score, plus the last run's
     outcome.
  3. Serves GET / (and /report) - main.py's own generated index.html
     report as-is, so the full DataTables view is reachable directly too,
     not just the dashboard's trimmed table.

OUTPUT_DIR should point at a mounted Railway volume (e.g. /app/state/
output) - otherwise every redeploy loses the last scan and the dashboard
card goes blank until the next scheduled run, up to 24h later.

SCREENER.IN RISK, STATED PLAINLY: this site has no official API. Running
the scrape daily from a cloud IP (rather than occasionally from a home
connection) raises the odds of an IP-based block over time. main.py
already degrades to a diagnostic-only empty report rather than crashing
if that happens (see its own STEP 2 handling) - /status surfaces that
via last_run.notes rather than hiding it, so a block reads as "0 fetched,
see notes" and not as a silent stale card.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", str(BASE_DIR / "output")))
RUN_STATE_PATH = OUTPUT_DIR / "run_state.json"
RUN_HOUR_IST = int(os.environ.get("RUN_HOUR_IST", "18"))
SCAN_TOP_N = int(os.environ.get("SCAN_TOP_N", "50"))
SCAN_TIMEOUT_SECONDS = int(os.environ.get("SCAN_TIMEOUT_SECONDS", str(30 * 60)))
DEFAULT_PORT = 8080

_IST_TZ = timezone(timedelta(hours=5, minutes=30))


def _ist_now() -> datetime:
    return datetime.now(_IST_TZ)


PREVIOUS_PICKS_PATH = OUTPUT_DIR / "previous_final.csv"


def _snapshot_previous_picks() -> None:
    """Copies whatever the LAST run wrote (before main.py overwrites it
    with today's fresh output) into PREVIOUS_PICKS_PATH, so /status can
    diff today's list against yesterday's. main.py always writes the
    same filenames every run - there was no history at all before this,
    which is exactly why "what changed since yesterday" couldn't be
    shown. No-op on the very first run ever (nothing to snapshot yet)."""
    for name in ("final.csv", "scored.csv"):
        path = OUTPUT_DIR / name
        if path.exists():
            try:
                shutil.copy2(path, PREVIOUS_PICKS_PATH)
            except Exception as e:
                print(f"Failed to snapshot previous picks: {e}")
            return


def _run_scan() -> None:
    """Runs main.py exactly as a human would from the command line, then
    records the outcome. Never raises - a scan that crashes is recorded
    as a failed run, not a dead service (the scheduler loop below keeps
    going either way)."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _snapshot_previous_picks()
    started = _ist_now()
    print(f"[{started.isoformat()}] Starting scan -> {OUTPUT_DIR}")
    try:
        proc = subprocess.run(
            [sys.executable, "main.py", "--output-dir", str(OUTPUT_DIR), "--top", str(SCAN_TOP_N)],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=SCAN_TIMEOUT_SECONDS,
        )
        ok = proc.returncode == 0
        # main.py exits 0 even on a diagnostic-only empty report (see its
        # own docstring) - returncode alone can't say "good" vs "empty",
        # so /status separately checks how many rows actually came back.
        tail = "\n".join((proc.stdout or "").splitlines()[-40:])
        err_tail = "\n".join((proc.stderr or "").splitlines()[-40:])
    except subprocess.TimeoutExpired:
        ok, tail, err_tail = False, "", f"Timed out after {SCAN_TIMEOUT_SECONDS}s"
    except Exception as e:
        ok, tail, err_tail = False, "", f"{type(e).__name__}: {e}"

    finished = _ist_now()
    state = {
        "started": started.isoformat(), "finished": finished.isoformat(),
        "process_ok": ok, "stdout_tail": tail, "stderr_tail": err_tail,
    }
    try:
        tmp = RUN_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(RUN_STATE_PATH)
    except Exception as e:
        print(f"Failed to write run_state.json: {e}")
    print(f"[{finished.isoformat()}] Scan finished, process_ok={ok}")


def _seconds_until_next_run() -> float:
    now = _ist_now()
    target = now.replace(hour=RUN_HOUR_IST, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _scheduler_loop() -> None:
    _run_scan()  # immediate first run, so a fresh deploy isn't blank for up to 24h
    while True:
        wait_s = _seconds_until_next_run()
        print(f"Next scan in {wait_s / 3600:.1f}h (at {RUN_HOUR_IST:02d}:00 IST)")
        time.sleep(wait_s)
        _run_scan()


def _load_run_state() -> dict:
    try:
        return json.loads(RUN_STATE_PATH.read_text())
    except Exception:
        return {}


_PICK_COLS = (
    "symbol", "name", "sector", "current_price", "multibagger_score",
    "technical_score", "supertrend_daily_signal", "supertrend_weekly_signal",
    "roe", "roce", "promoter_holding", "rationale",
)


def _load_top_picks(path: Path) -> list[dict]:
    """Same ranking/column logic _build_status() always used, factored
    out so today's picks and yesterday's snapshot are read identically -
    a diff comparing differently-shaped data would be misleading."""
    if not path.exists():
        return []
    try:
        df = pd.read_csv(path)
    except Exception:
        return []
    if df.empty:
        return []
    sort_col = "multibagger_score" if "multibagger_score" in df.columns else df.columns[0]
    ranked = df.sort_values(sort_col, ascending=False)
    cols = [c for c in _PICK_COLS if c in ranked.columns]
    return ranked[cols].head(SCAN_TOP_N).to_dict(orient="records")


def _diff_picks(today: list[dict], previous: list[dict]) -> dict:
    """New symbols, dropped symbols, and score moves for symbols in both
    lists - the actual "what changed since yesterday" a long, flat table
    can't show on its own. Empty `previous` (no snapshot yet, e.g. the
    very first run) yields an honest empty diff, not a misleading
    "everything is new"."""
    if not previous:
        return {"new": [], "dropped": [], "moved": [], "has_previous": False}

    today_by_symbol = {p.get("symbol"): p for p in today if p.get("symbol")}
    prev_by_symbol = {p.get("symbol"): p for p in previous if p.get("symbol")}

    new = [s for s in today_by_symbol if s not in prev_by_symbol]
    dropped = [s for s in prev_by_symbol if s not in today_by_symbol]

    moved = []
    for sym, pick in today_by_symbol.items():
        prev_pick = prev_by_symbol.get(sym)
        if prev_pick is None:
            continue
        today_score, prev_score = pick.get("multibagger_score"), prev_pick.get("multibagger_score")
        if today_score is None or prev_score is None:
            continue
        delta = today_score - prev_score
        if abs(delta) >= 1:  # ignore noise-level score jitter
            moved.append({"symbol": sym, "score_delta": round(delta, 1),
                           "today_score": today_score, "previous_score": prev_score})
    moved.sort(key=lambda m: abs(m["score_delta"]), reverse=True)

    return {"new": new, "dropped": dropped, "moved": moved, "has_previous": True}


def _build_status() -> dict:
    run_state = _load_run_state()
    notes: list[str] = []

    df = None
    source = None
    for name in ("final.csv", "scored.csv"):
        path = OUTPUT_DIR / name
        if path.exists():
            try:
                df = pd.read_csv(path)
                source = name
                break
            except Exception as e:
                notes.append(f"Failed to read {name}: {e}")

    if df is None or df.empty:
        notes.append("No scored candidates in the last run.")
        raw_path = OUTPUT_DIR / "raw_data.csv"
        if raw_path.exists():
            try:
                raw = pd.read_csv(raw_path)
                if "error" in raw.columns:
                    for err, count in raw["error"].value_counts().head(3).items():
                        notes.append(f"({count}x) {str(err)[:200]}")
            except Exception:
                pass
        top_picks = []
    else:
        sort_col = "multibagger_score" if "multibagger_score" in df.columns else df.columns[0]
        ranked = df.sort_values(sort_col, ascending=False)
        cols = [c for c in _PICK_COLS if c in ranked.columns]
        top_picks = ranked[cols].head(SCAN_TOP_N).to_dict(orient="records")

    previous_picks = _load_top_picks(PREVIOUS_PICKS_PATH)

    return {
        "project": "Multibagger",
        "last_run": {
            "started": run_state.get("started"),
            "finished": run_state.get("finished"),
            "process_ok": run_state.get("process_ok"),
            "source_csv": source,
            "candidate_count": 0 if df is None else len(df),
            "notes": notes,
        },
        "top_picks": top_picks,
        "changes": _diff_picks(top_picks, previous_picks),
    }


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/status",):
            body = json.dumps(_build_status()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path in ("/", "/report", "/index.html"):
            report_path = OUTPUT_DIR / "index.html"
            if report_path.exists():
                body = report_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                body = b"No report generated yet - the first scan is still running."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        pass


if __name__ == "__main__":
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    server = HTTPServer(("0.0.0.0", port), _Handler)
    print(f"Multibagger service listening on 0.0.0.0:{port}")
    server.serve_forever()
