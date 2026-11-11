# After each phase

Two parts: what Claude does before saying a phase is finished, and what you do before starting the next one.

## What Claude does

1. Run `python -m pytest -q` and paste the result. Fix failures; do not skip or delete tests to get a pass.
2. Start the app in simulated mode and confirm the new screen or route works.
3. Update `README.md` with anything the user must do to run or see the new feature.
4. Update `docs/STATUS.md`: the summary row, a section for the phase (built, tested, not tested, known gaps),
   new open issues, and any decisions made.
5. Update `docs/architecture.md` only if a contract, table, route or config section changed.
6. Report to the user in plain language:
   - what was built
   - how to see it working (exact command and page)
   - what is tested and what is not
   - anything that needs the user's input or a live broker run
7. Stop. Do not begin the next phase.

## What you do

### 1. Closing instruction (paste into the same Claude Code session)

```
The phase is built. Follow docs/AFTER_EACH_PHASE.md, section "What Claude does", in order.
Show me the pytest output. Do not start the next phase.
```

### 2. See it yourself

- Run `python -m pytest -q`. It must pass.
- Run `python -m app.main`, open http://127.0.0.1:8000 and check the new screen.
- Open `Trading_System_Build_Tracker.xlsx` and go through the "Done when" column for the phase.

### 3. Independent review (paste into Cursor with an OpenAI model, with the project open)

```
You are reviewing one phase of a trading research system. You did not write it. Be skeptical.
Read CLAUDE.md, docs/architecture.md and the phase section in docs/implementation.md, then review the
files changed in this phase (use git diff against the previous commit).

Look for, in this order:
1. Any code path, route or SDK call that could place, modify or cancel an order.
2. Look-ahead bias: a backtest or signal using data that was not available at that time.
3. Missing or wrong costs, and wrong position-size, payoff or margin arithmetic. Recompute one example by hand.
4. Stale, missing or quarantined data being treated as valid, or unknown values turned into zero.
5. Hardcoded rupee amounts, lot sizes or expiry dates that should come from config or the instrument list.
6. An automatic rule that can increase risk without the user's approval.
7. Tests that pass without really checking anything, and acceptance checks in implementation.md with no test.
8. Places where the code disagrees with docs/architecture.md.

For each finding give: severity (HIGH, MEDIUM, LOW), file and line, what is wrong, and how to confirm it.
Do not rewrite the code. End with a list of acceptance checks you could not verify.
```

### 4. Fix what the review found (paste into Claude Code)

```
An independent review of this phase found the issues below. For each one: confirm whether it is real,
fix it if it is, add a test that would have caught it, and tell me if you disagree and why.
Then rerun python -m pytest -q and update docs/STATUS.md.

<paste the review findings here>
```

### 5. Commit

```
git add -A
git commit -m "Phase N: <name> - built, reviewed, tests passing"
```

Run `git init` once before phase 2 and commit the phase 1 code first.

### 6. Update the tracker

Mark tasks Done only for checks you saw work. Leave anything needing a live broker run as "In progress".

### 7. Start the next phase (new Claude Code session)

```
Read CLAUDE.md, docs/architecture.md, docs/implementation.md and docs/STATUS.md.
Then build Phase N from docs/implementation.md. Build only that phase.
```

## Extra checks at specific points

| When | Check |
|---|---|
| Before phase 2 | Phase 1 runs with live Breeze data; stock codes verified |
| Before phase 3 backtests | Brokerage and tax rates filled in `config.yaml` from your own contract note |
| After phase 3 | Compare one backtest trade's costs with a real contract note |
| After phase 4 | Check one spread's max loss and margin by hand against ICICI's margin calculator |
| After phase 5 | Change `capital_inr` and confirm every limit and quantity rescales |
| Before phase 6 | Telegram bot token in `.env` |
| After phase 6 | Leave it running for a full market day in paper mode and read every card |
| After phase 8 | 8-12 weeks of paper trading before any strategy goes live |

## If something goes wrong

- **Tests fail and Claude cannot fix them:** stop, run `git stash` or `git checkout .` to return to the last commit, and start the phase again in a new session.
- **Claude starts changing earlier phases:** tell it to stop and limit changes to the current phase unless a bug requires it.
- **Claude wants to remove or weaken a safety test:** refuse. That is a rule in `CLAUDE.md`.
- **A session gets long and confused:** end it. `docs/STATUS.md` carries the state into a fresh session.
