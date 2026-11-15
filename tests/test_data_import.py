"""External-repo import, corporate-action adjustment, inbox importers and the Data status page."""
import json
import subprocess
from datetime import date, datetime, timedelta, timezone

import pytest

from app.calendar import MarketCalendar
from app.config import Settings
from app.corporate_actions import AdjustedHistory, applied_factors, bonus_factor, split_factor, stated_factor
from app.history import bar_date, daily_start
from app.manual_data import Event, FiiDii, ManualData, events_to_csv, fii_dii_to_csv
from app.models import Bar
from app.store import Store
from tests.test_step1 import sim_settings

NOW = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)


def weekdays(a, n):
    out, d = [], a
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def bars_with_split(sym="X", n=400, at=300, factor=0.2, price=1000.0):
    """Flat prices that drop to price x factor on bar `at` (the ex-date)."""
    days = weekdays(date(2024, 1, 1), n)
    out = []
    for i, d in enumerate(days):
        p = price * (factor if i >= at else 1.0) * (1 + 0.001 * (i % 5))
        out.append(Bar(sym, daily_start(d), p, p * 1.01, p * 0.99, p, 1000.0))
    return out, days


def act(sym, ex, t, factor=None, purpose="", ratio=None, source="external_repo"):
    return {"symbol": sym, "ex_date": ex.isoformat(), "action_type": t, "purpose": purpose or t, "record_date": None,
            "announced_on": None, "ratio": ratio, "price_factor": factor, "factor_basis": "test", "source": source,
            "source_ref": "test", "imported_utc": NOW.isoformat()}


# ---------------- factors ----------------
def test_factors_from_the_stated_ratios():
    assert split_factor("10:2") == pytest.approx(0.2) and split_factor("2:1") == 0.5 and split_factor("1:2") is None
    assert bonus_factor("1:1") == 0.5 and bonus_factor("1:2") == pytest.approx(2 / 3) and bonus_factor("4:1") == 0.2
    assert bonus_factor("4:1", "Scheme Of Arrangement - Bonus Ncrps 4:1") is None       # preference shares: no equity change
    assert stated_factor("DEMERGER", "", "Demerger")[0] is None
    assert stated_factor("RIGHTS", "1:8", "Rights")[0] is None and stated_factor("BONUS", "x", "")[0] is None


def test_actions_apply_only_where_the_raw_prices_show_them_and_same_day_actions_together():
    bars, days = bars_with_split(factor=0.1)
    ex = days[300]
    # a bonus 4:1 (x0.2) and a split 2:1 (x0.5) on the same day: one 0.1 price change
    f = applied_factors(bars, [act("X", ex, "BONUS", 0.2), act("X", ex, "SPLIT", 0.5, purpose="split")])
    assert len(f) == 1 and f[0]["factor"] == pytest.approx(0.1) and "BONUS" in f[0]["name"] and "SPLIT" in f[0]["name"]
    # an action the prices do not show (the history is already adjusted): not applied, the reason kept
    flat, d2 = bars_with_split(factor=1.0)
    g = applied_factors(flat, [act("X", d2[300], "SPLIT", 0.5)])
    assert g[0]["factor"] is None and "not seen in the prices" in g[0]["basis"]
    # a demerger: the factor is the ex-date opening gap
    dem, d3 = bars_with_split(factor=0.8)
    h = applied_factors(dem, [act("X", d3[300], "DEMERGER")])
    assert h[0]["factor"] == pytest.approx(dem[300].open / dem[299].close) and "opening gap" in h[0]["basis"]


def test_ACCEPTANCE_adjusted_history_is_point_in_time_and_resolves_the_break():
    bars, days = bars_with_split(factor=0.2, at=300)
    adj = AdjustedHistory(bars, [act("X", days[300], "SPLIT", 0.2)], 15.0)
    assert 300 in adj.raw_breaks                                         # the split is a price break in the raw bars
    # before the ex-date: nothing is known, the raw bars are used unchanged
    w = adj.window(299, 1000)
    assert [b.close for b in w] == [b.close for b in bars[:300]]
    # on and after the ex-date: the earlier bars read in post-split terms and the break no longer cuts the history
    w = adj.window(350, 1000)
    assert len(w) == 351 and w[0].close == pytest.approx(bars[0].close * 0.2) and w[-1].close == bars[350].close
    assert w[0].volume == pytest.approx(bars[0].volume / 0.2)
    # raw prices are never changed
    assert bars[0].close == pytest.approx(1000.0)
    # without the action the raw break cuts the history to the bars after it
    raw = AdjustedHistory(bars, [], 15.0)
    assert len(raw.window(350, 1000)) == 51
    # an unexplained break still cuts it
    assert adj.last_unexplained_break(350) == 0 and raw.last_unexplained_break(350) == 300


