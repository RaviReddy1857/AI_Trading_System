"""Fixes from the Codex review of the phase 3 backtest engine (2026-11-13), before phases 4 and 9 reuse it.

1. Entry validation uses the final slipped fill, not only the raw open.
2. Incomplete intraday sessions are not traded; a trade never gets an EOD exit from a truncated session.
3. ORB range_minutes must be a whole number of 5-minute bars, checked at config load.
"""
from collections import Counter
from datetime import time
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.backtest import build_report, required_bar_starts, session_complete
from app.config import load_settings as load_real_settings
from tests.test_phase3 import (DAY, FULL_SESSION_BARS, Stub, engine_with, five, full_day, ist, settings)

TCS = [("TCS", "stock")]


# ---------------- 1. fill beyond stop/target ----------------
@pytest.mark.parametrize("direction, stop, target", [
    ("LONG", 990.0, 1000.40),      # open 1000 -> buy fill 1000.50 (0.05% slippage): past the target
    ("LONG", 990.0, 1000.50),      # fill exactly at the target
    ("SHORT", 1010.0, 999.60),     # open 1000 -> sell fill 999.50: past the target
    ("SHORT", 1010.0, 999.50),     # fill exactly at the target
])
def test_slipped_fill_at_or_beyond_target_is_not_a_trade(direction, stop, target):
    """The raw open (1000) is inside stop..target, but the slipped fill is not. Before the fix this
    opened a trade that then 'hit the target' on its entry bar at a loss."""
    bars = full_day("TCS", [(999.8, 1000.2, 999.7, 999.9), (999.9, 1000.1, 999.8, 999.9), (1000, 1000.1, 999.9, 1000)])
    eng, _ = engine_with(bars)
    trades, skipped = eng.run(Stub(ist(9, 25), stop=stop, target=target, direction=direction), [DAY], TCS)
    assert trades == []
    assert skipped["FILL_BEYOND_STOP_OR_TARGET"] == 1 and skipped["GAPPED_PAST_STOP_OR_TARGET"] == 0


def test_fill_inside_the_levels_still_trades():
    bars = full_day("TCS", [(999.8, 1000.2, 999.7, 999.9), (999.9, 1000.1, 999.8, 999.9), (1000, 1000.1, 999.9, 1000)])
    eng, _ = engine_with(bars)
    trades, skipped = eng.run(Stub(ist(9, 25), stop=990.0, target=1000.60), [DAY], TCS)
    assert len(trades) == 1 and trades[0].entry_price == 1000.50 and not skipped


def test_raw_open_beyond_the_target_is_still_a_gap_skip():
    bars = full_day("TCS", [(999.8, 1000.2, 999.7, 999.9), (999.9, 1000.1, 999.8, 999.9), (1002, 1003, 1001, 1002)])
    eng, _ = engine_with(bars)
    trades, skipped = eng.run(Stub(ist(9, 25), stop=990.0, target=1001.0), [DAY], TCS)
    assert trades == [] and skipped["GAPPED_PAST_STOP_OR_TARGET"] == 1


def test_report_lists_fill_skips_and_keeps_data_exclusions_separate():
    s = settings()
    from app.strategies import build_strategies
    from app.costs import CostModel
    strat = build_strategies(s, ["orb"])[0]
    skipped = Counter({"FILL_BEYOND_STOP_OR_TARGET": 2, "SESSIONS_INCOMPLETE": 5, "DROPPED_MISSING_BAR_DURING_TRADE": 1})
    rep = build_report(strat, s, CostModel(s.costs), "sim", [DAY], [], [], skipped, None, None, 1)
    assert rep["skipped_signals"] == {"FILL_BEYOND_STOP_OR_TARGET": 2}
    x = rep["data_exclusions"]
    assert (x["sessions_incomplete"], x["sessions_without_bars"], x["trades_dropped_missing_bar"],
            x["trades_dropped_session_ended"]) == (5, 0, 1, 0)
    assert "15:15" in x["rule"]


# ---------------- 2. incomplete intraday sessions ----------------
def test_required_bars_run_from_0915_to_the_exit_bar():
    req = required_bar_starts(time(15, 15))
    assert len(req) == FULL_SESSION_BARS == 72
    assert req[0] == time(9, 15) and req[-1] == time(15, 10)        # 15:15 and 15:20 not required
    assert required_bar_starts(time(14, 0))[-1] == time(13, 55)


def test_truncated_session_gives_no_trade_and_no_eod_exit():
    """Data ends at 13:00 with a position open. Before the fix the trade was closed 'EOD' at the
    13:00 bar's close."""
    flat = [(1000, 1001, 999, 1000)] * 45                                   # 09:15 .. 12:55
    eng, _ = engine_with(five("TCS", flat))
    trades, skipped = eng.run(Stub(ist(9, 20), stop=950.0, target=1100.0), [DAY], TCS)
    assert trades == [] and skipped["SESSIONS_INCOMPLETE"] == 1


