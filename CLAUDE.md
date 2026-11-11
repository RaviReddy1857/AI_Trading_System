# Project: AI Trading Research and Signal System

Single-user, local-first system for Indian markets (NSE). It produces trade signals with reasons,
sizing and estimates. The user places every order manually in ICICI Direct.

## Read these before doing anything
1. `docs/architecture.md` - how the system is put together and the contracts between parts
2. `docs/implementation.md` - the phase you are asked to build, its tasks and acceptance checks
3. `docs/STATUS.md` - what is already built, tested and still open

Build only the phase the user names. Do not start the next phase.

## Non-negotiable rules
- Research and suggestions only. Never add code, routes or SDK calls that place, modify, cancel or square off orders. tests/test_safety.py must keep passing.
- The broker SDK is imported only in app/feeds/breeze_feed.py, using data functions only.
- Every rupee limit comes from config.yaml as a percentage of capital. No hardcoded rupee amounts.
- Strategies only use prices marked usable (status LIVE) by the quality gate. Missing or stale data means NO TRADE.
- Numbers on a signal are computed by Python code, never by an LLM.
- Every strategy has an ID and a version, and is deterministic: same input gives same output.
- All backtests include brokerage, taxes and slippage, and enter at the next bar after the signal, never the same bar.
- Add pytest tests for everything new; run `python -m pytest -q` and fix failures before finishing.
- Finish by updating README.md and listing what was built, what was tested and what is still untested.

## How to work
- Work in small steps; run `python -m pytest -q` often.
- Reuse existing modules (`app/quality.py`, `app/hub.py`, `app/store.py`, `app/config.py`); do not duplicate them.
- New thresholds go in `config.yaml` with a validated field in `app/config.py`.
- Never read, print or log values from `.env`. Never put credentials in code, tests, prompts or docs.
- If a requirement conflicts with a rule above, stop and ask instead of working around the rule.
- If something cannot be tested here (for example the live broker connection), say so plainly in `docs/STATUS.md`.

## When the phase is finished
Follow `docs/AFTER_EACH_PHASE.md`, section "What Claude does".