def test_ACCEPTANCE_the_backtest_engine_reads_the_adjusted_window():
    from app.backtest import Engine
    from app.options.backtest import OptionEngine
    from app.options.option_history import OptionHistory
    from tests.test_phase3 import FixedRegime
    from tests.test_phase9_core import settings
    cfg = settings()
    bars, days = bars_with_split("RELIANCE", factor=0.2, at=300)
    store = Store(":memory:")
    store.save_history_bars("bars_1d", bars + [Bar("NIFTY", b.start, 100, 100, 100, 100, None) for b in bars]
                            + [Bar("INDIAVIX", b.start, 14, 14, 14, 14, None) for b in bars], "sim")
    base = Engine(cfg, store, "sim", MarketCalendar(), regime=FixedRegime("RANGE"))
    eng = OptionEngine(cfg, store, "sim", OptionHistory(store, "sim"), base, lot_sizes={"RELIANCE": 500})
    start = max(k for k in base.breaks("RELIANCE") if k <= 350)
    assert len(eng.daily_window("RELIANCE", 350, start)) == 51          # no action stored: raw after the break
    store.save_corporate_actions([act("RELIANCE", days[300], "SPLIT", 0.2)])
    eng2 = OptionEngine(cfg, store, "sim", OptionHistory(store, "sim"), base, lot_sizes={"RELIANCE": 500})
    w = eng2.daily_window("RELIANCE", 350, start)
    assert len(w) == 351 and w[0].close == pytest.approx(bars[0].close * 0.2)
    eng2.adjust_for_actions = False
    eng2._adjusted.clear()
    assert len(eng2.daily_window("RELIANCE", 350, start)) == 51


# ---------------- overlays: imported rows never replace yours ----------------
def manual_in(tmp_path, events="date,type,note,symbol\n", fii="date,fii_net_cr,dii_net_cr\n"):
    raw = sim_settings().model_dump()
    raw["calendar"] = {"events_csv": str(tmp_path / "events.csv"), "holidays_csv": str(tmp_path / "holidays.csv"),
                       "fii_dii_csv": str(tmp_path / "fii_dii.csv")}
    raw["data_status"] = {**raw["data_status"], "inbox_dir": str(tmp_path / "inbox")}
    s = Settings.model_validate(raw)
    (tmp_path / "events.csv").write_text(events, encoding="utf-8")
    (tmp_path / "holidays.csv").write_text("date,note\n", encoding="utf-8")
    (tmp_path / "fii_dii.csv").write_text(fii, encoding="utf-8")
    store = Store(":memory:")
    m = ManualData(s, store, MarketCalendar())
    return s, store, m


def test_ACCEPTANCE_overlay_rows_merge_on_load_and_your_rows_win(tmp_path):
    s, store, m = manual_in(tmp_path, events="date,type,note,symbol\n2026-08-05,RBI_POLICY,my note,\n",
                            fii="date,fii_net_cr,dii_net_cr\n2026-11-10,-100,50\n")
    ext = tmp_path / "external"
    ext.mkdir()
    (ext / "events_external.csv").write_text(events_to_csv([Event(date(2026, 8, 5), "RBI_POLICY", "imported", ""),
                                                            Event(date(2026, 6, 6), "RBI_POLICY", "imported", "")]))
    (ext / "fii_dii_external.csv").write_text(fii_dii_to_csv({date(2026, 11, 10): FiiDii(date(2026, 11, 10), 999, 1, "ext"),
                                                              date(2026, 11, 11): FiiDii(date(2026, 11, 11), 7, 1, "ext")}))
    m.load_all()
    ev = {(e["date"], e["note"]) for e in store.all_events()}
    assert (date(2026, 8, 5), "my note") in ev and (date(2026, 8, 5), "imported") not in ev and (date(2026, 6, 6), "imported") in ev
    assert {r["date"]: r["fii_net_cr"] for r in store.fii_dii_all()} == {date(2026, 11, 10): -100, date(2026, 11, 11): 7}
    # your file is not touched by the overlay
    assert "999" not in (tmp_path / "fii_dii.csv").read_text()
    # an invalid overlay is not used and is reported; your rows still load
    (ext / "fii_dii_external.csv").write_text("date,fii_net_cr\nnot-a-date,1\n")
    m.load_all()
    assert [r["date"] for r in store.fii_dii_all()] == [date(2026, 11, 10)]
    assert any("imported rows" in p for p in m.problems())


