"""Offline tests for the outcome endpoints (POST /v1/outcomes, GET /v1/outcomes, GET /v1/unlabeled)."""
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
import requests

import server
from test_server import KEY, FakeStore, H, IST, post, rec


class OutcomeStore(FakeStore):
    """Same behaviour as the SQL: upsert on (signal, horizon, method), unknown signals skipped, NOT EXISTS for unlabeled."""

    def __init__(self):
        super().__init__()
        self.outcomes = {}
        self.fail_outcomes = False

    def insert_outcomes(self, recs):
        if self.fail_outcomes:
            raise RuntimeError("db down")
        out = []
        for r in recs:
            sig = next((x for x in self.rows if x["dedupe_key"] == r["dedupe_key"]), None)
            if not sig:
                out.append("unknown_signal")
                continue
            key = (sig["id"], r["horizon"], r["method"])
            out.append("updated" if key in self.outcomes else "inserted")
            self.outcomes[key] = r
        return out

    def query_outcomes(self, filters, since, until, limit):
        rows = []
        for (sid, horizon, method), o in self.outcomes.items():
            sig = next(x for x in self.rows if x["id"] == sid)
            if any(filters.get(k) and sig[k] != filters[k] for k in ("source", "strategy", "symbol", "action")):
                continue
            if any(filters.get(k) and v != filters[k] for k, v in (("method", method), ("horizon", horizon))):
                continue
            rows.append({**{k: sig[k] for k in ("id", "dedupe_key", "source", "strategy", "symbol", "action")}, "horizon": horizon,
                         "method": method, "ret_pct": o["ret_pct"], "hit_target": o["hit_target"], "detail": o["detail"]})
        return rows[:limit]

    def query_unlabeled(self, filters, since, until, method, horizon, limit):
        done = {sid for (sid, h, m) in self.outcomes if (m, h) == (method, horizon)}
        return [x for x in self.rows if x["kind"] == "signal" and x["id"] not in done
                and all(not filters.get(k) or x[k] == filters[k] for k in ("source", "strategy", "symbol", "action"))][:limit]


