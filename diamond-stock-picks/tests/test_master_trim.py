"""The trimmed scrip master (dhan_client._read_master) must give every lookup exactly the answer the full file gives.
Runs against the local cached master file; skipped when there is none (a fresh checkout)."""
from datetime import date
from functools import partial

import pandas as pd
import pytest

import dhan_client as dc

pytestmark = pytest.mark.skipif(not dc._MASTER_CACHE_PATH.exists(), reason="no local scrip master file")

INDEX_NAMES = ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "BANKEX", "NIFTYNXT50")
UNDERLYINGS = ("NIFTY", "BANKNIFTY", "SENSEX", "RELIANCE", "SBIN", "HDFCBANK", "IDEA", "NOSUCHNAME")


def _clear():
    for name in ("_equity_ids", "_index_ids", "get_futures_universe"):
        fn = getattr(dc, name, None)
        if fn is not None and hasattr(fn, "cache_clear"):
            fn.cache_clear()


def _answers(monkeypatch, master: pd.DataFrame) -> dict:
    monkeypatch.setattr(dc, "_master", lambda: master.copy())
    _clear()
    out = {"equity_ids": dc._equity_ids(), "index_ids": dc._index_ids()}
    if hasattr(dc, "index_lot_size"):
        out["lot"] = {n: dc.index_lot_size(n) for n in INDEX_NAMES}
    for inst in ("FUTSTK", "FUTIDX"):
        out[inst] = dc._build_futures_table(inst).to_dict("records")
    out["universe"] = dc.get_futures_universe().to_dict("records")
    resolved = {}
    for u in UNDERLYINGS:
        try:
            resolved[u] = dc.resolve_option_underlying(u)
        except Exception as e:
            resolved[u] = f"{type(e).__name__}: {e}"
    out["resolve"] = resolved
    try:                                                    # service-specific users of the master
        import stock_scan
        out["stock_contracts"] = {k: vars(v) for k, v in stock_scan.load_contracts(date.today(), master=master.copy()).items()}
    except ImportError:
        pass
    try:
        import outcome_labeler
        out["contract_index"] = outcome_labeler.build_contract_index(master.copy())
    except ImportError:
        pass
    _clear()
    return out


def test_trimmed_master_gives_identical_lookups(monkeypatch):
    full = pd.read_csv(dc._MASTER_CACHE_PATH, low_memory=False)
    trimmed = dc._read_master()
    assert len(trimmed) < len(full) * 0.6 and trimmed.shape[1] == len(dc.MASTER_COLUMNS)
    a, b = _answers(monkeypatch, full), _answers(monkeypatch, trimmed)
    assert a.keys() == b.keys()
    for k in a:
        assert a[k] == b[k], k
    # and the comparison was not vacuous
    assert len(a["equity_ids"]) > 1000 and len(a["index_ids"]) > 10 and len(a["universe"]) > 100