# ---------------- the one-time import from a repo clone ----------------
def make_repo(path, breeze_closes):
    import pyarrow as pa
    import pyarrow.parquet as pq
    (path / "data/CORPORATE_ACTIONS").mkdir(parents=True)
    (path / "data/FLOWS").mkdir(parents=True)
    (path / "data/MACRO").mkdir(parents=True)
    (path / "data/NSE").mkdir(parents=True)
    (path / "LICENSE").write_text("personal use only")
    (path / "data/CORPORATE_ACTIONS/corporate_actions.csv").write_text(
        "Ex_Date,Record_Date,Symbol,Company_Name,Series,Action_Type,Ratio_or_Amount,Purpose,Face_Value,ISIN,BC_Start_Date,BC_End_Date\n"
        "2025-06-16,2025-06-16,RELIANCE,R,EQ,SPLIT,10:2,Face Value Split,2,I,,\n"
        "2025-08-26,2025-08-26,RELIANCE,R,EQ,DIVIDEND,5,Dividend - Rs 5,2,I,,\n"
        "2025-08-25,2025-08-25,TVS,T,EQ,BONUS,4:1,Scheme Of Arrangement - Bonus Ncrps 4:1,1,I,,\n"
        "2018-01-01,,OLD,O,EQ,SPLIT,10:1,old,1,I,,\n"
        "2025-01-01,,RELIANCE,R,EQ,MEETING,,Agm,1,I,,\n")
    (path / "data/MACRO/rbi_policy_rates.csv").write_text(
        "Date,Repo,Reverse_Repo,SDF,MSF,Bank_Rate,CRR,SLR,Event\n2018-08-01,6.5,,,,,,,old\n2025-06-06,5.5,,,,,,,50 bps cut\n")
    (path / "data/FLOWS/fii_dii_cash.csv").write_text(
        "Date,FII_Buy_Cr,FII_Sell_Cr,FII_Net_Cr,DII_Buy_Cr,DII_Sell_Cr,DII_Net_Cr,Total_Net_Cr\n"
        "2026-11-10,1,2,-1.5,3,1,2.0,0.5\n2026-11-11,1,2,-3.0,3,1,4.0,1.0\n")
    (path / "data/FLOWS/fii_derivatives.csv").write_text("Date,FII_Long_Ratio_Pct\n2023-12-29,50\n2024-01-01,68.84\n")
    days = sorted(breeze_closes)
    def table(closes):
        return pa.table({"Date": pa.array([datetime(d.year, d.month, d.day) for d in days], pa.timestamp("ns")),
                         "Open": closes, "High": closes, "Low": closes, "Close": closes,
                         "Adj Close": closes, "Volume": [1] * len(days)})
    # RELIANCE: the source's closes before the split are adjusted (x0.2); one day differs by 3% (a real mismatch)
    rel = [breeze_closes[d] * (0.2 if d < date(2025, 6, 16) else 1.0) for d in days]
    rel[-1] *= 1.03
    pq.write_table(table(rel), path / "data/NSE/RELIANCE_NS.parquet")
    other = [100.0] * len(days)
    other[3] = None
    pq.write_table(table(other), path / "data/NSE/ABB_NS.parquet")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "data"], cwd=path, check=True)


