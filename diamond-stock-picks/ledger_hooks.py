"""Ledger hooks for diamond-stock-picks. Every function here is safe to call: nothing raises and nothing waits on the network.

What is recorded (source "diamond-stock-picks"):
  * each stock on each list at a re-screen  -> kind signal, action BUY (entry = last close, stop = STOP_PCT below it)
  * each stock that left the list           -> kind event,  action DROPPED
  * each holding that crossed its stop      -> kind event,  action STOP_HIT
dedupe keys carry the data date, so a redeploy or a repeat run on the same data adds nothing twice.
"""
from __future__ import annotations

from datetime import datetime

try:
    from ledger_client import LedgerClient
except Exception:                             # pragma: no cover - the ledger is optional
    LedgerClient = None

SOURCE = "diamond-stock-picks"
RULE_VERSION = "diamond-stock-picks-v1"       # bump when the engine strategies or the stop rule change
STRATEGY_NAMES = {"steady": "diamond_stock_steady", "gods_plan": "diamond_stock_gods_plan"}

_ledger = None


def get_ledger():
    global _ledger
    if _ledger is None and LedgerClient is not None:
        try:
            _ledger = LedgerClient(service="diamond-stock-picks")
        except Exception as e:
            print(f"ledger client unavailable: {e}")
    return _ledger


def status() -> dict:
    try:
        client = get_ledger()                  # created on first use, so /status shows the real state before any record
        return client.stats() if client is not None else {"enabled": False}
    except Exception:
        return {"enabled": None, "error": "stats unavailable"}


def pick_records(rows: list, active: dict, data_day: str, when: datetime, stop_pct: float) -> list:
    """One signal per (strategy, stock) on the list. `rows` are picks.build_rows/_decorate output, `active` is {strategy: [symbols]}."""
    by_symbol = {r["symbol"]: r for r in (rows or []) if isinstance(r, dict) and r.get("symbol")}
    out = []
    for strategy, symbols in (active or {}).items():
        for sym in symbols or []:
            r = by_symbol.get(sym)
            if not r or not (r.get("price") or 0) > 0:
                continue
            out.append({
                "source": SOURCE, "strategy": STRATEGY_NAMES.get(strategy, f"diamond_stock_{strategy}"), "symbol": sym,
                "action": "BUY", "kind": "signal", "direction": "BULLISH", "rule_version": RULE_VERSION,
                "dedupe_key": f"{SOURCE}:{data_day}:{strategy}:{sym}:list",
                "ts_signal": when.isoformat(), "entry_price": r["price"], "stop": r.get("stop"),
                "features": {**r, "strategy": strategy, "stop_pct": stop_pct, "data_as_of": data_day},
            })
    return out


def dropped_records(dropped: list, held: dict, data_day: str, when: datetime) -> list:
    return [{
        "source": SOURCE, "strategy": "diamond_stock_picks", "symbol": s, "action": "DROPPED", "kind": "event",
        "rule_version": RULE_VERSION, "dedupe_key": f"{SOURCE}:{data_day}:{s}:dropped", "ts_signal": when.isoformat(),
        "features": {"held": s in (held or {}), "data_as_of": data_day},
    } for s in dropped or []]


def stop_hit_record(holding: dict, day: str, when: datetime) -> dict:
    return {
        "source": SOURCE, "strategy": "diamond_stock_picks", "symbol": holding["symbol"], "action": "STOP_HIT", "kind": "event",
        "rule_version": RULE_VERSION, "dedupe_key": f"{SOURCE}:{day}:{holding['symbol']}:stop_hit", "ts_signal": when.isoformat(),
        "entry_price": holding.get("buy"), "stop": holding.get("stop"), "features": dict(holding),
    }


def record(records) -> None:
    """Queue records for the ledger. Never raises, never blocks."""
    try:
        client = get_ledger()
        if client is not None and records:
            client.record_many(records)
    except Exception as e:
        print(f"ledger record failed (ignored): {type(e).__name__}: {e}")
