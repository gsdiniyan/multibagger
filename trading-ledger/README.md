# trading-ledger

Append-only log of every signal the trading systems produce (and the ones they rejected), stored in Postgres on Railway,
so accuracy can be measured later. Nothing here trades or changes any scanner.

- **API** (`server.py`, Railway service `ledger-api`): `POST /v1/signals` (one record or a list, at most 200), `GET /v1/signals`, `GET /v1/summary`, `GET /health`.
  Every call except `/health` needs the `X-Ledger-Key` header (Railway variable `LEDGER_API_KEY` on `ledger-api`).
- **Idempotent**: `dedupe_key` is unique, the first record wins, a repeat answers "duplicate".
- **Client** (`ledger_client.py`): copied into each service. `record()` never raises and never waits on the network; records
  wait in a spool file under `STATE_DIR` when the ledger is down. Off unless `LEDGER_URL` and `LEDGER_API_KEY` are set.
- **Connected**: options scanner, diamond-directional, diamond-stock-picks. Each shows its client state under `ledger` in `/status`.
- **Record format**: see the top of `server.py` (required: source, strategy, symbol, action, dedupe_key, ts_signal with timezone).
  `kind` is `signal`, `rejected` or `event`.

Tests: `python -m pytest test_server.py test_ledger_client.py` (60, no network or database needed).

## Outcomes (added 2026-09-21)
- `POST /v1/outcomes` records what happened to a signal (named by its `dedupe_key`; re-posting the same signal, horizon and method
  replaces the label). `GET /v1/outcomes` returns labels joined to their signals; `GET /v1/unlabeled?method=&horizon=` lists signals
  still waiting.
- **Labeler**: `outcome_labeler.py` (in the options-scanner service) runs weekdays at 16:05 IST for the options-scanner and
  diamond-directional signals. Method `premium_20_15_v1`, horizon `intraday_1515`: entry at the recorded premium, exit by 15:15,
  win = +20% before -15%, from Dhan's 1-minute candles of the option contract. Status is under `labeler` in that service's `/status`.
- Not covered yet: SENSEX options (BSE), signals whose contract has already expired (`no_contract`), stock picks (a 90-day design).