def test_ACCEPTANCE_one_time_import_tags_rows_keeps_breeze_first_and_cross_checks(tmp_path):
    from app.external_data import PRICE_BASIS, run_import
    s, store, m = manual_in(tmp_path, fii="date,fii_net_cr,dii_net_cr\n2026-11-10,-100,50\n")
    days = weekdays(date(2025, 6, 2), 30)
    closes = {d: (1000.0 if d < date(2025, 6, 16) else 200.0) for d in days}
    store.save_history_bars("bars_1d", [Bar("RELIANCE", daily_start(d), c, c, c, c, 1) for d, c in closes.items()], "breeze")
    repo = tmp_path / "repo"
    repo.mkdir()
    make_repo(repo, closes)
    raw = s.model_dump()
    raw["external_data"] = {**raw["external_data"], "repo_path": str(repo), "history_since": date(2025, 1, 1)}
    s = Settings.model_validate(raw)
    rep = run_import(s, store, repo, NOW, fno=["RELIANCE", "ABB", "ICICILIFE"], priority=["RELIANCE"])
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert rep["commit"] == head
    # 1. corporate actions since 2019, tagged; preference-share bonus kept for the record without a price factor
    ca = store.corporate_actions()
    assert {(a["symbol"], a["action_type"]) for a in ca} == {("RELIANCE", "SPLIT"), ("RELIANCE", "DIVIDEND"), ("TVS", "BONUS")}
    assert all(a["source"] == "external_repo" and head in a["source_ref"] for a in ca)
    assert next(a for a in ca if a["symbol"] == "TVS")["price_factor"] is None
    assert next(a for a in ca if a["action_type"] == "SPLIT")["price_factor"] == pytest.approx(0.2)
    # 2. RBI dates since 2019 in the gitignored overlay, tagged
    ov = (tmp_path / "external" / "events_external.csv").read_text()
    assert "2025-06-06,RBI_POLICY" in ov and "2018-08-01" not in ov and "source=external_repo" in ov and head[:12] in ov
    # 3. FII/DII: your day wins
    m.load_all()
    assert {r["date"]: r["fii_net_cr"] for r in store.fii_dii_all()} == {date(2026, 11, 10): -100, date(2026, 11, 11): -3.0}
    assert rep["fii_dii"]["days_you_already_had"] == 1
    # 4. positioning from 2024 only
    assert store.positioning_dates() == ["2024-01-01"]
    # 5. bars only for F&O stocks without Breeze bars; a missing price is left out, never filled
    assert set(store.external_bar_summary()) == {"ABB"} and store.external_bar_summary()["ABB"][2] == 29
    assert store.external_bars("ABB")[0]["price_basis"] == PRICE_BASIS and rep["external_bars_1d"]["not_in_repo"] == ["ICICILIFE"]
    # 6. cross-check: the pre-split days are explained by the split; the 3% day is a real mismatch
    cc = {r["date"]: r for r in store.crosscheck_rows()}
    last = max(days).isoformat()
    assert cc[last]["explained_by"] is None and cc[last]["diff_pct"] == pytest.approx(3.0, abs=0.01)
    assert all(r["explained_by"].startswith("SPLIT") for d, r in cc.items() if d < "2025-06-16")
    assert rep["crosscheck"]["stocks"]["RELIANCE"]["unexplained"] == 1
    # every dataset logged with the commit
    assert {"corporate_actions", "rbi_policy", "fii_dii", "positioning", "external_bars_1d", "crosscheck"} <= set(store.last_imports())


# ---------------- inbox ----------------
BOARD = ('"SYMBOL","COMPANY NAME","PURPOSE","DETAILS","MEETING DATE"\n'
         '"RELIANCE","Reliance","Financial Results","To consider results","20-Jan-2027"\n'
         '"TCS","Tata","Fund Raising","","25-Jan-2027"\n')
ACTIONS = ('"SYMBOL","COMPANY NAME","SERIES","PURPOSE","FACE VALUE","EX-DATE","RECORD DATE"\n'
           '"INFY","Infosys","EQ","Bonus 1:1","5","10-Dec-2026","10-Dec-2026"\n'
           '"TCS","Tata","EQ","Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share","2","12-Dec-2026","-"\n'
           '"X","X","EQ","Annual General Meeting","1","12-Dec-2026","-"\n')


def full_board(n=150, start=date(2026, 11, 16), days=60, extra=""):
    """An all-companies board-meetings export: n companies, meetings spread over `days` days from `start`."""
    rows = [f'"CO{i:04d}","Company {i}","Fund Raising","","{(start + timedelta(days=i % days)):%d-%b-%Y}"' for i in range(n)]
    return BOARD + "\n".join(rows) + "\n" + extra


FII = "Category,Date,Buy Value,Sell Value,Net Value\nFII/FPI,13-Nov-2026,10,20,-10\nDII,13-Nov-2026,20,10,10\nFII/FPI,10-Nov-2026,1,1,999\n"


