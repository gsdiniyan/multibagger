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
