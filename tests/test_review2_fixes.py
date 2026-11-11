"""Regression tests for the second independent review (findings 1-3).
Each test fails on the code before the fix and passes after it."""
from datetime import date, timedelta

import pytest

from app.calendar import MarketCalendar
from app.config import Settings, load_settings
from app.feeds.breeze_feed import tick_from_breeze
from app.hub import MarketHub
from app.manual_data import ManualData
from app.models import Tick
from app.quality import BAD_PREV_CLOSE, BAD_PRICE, BAD_VOLUME, check_tick
from app.regime import RegimeService
from app.store import Store
from tests.phase2_fixtures import SESSION, T0, live_ticks, settings_with_tmp_data, store_history

INF, NAN = float("inf"), float("nan")


# ---------------- 1. config rejects non-finite numbers; capital has a ceiling ----------------
@pytest.mark.parametrize("section, field", [
    ("account", "capital_inr"), ("risk", "max_loss_per_trade_pct"), ("risk", "uncommitted_cash_pct"),
    ("feed", "max_jump_pct"), ("regime", "volatile_range_atr_mult"), ("regime", "vix_percentile"),
    ("regime", "breadth_up_pct"),
])
@pytest.mark.parametrize("bad", [INF, -INF, NAN])
def test_config_rejects_non_finite(section, field, bad):
    raw = load_settings().model_dump()
    raw[section][field] = bad
    with pytest.raises(ValueError):
        Settings.model_validate(raw)


def test_capital_upper_bound():
    raw = load_settings().model_dump()
    raw["account"]["capital_inr"] = 1_000_000_000          # Rs 100 crore: allowed
    Settings.model_validate(raw)
    raw["account"]["capital_inr"] = 1_000_000_001
    with pytest.raises(ValueError):
        Settings.model_validate(raw)


def test_yaml_inf_is_rejected(tmp_path):
    text = (load_settings.__globals__["ROOT"] / "config.yaml").read_text(encoding="utf-8")
    bad = tmp_path / "config.yaml"
    bad.write_text(text.replace("max_jump_pct: 5.0", "max_jump_pct: .inf"), encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(bad)


# ---------------- 2. a missing file keeps the last good copy ----------------
GOOD = {"holidays.csv": "date,note\n2026-11-11,test holiday\n",
        "events.csv": "date,type,note\n2026-11-11,BUDGET,Union Budget\n",
        "fii_dii.csv": "date,fii_net_cr,dii_net_cr\n2026-11-10,-5,1\n"}


def load(tmp_path, store):
    s = settings_with_tmp_data(tmp_path, create_files=False)
    cal = MarketCalendar()
    m = ManualData(s, store, cal)
    m.load_all()
    return s, m, cal


@pytest.mark.parametrize("fname", list(GOOD))
@pytest.mark.parametrize("damage", ["delete", "truncate"])
def test_missing_or_emptied_file_keeps_last_good_copy(tmp_path, fname, damage):
    store = Store(":memory:")                              # same DB across "restarts"
    for name, text in GOOD.items():
        (tmp_path / name).write_text(text)
    load(tmp_path, store)
    if damage == "delete":
        (tmp_path / fname).unlink()
    else:
        (tmp_path / fname).write_text("")
    s, m, cal = load(tmp_path, store)
    assert m.errors[fname.removesuffix(".csv")]
    assert not cal.is_trading_day(date(2026, 11, 11))      # holiday still known
    assert [e["type"] for e in store.events_on(date(2026, 11, 11))] == ["BUDGET"]
    assert [r["fii_net_cr"] for r in store.fii_dii_before(date(2026, 11, 11), 5)] == [-5]
    problem = next(p for p in m.problems() if fname in p)
    assert ("file not found" if damage == "delete" else "no header row") in problem


@pytest.mark.parametrize("fname", list(GOOD))
def test_missing_file_makes_regime_unknown_naming_it(tmp_path, fname):
    s = settings_with_tmp_data(tmp_path)                   # header-only files
    (tmp_path / fname).unlink()
    store = Store(":memory:")
    cal = MarketCalendar()
    m = ManualData(s, store, cal)
    m.load_all()
    hub = MarketHub(s, store, "breeze", cal)
    store_history(store, cal, SESSION)
    live_ticks(hub, T0, nifty=22700.0)
    out = RegimeService(s, store, hub, cal, m).evaluate(T0 + timedelta(seconds=5))
    assert out["regime"] == "UNKNOWN"
    assert any(fname in r and "file not found" in r for r in out["reasons"]), out["reasons"]


def test_header_only_file_is_an_empty_list_not_an_error(tmp_path):
    s = settings_with_tmp_data(tmp_path)                   # writes header-only files
    m = ManualData(s, Store(":memory:"), MarketCalendar())
    m.load_all()
    assert m.problems() == []


def test_fii_import_with_missing_file_keeps_stored_days(tmp_path):
    store = Store(":memory:")
    for name, text in GOOD.items():
        (tmp_path / name).write_text(text)
    load(tmp_path, store)
    (tmp_path / "fii_dii.csv").unlink()
    _, m, _ = load(tmp_path, store)
    res = m.import_text("fii_dii", "date,fii_net_cr,dii_net_cr\n2026-11-11,7,2\n")
    assert res["ok"] and res["rows_total"] == 2
    assert "2026-11-10,-5.0" in (tmp_path / "fii_dii.csv").read_text()
    assert m.problems() == []


# ---------------- 3. negative bid, ask, volume or previous close ----------------
@pytest.mark.parametrize("kw, reason", [
    ({"bid": -1.0, "ask": 100.1}, BAD_PRICE),
    ({"bid": 99.9, "ask": -0.5}, BAD_PRICE),
    ({"bid": -1.0, "ask": -0.5}, BAD_PRICE),
    ({"cum_volume": -50.0}, BAD_VOLUME),
    ({"cum_volume": INF}, BAD_VOLUME),
    ({"prev_close": -1.0}, BAD_PREV_CLOSE),
    ({"prev_close": NAN}, BAD_PREV_CLOSE),
])
def test_negative_fields_are_quarantined(kw, reason):
    assert reason in check_tick(Tick("X", 100.0, **kw), None, 5)


def test_zero_and_missing_fields_still_pass():
    assert check_tick(Tick("X", 100.0, bid=0.0, ask=0.0, cum_volume=0.0, prev_close=0.0), None, 5) == []
    assert check_tick(Tick("X", 100.0), None, 5) == []


def test_negative_breeze_quote_reaches_the_gate(tmp_path):
    t = tick_from_breeze({"last": 100, "bPrice": -1, "sPrice": 0, "ttq": 10, "close": 99}, "RELIANCE", "stock")
    assert t.bid == -1.0                                   # not hidden as "no quote"
    hub = MarketHub(settings_with_tmp_data(tmp_path), Store(":memory:"), "breeze")
    hub.on_tick(Tick(t.symbol, t.last, t.bid, t.ask, t.cum_volume, t.prev_close, received_at=T0))
    assert hub.stats() == {"good_ticks": 0, "quarantined_ticks": 1}
    for raw in ({"last": 100, "bPrice": 0, "sPrice": 100.1}, {"last": 100}):
        assert tick_from_breeze(raw, "RELIANCE", "stock").bid is None   # 0 or missing = no quote
    neg_vol = tick_from_breeze({"last": 100, "ttq": -5, "close": -3}, "RELIANCE", "stock")
    assert set(check_tick(neg_vol, None, 5)) == {BAD_VOLUME, BAD_PREV_CLOSE}
