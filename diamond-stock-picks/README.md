# diamond-stock-picks

Railway service that runs the **Steady** and **God's Plan** strategies from
[pvc1997/diamond_stock_engine](https://github.com/pvc1997/diamond_stock_engine) (MIT, subset copied
into `engine/`) on **Dhan** prices, adds a per-stock fundamentals check, an entry reference price,
quantity and stop for each pick, and raises alerts that the trading dashboard displays.

Screen output, not investment advice. Backtests of the engine are inflated by survivorship bias
(today's index members applied to past years): random 18-stock portfolios from the same list earned
about 19% a year against about 22% for these strategies, over 2016-2026.

## What it does

| Job | When |
|---|---|
| Fetch ~3.3 years of daily prices for the 170-stock universe + Nifty from Dhan, run both strategies | startup, then weekdays 16:10 IST |
| Re-screen the **list** (and refresh fundamentals) | Mondays (`PICKS_REFRESH_WEEKDAY`) or when there is no list yet |
| Re-price the list and your holdings from Dhan live quotes, raise stop alerts | every 15 min, 09:15-15:30 IST |

The raw screen drifts about one stock a day, which is noise for a 90-day rebalance design, so the
list is only re-screened weekly. Prices, quantities and stops update every run.

`GET /status` returns the picks table (price, quantity, stop, P/E, debt, ROE, growth, verdict),
your holdings, alerts, and data warnings.

## Alerts

`STOP_HIT` (holding at/below buy x (1 - STOP_PCT/100)), `NEW_PICK`, `DROPPED_PICK` (high severity if
you hold it), `FUNDAMENTALS` (a listed stock rated AVOID), `DATA_ERROR` (usually an expired Dhan token).

## Configuration (Railway variables)

| Variable | Default | Meaning |
|---|---|---|
| `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN` | required | Dhan credentials (token expires about daily) |
| `HOLDINGS` | empty | what you own: `DIVISLAB:3@8570,SBIN:27@990` (symbol:qty@buy price) |
| `CAPITAL` | 500000 | rupees the engine splits across the picks (sets quantities) |
| `STOP_PCT` | 10 | stop distance below buy price / reference price |
| `STOP_CHECK_MINUTES` | 15 | intraday check interval |
| `RUN_TIME` | 16:10 | daily run, IST |
| `PICKS_REFRESH_WEEKDAY` | 0 | 0 = Monday |
| `STATE_DIR` | `./state` | alerts and the active list; mount a Railway volume here to keep them across deploys |

## Data notes

* Dhan daily bars are stamped midnight IST but read as UTC (one day early): corrected in `prices.py`.
* Dhan prices are not adjusted for splits/bonuses/demergers. A day where Dhan's move differs from
  yfinance's by more than 15% is scaled; if it cannot be verified the stock is excluded and listed
  in `/status` under `excluded`. Dividends are not adjusted.
* yfinance is used for fundamentals (Dhan has none) and that split check only. Fundamentals are a
  current snapshot; ROE and debt are often missing for Indian stocks and are shown as n/a, and the
  flags are prompts to look twice, not verdicts.
* Fix carried over from the upstream engine: Steady/God's Plan inverse-volatility weights were
  computed from the last 365 days up to *today*; they now use data up to the screen date.
* Not addressed (needs historical index membership and delisted-stock prices): survivorship bias.

## Tests

```
pip install pytest -r requirements.txt
python -m pytest tests
```

Offline: synthetic prices drive the real engine code, no Dhan or network needed.
