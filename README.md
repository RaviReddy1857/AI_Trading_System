# Trading Research System - Step 1: data feed and quality gate

Research and suggestions only. **This app cannot place, change or close orders**,
and a test fails the build if such code is ever added. You trade manually in ICICI Direct.

## What step 1 gives you
- Live price table at http://127.0.0.1:8000 (local machine only)
- Two feed modes: `sim` (fake prices, no login needed) and `breeze` (live ICICI data, read-only)
- Quality gate: bad prices, crossed bid/ask, sudden jumps and stale data are quarantined
- 1-minute bars saved to `data/market.db` (SQLite) for the strategies in step 3
- `config.yaml`: capital and every risk limit as a % of capital

## Run it (Windows, Python 3.11+)
```
cd trading_system
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q          # should say 18 passed
python -m app.main           # then open http://127.0.0.1:8000
```
It starts in `sim` mode, so you will see moving fake prices straight away.

## Switch to live ICICI data
1. Create an app at https://api.icicidirect.com/apiuser/home to get an API key and secret.
2. Copy `.env.example` to `.env` and fill in the key and secret.
3. Each morning, log in at `https://api.icicidirect.com/apiuser/login?api_key=YOUR_KEY`,
   copy the session token from the address bar into `.env` (`BREEZE_SESSION_TOKEN`).
4. In `config.yaml` set `feed.mode: breeze`, then run `python -m app.main`.

Outside 09:15-15:30 IST on weekdays, symbols show `CLOSED`.

## Watchlist codes
ICICI uses its own short codes (`RELIND` for Reliance, `CNXBAN` for Bank Nifty).
The codes in `config.yaml` are from memory and **not yet verified against your account**.
Check each one once: `python scripts/lookup_code.py RELIANCE`. Unknown codes are skipped
with an error in the console.

## Status values
| Status | Meaning |
|---|---|
| LIVE | Fresh, passed all checks; usable by strategies |
| STALE | No good tick for `stale_after_seconds`; strategies must say NO TRADE |
| NO_DATA | Nothing received yet |
| CLOSED | Outside market hours |

## Files
- `app/feeds/breeze_feed.py` - the only file that touches the broker SDK (data calls only)
- `app/quality.py` - quality rules
- `app/hub.py` - tick -> quality gate -> bars -> price table
- `app/main.py` - web server and API (`/health`, `/api/prices`, `/api/quality`, `/api/bars/{symbol}`, `/api/config`)
- `tests/test_safety.py` - fails if order code, order routes or a public host appear

## Not yet tested
The Breeze live connection was written from the SDK source (breeze-connect 1.0.69) but
could not be run without your login. Expect small fixes on first live run.
