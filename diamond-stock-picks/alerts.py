"""Alert generation and persisted state (JSON file under STATE_DIR)."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime

MAX_ALERTS = 200


def parse_holdings(text: str) -> dict[str, dict[str, float]]:
    """'DIVISLAB:3@8570, SBIN:27@990' -> {'DIVISLAB': {'qty': 3, 'buy': 8570.0}, ...}"""
    out: dict[str, dict[str, float]] = {}
    for part in (text or "").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            sym, rest = part.split(":")
            qty, buy = rest.split("@")
            out[sym.strip().upper().replace(".NS", "")] = {"qty": int(qty), "buy": float(buy)}
        except ValueError:
            continue  # a malformed entry is skipped, not fatal
    return out


class State:
    def __init__(self, directory: str):
        self.path = os.path.join(directory, "state.json")
        os.makedirs(directory, exist_ok=True)
        self.data: dict = {"active": {}, "list_date": None, "alerts": [], "fundamentals": {}}
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written file

    def add_alert(self, kind: str, symbol: str, message: str, day: str, severity: str = "info") -> bool:
        """Append unless the same (kind, symbol, day) is already recorded. Returns True if new."""
        for a in self.data["alerts"]:
            if a["kind"] == kind and a["symbol"] == symbol and a["day"] == day:
                return False
        self.data["alerts"].insert(0, {
            "at": datetime.now().isoformat(timespec="seconds"), "day": day, "kind": kind,
            "symbol": symbol, "severity": severity, "message": message,
        })
        del self.data["alerts"][MAX_ALERTS:]
        return True


def diff_lists(prev: dict[str, list[str]], new: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """(added, dropped) across the union of both strategies."""
    old = {s for v in prev.values() for s in v}
    cur = {s for v in new.values() for s in v}
    return sorted(cur - old), sorted(old - cur)


def check_holdings(holdings: dict, last_close: dict[str, float], active: set[str], stop_pct: float) -> list[dict]:
    rows = []
    for sym, h in holdings.items():
        px = last_close.get(sym)
        if px is None or px != px:
            rows.append({"symbol": sym, "status": "NO PRICE", **h})
            continue
        stop = h["buy"] * (1 - stop_pct / 100)
        status = "STOP HIT" if px <= stop else ("HELD" if sym in active else "NOT ON LIST")
        rows.append({
            "symbol": sym, "qty": h["qty"], "buy": h["buy"], "last": round(px, 2),
            "pnl_pct": round((px / h["buy"] - 1) * 100, 2), "pnl_rs": round((px - h["buy"]) * h["qty"]),
            "stop": round(stop, 2), "status": status,
        })
    return rows