def test_ACCEPTANCE_inbox_imports_each_kind_moves_files_and_never_overwrites_your_rows(tmp_path):
    from app.inbox import run_inbox
    s, store, m = manual_in(tmp_path, fii="date,fii_net_cr,dii_net_cr\n2026-11-10,-100,50\n")
    m.load_all()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "board.csv").write_text(BOARD)
    (inbox / "actions.csv").write_text(ACTIONS)
    (inbox / "fii.csv").write_text(FII)
    (inbox / "rbi.csv").write_text("date,decision\n2026-12-05,scheduled\n2026-10-01,repo unchanged at 5.25%\n")
    (inbox / "junk.csv").write_text("a,b\n1,2\n")
    from app.inbox import BoardMeetingsDeclaration
    decl = BoardMeetingsDeclaration(date(2026, 11, 15), date(2027, 1, 31), all_companies=True)
    res = {r["file"]: r for r in run_inbox(s, store, m, NOW, board_meetings=decl)}
    # review fix 2026-11-15: a two-company file cannot be the all-companies list, whatever you declare
    assert res["board.csv"]["ok"] is False and "only 2 distinct companies" in res["board.csv"]["errors"][0]
    assert res["junk.csv"]["ok"] is False and "not recognised" in res["junk.csv"]["errors"][0]
    rej = list((inbox / "rejected").iterdir())
    assert any(p.name.endswith("junk.csv") for p in rej) and any(p.name.endswith(".reason.txt") for p in rej)
    assert len(list((inbox / "done").iterdir())) == 3 and not [p for p in inbox.iterdir() if p.is_file()]
    ev = store.all_events()
    assert not [e for e in ev if e["type"] in ("COVERAGE", "RESULTS")]          # the rejected file gave nothing
    # a declared, unfiltered all-companies export: RESULTS for the results meeting only; COVERAGE for every watchlist
    # stock through the DECLARED end date (2027-01-31), not the latest meeting date in the file (2027-01-25)
    (inbox / "board_all.csv").write_text(full_board(days=70))
    got = run_inbox(s, store, m, NOW, board_meetings=decl)[0]
    assert got["kind"] == "board_meetings" and got["covered_through"] == "2027-01-31" and got["distinct_companies"] == 152
    ev = store.all_events()
    res_ev = [e for e in ev if e["type"] == "RESULTS"]
    assert [(e["symbol"], e["date"]) for e in res_ev] == [("RELIANCE", date(2027, 1, 20))]
    cov = [e for e in ev if e["type"] == "COVERAGE"]
    stocks = [i.symbol for i in s.watchlist if i.kind == "stock"]
    assert {e["symbol"] for e in cov} == set(stocks) and all("covered_through=2027-01-31" in e["note"] for e in cov)
    assert all("covered_from=2026-11-15" in e["note"] and "all companies, confirmed by you" in e["note"] for e in cov)
    # corporate actions: bonus and split with factors, the meeting ignored, tagged as an inbox file
    ca = {a["symbol"]: a for a in store.corporate_actions()}
    assert set(ca) == {"INFY", "TCS"} and ca["INFY"]["price_factor"] == 0.5 and ca["TCS"]["price_factor"] == pytest.approx(0.2)
    assert ca["TCS"]["source"] == "nse_inbox" and ca["TCS"]["source_ref"] == "actions.csv"
    # FII/DII: the new day added, your 10-Nov row kept
    assert {r["date"]: r["fii_net_cr"] for r in store.fii_dii_all()} == {date(2026, 11, 10): -100, date(2026, 11, 13): -10}
    # RBI: a scheduled date and a decision
    rbi = [e["note"] for e in ev if e["type"] == "RBI_POLICY"]
    assert any(n.startswith("SCHEDULED") for n in rbi) and any("repo unchanged" in n for n in rbi)
    # the same files again: everything is a duplicate
    (inbox / "board2.csv").write_text(full_board(days=70))
    again = run_inbox(s, store, m, NOW, board_meetings=decl)[0]
    assert again["added"] == 0 and again["duplicates"] > 0
    # the comment lines of events.csv survive
    assert (tmp_path / "events.csv").read_text().count("COVERAGE") == len(stocks)