@pytest.fixture()
def api():
    store = OutcomeStore()
    srv = server.make_server(store, KEY, 0, "127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield store, base
    srv.shutdown()
    srv.server_close()


def out(**kw):
    base = {"dedupe_key": "options-scanner:2026-09-21:KAYNES:PE", "horizon": "intraday_1515", "method": "premium_20_15_v1",
            "ret_pct": 20.0, "hit_target": True, "hit_stop": False, "mfe_pct": 31.5, "mae_pct": -4.0,
            "detail": {"result": "win", "entry_prem": 98.15, "minutes_to_hit": 15}}
    base.update(kw)
    return base


def post_out(base, body, headers=H):
    return requests.post(base + "/v1/outcomes", data=json.dumps(body), headers={**headers, "Content-Type": "application/json"}, timeout=10)


def test_an_outcome_is_stored_against_its_signal_and_read_back_joined(api):
    store, base = api
    assert post(base, rec()).status_code == 200
    r = post_out(base, out())
    assert r.status_code == 200 and r.json() == {"received": 1, "inserted": 1, "updated": 0, "unknown_signals": []}
    got = requests.get(base + "/v1/outcomes?method=premium_20_15_v1", headers=H, timeout=10).json()["outcomes"]
    assert len(got) == 1 and got[0]["symbol"] == "KAYNES" and got[0]["ret_pct"] == 20.0 and got[0]["detail"]["result"] == "win"


def test_labeling_again_replaces_the_label_instead_of_adding_a_second_one(api):
    store, base = api
    post(base, rec())
    post_out(base, out())
    r = post_out(base, out(ret_pct=-15.0, hit_target=False, hit_stop=True, detail={"result": "loss"})).json()
    assert r["inserted"] == 0 and r["updated"] == 1
    got = requests.get(base + "/v1/outcomes", headers=H, timeout=10).json()["outcomes"]
    assert len(got) == 1 and got[0]["ret_pct"] == -15.0 and got[0]["detail"]["result"] == "loss"


def test_a_different_method_or_horizon_is_a_separate_label(api):
    store, base = api
    post(base, rec())
    post_out(base, [out(), out(method="premium_30_20_v1"), out(horizon="close")])
    assert len(requests.get(base + "/v1/outcomes", headers=H, timeout=10).json()["outcomes"]) == 3
    assert len(requests.get(base + "/v1/outcomes?method=premium_30_20_v1", headers=H, timeout=10).json()["outcomes"]) == 1


def test_an_unknown_signal_is_reported_and_the_rest_of_the_batch_is_stored(api):
    store, base = api
    post(base, rec())
    r = post_out(base, [out(), out(dedupe_key="never-recorded")]).json()
    assert r["inserted"] == 1 and r["unknown_signals"] == ["never-recorded"]


def test_unlabeled_lists_only_signals_without_an_outcome_for_that_method(api):
    store, base = api
    post(base, [rec(dedupe_key="a", symbol="A"), rec(dedupe_key="b", symbol="B"),
                rec(dedupe_key="c", symbol="C", kind="rejected")])
    q = "/v1/unlabeled?method=premium_20_15_v1&horizon=intraday_1515"
    assert sorted(s["symbol"] for s in requests.get(base + q, headers=H, timeout=10).json()["signals"]) == ["A", "B"]     # rejected is not labeled
    post_out(base, out(dedupe_key="a"))
    assert [s["symbol"] for s in requests.get(base + q, headers=H, timeout=10).json()["signals"]] == ["B"]
    other = requests.get(base + "/v1/unlabeled?method=other&horizon=intraday_1515", headers=H, timeout=10).json()["signals"]
    assert len(other) == 2                                                                            # a new method starts unlabeled


def test_unlabeled_needs_method_and_horizon(api):
    _, base = api
    assert requests.get(base + "/v1/unlabeled", headers=H, timeout=10).status_code == 400
    assert requests.get(base + "/v1/unlabeled?method=x", headers=H, timeout=10).status_code == 400


@pytest.mark.parametrize("bad", [
    {"horizon": None}, {"method": ""}, {"dedupe_key": 5}, {"ret_pct": "20"}, {"ret_pct": float("nan")}, {"hit_target": "yes"},
    {"hit_stop": 1}, {"detail": [1]}, {"unexpected": 1}, {"horizon": "h" * 41}, {"method": "m" * 81},
])
def test_malformed_outcomes_are_rejected_and_nothing_is_stored(api, bad):
    store, base = api
    post(base, rec())
    body = json.dumps([out(), {**out(dedupe_key="x"), **bad}], allow_nan=True)
    r = requests.post(base + "/v1/outcomes", data=body, headers={**H, "Content-Type": "application/json"}, timeout=10)
    assert r.status_code == 400 and store.outcomes == {}


def test_optional_numbers_may_be_null_and_a_timeout_label_is_valid(api):
    store, base = api
    post(base, rec())
    r = post_out(base, out(ret_pct=None, hit_target=None, hit_stop=None, mfe_pct=None, mae_pct=None, detail={"result": "no_data"}))
    assert r.status_code == 200 and r.json()["inserted"] == 1


def test_outcomes_need_the_key(api):
    _, base = api
    assert post_out(base, out(), headers={}).status_code == 401
    assert requests.get(base + "/v1/outcomes", timeout=10).status_code == 401
    assert requests.get(base + "/v1/unlabeled?method=a&horizon=b", headers={"X-Ledger-Key": "x" * 40}, timeout=10).status_code == 401


def test_batch_limits_and_storage_failures(api):
    store, base = api
    post(base, rec())
    assert post_out(base, [out()] * 201).status_code == 400
    assert post_out(base, []).status_code == 400
    store.fail_outcomes = True
    assert post_out(base, out()).status_code == 503              # the labeler retries on 5xx
    assert requests.get(base + "/v1/outcomes?limit=0", headers=H, timeout=10).status_code == 400
    assert requests.get(base + "/v1/outcomes?limit=1001", headers=H, timeout=10).status_code == 400


def test_signals_endpoint_is_unaffected(api):
    store, base = api
    assert post(base, rec()).json()["inserted"] == 1 and post(base, rec()).json()["duplicates"] == 1
    assert requests.get(base + "/health", timeout=10).json()["version"] == server.VERSION == "1.1.0"