def test_session_missing_a_midday_bar_is_not_traded():
    bars = [b for b in full_day("TCS", [(1000, 1001, 999, 1000)]) if b.start != ist(11, 0)]
    eng, _ = engine_with(bars)
    trades, skipped = eng.run(Stub(ist(9, 20), stop=950.0, target=1100.0), [DAY], TCS)
    assert trades == [] and skipped["SESSIONS_INCOMPLETE"] == 1


def test_session_missing_its_exit_bar_is_not_traded():
    """The 15:10 bar (ending 15:15) is missing but 15:15 exists. Before the fix the trade left 'EOD' at
    the 15:15 bar's open."""
    flat = five("TCS", [(1000, 1001, 999, 1000)] * 75)                     # 09:15 .. 15:25
    bars = [b for b in flat if b.start != ist(15, 10)]
    eng, _ = engine_with(bars)
    trades, skipped = eng.run(Stub(ist(9, 20), stop=950.0, target=1100.0), [DAY], TCS)
    assert trades == [] and skipped["SESSIONS_INCOMPLETE"] == 1


def test_session_without_the_1515_and_1520_bars_is_usable():
    bars = full_day("TCS", [(1000, 1001, 999, 1000)])                      # exactly 09:15 .. 15:10
    assert len(bars) == 72 and session_complete(bars, required_bar_starts(time(15, 15)))
    eng, _ = engine_with(bars)
    trades, skipped = eng.run(Stub(ist(9, 20), stop=950.0, target=1100.0), [DAY], TCS)
    assert len(trades) == 1 and trades[0].exit_reason == "EOD"
    assert trades[0].exit_time == ist(15, 15).isoformat() and not skipped


def test_symbol_without_bars_that_session_is_counted():
    eng, _ = engine_with(full_day("INFY", [(1000, 1001, 999, 1000)]))
    trades, skipped = eng.run(Stub(ist(9, 20), stop=950.0, target=1100.0), [DAY], TCS)
    assert trades == [] and skipped["SESSIONS_NO_BARS"] == 1


def test_guard_trade_crossing_a_missing_bar_is_dropped(monkeypatch):
    """Even if a gappy session got past the completeness check, a trade whose holding period crosses a
    missing bar is dropped (the missing bar may have hit the stop)."""
    bars = [b for b in full_day("TCS", [(1000, 1001, 999, 1000)]) if b.start != ist(11, 0)]
    eng, _ = engine_with(bars)
    monkeypatch.setattr(eng, "session_usable", lambda bars: True)
    trades, skipped = eng.run(Stub(ist(9, 20), stop=950.0, target=1100.0), [DAY], TCS)
    assert trades == [] and skipped["DROPPED_MISSING_BAR_DURING_TRADE"] == 1


def test_guard_truncated_session_never_produces_an_eod_exit(monkeypatch):
    eng, _ = engine_with(five("TCS", [(1000, 1001, 999, 1000)] * 45))     # ends 13:00
    monkeypatch.setattr(eng, "session_usable", lambda bars: True)
    trades, skipped = eng.run(Stub(ist(9, 20), stop=950.0, target=1100.0), [DAY], TCS)
    assert trades == [] and skipped["DROPPED_SESSION_ENDED_BEFORE_EXIT"] == 1


def test_guard_missing_exit_bar_is_dropped_not_exited_at_a_later_price(monkeypatch):
    flat = five("TCS", [(1000, 1001, 999, 1000)] * 75)
    eng, _ = engine_with([b for b in flat if b.start != ist(15, 10)])
    monkeypatch.setattr(eng, "session_usable", lambda bars: True)
    trades, skipped = eng.run(Stub(ist(9, 20), stop=950.0, target=1100.0), [DAY], TCS)
    assert trades == [] and skipped["DROPPED_MISSING_BAR_DURING_TRADE"] == 1


# ---------------- 3. ORB range_minutes ----------------
@pytest.mark.parametrize("sid", ["orb", "orb_v2"])
@pytest.mark.parametrize("minutes", [17, 22, 33])
def test_orb_range_minutes_must_be_a_multiple_of_5_at_config_load(tmp_path, sid, minutes):
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
    raw["strategies"][sid]["range_minutes"] = minutes
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValidationError, match="multiple of 5"):
        load_real_settings(p)


@pytest.mark.parametrize("sid, minutes", [("orb", 5), ("orb", 60), ("orb_v2", 45), ("orb_v2", 90)])
def test_orb_range_minutes_multiples_of_5_load(tmp_path, sid, minutes):
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
    raw["strategies"][sid]["range_minutes"] = minutes
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert getattr(load_real_settings(p).strategies, sid).range_minutes == minutes


def test_backtests_page_shows_data_exclusions():
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_step1 import FakeFeed
    app = create_app(settings(), feed=FakeFeed(), db_path=":memory:", background=False)
    with TestClient(app) as c:
        page = c.get("/backtests")
        assert page.status_code == 200 and "Data exclusions" in page.text and "data_exclusions" in page.text