# ---------------- the Data status page ----------------
def test_ACCEPTANCE_data_status_never_shows_missing_or_stale_as_ok(tmp_path):
    from app.data_status import data_status
    s, store, m = manual_in(tmp_path)
    m.load_all()
    cal = MarketCalendar()
    st = data_status(s, store, cal, "sim", manual=m, now=NOW)
    rows = {r["source"]: r for r in st["rows"]}
    assert rows["Breeze session"]["colour"] == "red" and "apisession" in rows["Breeze session"]["what_to_do"]
    assert rows["FII/DII cash flows"]["status"] == "MISSING"
    assert rows["Results dates and event coverage"]["status"] == "MISSING"
    assert rows["Corporate actions"]["status"] == "MISSING" and rows["RBI policy dates"]["status"] == "MISSING"
    assert all(r["what_to_do"] != "-" for r in st["rows"] if r["status"] in ("MISSING", "STALE", "PARTIAL"))
    assert all(r["blocks"] for r in st["rows"])
    assert st["counts"]["green"] == 0 and st["how_to_update"]["daily"]
    # fresh FII/DII: OK; two trading days old: STALE
    (tmp_path / "fii_dii.csv").write_text("date,fii_net_cr,dii_net_cr\n2026-11-13,1,1\n")
    m.load_all()
    assert {r["source"]: r for r in data_status(s, store, cal, "sim", manual=m, now=NOW)["rows"]}["FII/DII cash flows"]["status"] == "OK"
    (tmp_path / "fii_dii.csv").write_text("date,fii_net_cr,dii_net_cr\n2026-11-11,1,1\n")
    m.load_all()
    assert {r["source"]: r for r in data_status(s, store, cal, "sim", manual=m, now=NOW)["rows"]}["FII/DII cash flows"]["status"] == "STALE"
    # RBI: a scheduled meeting passed without a decision is amber
    (tmp_path / "events.csv").write_text("date,type,note,symbol\n2026-11-01,RBI_POLICY,SCHEDULED MPC decision,\n"
                                        "2026-08-05,RBI_POLICY,RBI decision: hold,\n")
    m.load_all()
    r = {x["source"]: x for x in data_status(s, store, cal, "sim", manual=m, now=NOW)["rows"]}["RBI policy dates"]
    assert r["status"] == "STALE" and "2026-11-01" in r["coverage"]


def test_times_are_shown_in_ist_and_the_days_actual_budget_is_used(tmp_path):
    from app.data_status import data_status, fmt_ist
    from app.options.option_history import budgeted_fetcher, todays_budget
    assert fmt_ist("2026-11-15T05:00:17.848403+00:00") == "15 Nov 2026, 10:30 AM"
    assert fmt_ist("2026-11-15T09:05:00+00:00") == "15 Nov 2026, 2:35 PM"
    assert fmt_ist("2026-11-14T18:30:00+00:00") == "15 Nov 2026, 12:00 AM"       # midnight IST
    assert fmt_ist("2026-11-12") == "12 Nov 2026" and fmt_ist(None) is None and fmt_ist("live prices today") == "live prices today"
    s, store, m = manual_in(tmp_path)
    m.load_all()
    day = NOW.astimezone(__import__("app.quality", fromlist=["IST"]).IST).date()
    default = s.options.backtest.daily_request_budget
    assert todays_budget(s, store, day) == default
    row = lambda: {r["source"]: r for r in data_status(s, store, MarketCalendar(), "sim", manual=m, now=NOW)["rows"]}[
        "Breeze option history (stock options)"]
    assert f"of {default:,} requests used today" in row()["coverage"]
    store.set_day_budget(day, 4500)
    store.add_requests(day, 3500)
    assert todays_budget(s, store, day) == 4500 and store.day_budget(day + timedelta(days=1)) is None
    r = row()
    assert "3,500 of 4,500 (set for today only; default 3,500) requests used today" in r["coverage"]
    assert r["detail"]["budget"] == 4500 and store.requests_used(day) == 3500       # the budget is not counted as requests
    assert budgeted_fetcher(s, store, object(), now=NOW).max_requests == 1000        # the raised budget is what a run may use
    st = data_status(s, store, MarketCalendar(), "sim", manual=m, now=NOW)
    assert st["as_of_ist"] == "15 Nov 2026, 1:30 PM" and st["last_session_ist"] == "13 Nov 2026"


def test_data_status_routes_and_import_routes_follow_the_write_checks():
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.test_step1 import FakeFeed
    app = create_app(sim_settings(), feed=FakeFeed(), db_path=":memory:", background=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        j = c.get("/api/data-status").json()
        assert set(j["counts"]) == {"green", "amber", "red"} and j["rows"]
        assert "Data status" in c.get("/data-status").text and 'id="dstrip"' in c.get("/").text
        assert c.post("/data/import/rbi-decision", content="date=x").status_code == 415           # not JSON: refused
        bad = c.post("/data/import/rbi-decision", json={"date": "2026-12-05", "decision": ""})
        assert bad.status_code == 400 and "empty" in bad.json()["errors"][0]
    with TestClient(app, base_url="http://evil.example:8000") as c:
        assert c.post("/data/import/inbox", json={}).status_code == 403
