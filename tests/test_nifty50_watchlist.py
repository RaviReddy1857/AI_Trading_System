"""Nifty 50 watchlist: symbol -> Breeze code mapping, sector field, config rewrite,
paced Breeze subscriptions with a summary, paced/retrying/skipping history download,
and breadth over every watchlist stock. Uses small fixture lists only."""
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from app.config import Settings
from app.feeds.breeze_feed import BreezeFeed, parse_scrip_master
from app.history import (DAILY, INTRADAY, BreezeHistorySource, HistoryLoader, SimHistorySource,
                         daily_start, summarize)
from app.manual_data import ManualData
from app.models import Bar, Tick
from app.calendar import MarketCalendar
from app.hub import MarketHub
from app.regime import RegimeService
from app.store import Store
from app.watchlist import (BEGIN, END, Constituent, map_constituents, read_constituents,
                           render_block, replace_block, update_config)
from tests.fixture_settings import FIXTURE_WATCHLIST, load_settings
from tests.phase2_fixtures import SESSION, STOCKS, T0, live_ticks, settings_with_tmp_data, store_history
from tests.test_breeze_feed import FakeBreezeSDK, TOKENS

# ---------------- fixtures ----------------
CSV = ("\ufeffCompany Name,Industry,Symbol,Series,ISIN Code\n"
       "Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018\n"
       "Mahindra & Mahindra Ltd.,Automobile and Auto Components,M&M,EQ,INE101A01026\n"
       "HDFC Bank Ltd.,Financial Services,HDFCBANK,EQ,INE040A01034\n"
       "New Listing Ltd.,Services,NEWCO,EQ,INE999Z01011\n")


def scrip(token, code, series, company, isin, symbol):
    return {"Token": token, "ShortName": code, "Series": series, "CompanyName": company,
            "ISINCode": isin, "ExchangeCode": symbol}


SCRIPS = [
    scrip("2885", "RELIND", "EQ", "RELIANCE INDUSTRIES", "INE002A01018", "RELIANCE"),
    scrip("2031", "MAHMAH", "EQ", "MAHINDRA & MAHINDRA LIMITED", "INE101A01026", "M&M"),
    scrip("1333", "HDFBAN", "EQ", "HDFC BANK LIMITED", "INE040A01034", "HDFCBANK"),
    # same NSE symbol but a warrant (series W3): must be ignored
    scrip("17320", "HDFWA2", "W3", "HDFC BANK WARRANT", "INE040A13013", "HDFCBANK"),
]

SMALL_CONFIG = """account: {capital_inr: 200000, mode: paper}
risk: {max_loss_per_trade_pct: 1, max_loss_per_option_pct: 2.5, daily_loss_stop_pct: 2,
       monthly_loss_pause_pct: 6, uncommitted_cash_pct: 25, max_open_selling_spreads: 2,
       drawdown_halve_pct: 10, drawdown_pause_pct: 15}
feed: {mode: sim, stale_after_seconds: 30, max_jump_pct: 5}
watchlist:
  - {symbol: NIFTY,     breeze_code: NIFTY,  kind: index}
  - {symbol: INDIAVIX,  breeze_code: INDVIX, kind: index}
%s
%s
  - {symbol: TCS,       breeze_code: TCS,    kind: stock}
"""


def small_config(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_bytes((SMALL_CONFIG % (BEGIN, END)).encode())
    return p


# ---------------- 1. mapping ----------------
def test_read_constituents_handles_bom_and_columns():
    rows = read_constituents(CSV)
    assert [r.symbol for r in rows] == ["RELIANCE", "M&M", "HDFCBANK", "NEWCO"]
    assert rows[1] == Constituent("Mahindra & Mahindra Ltd.", "Automobile and Auto Components",
                                  "M&M", "INE101A01026")


@pytest.mark.parametrize("text, msg", [
    ("Company Name,Symbol\nX,Y\n", "Industry"),
    ("Company Name,Industry,Symbol\nA,Power,X\nB,Power,X\n", "twice"),
    ("Company Name,Industry,Symbol\nA,,X\n", "empty Industry"),
    ("Company Name,Industry,Symbol\n", "no rows"),
])
def test_read_constituents_rejects_bad_files(text, msg):
    with pytest.raises(ValueError, match=msg):
        read_constituents(text)


def test_mapping_uses_eq_row_and_isin_and_lists_unmapped():
    mapped, unmapped = map_constituents(read_constituents(CSV), SCRIPS)
    assert [(m.symbol, m.breeze_code, m.sector) for m in mapped] == [
        ("RELIANCE", "RELIND", "Oil Gas & Consumable Fuels"),
        ("M&M", "MAHMAH", "Automobile and Auto Components"),
        ("HDFCBANK", "HDFBAN", "Financial Services"),       # warrant row ignored
    ]
    assert [s for s, _ in unmapped] == ["NEWCO"]
    assert "not" in unmapped[0][1].lower() or "no EQ" in unmapped[0][1]


def test_isin_mismatch_and_ambiguous_codes_are_not_mapped():
    c = [Constituent("Reliance", "Energy", "RELIANCE", "INE000000000"),
         Constituent("Dup", "Power", "DUP", None)]
    rows = SCRIPS + [scrip("1", "DUPA", "EQ", "DUP A", "", "DUP"), scrip("2", "DUPB", "EQ", "DUP B", "", "DUP")]
    mapped, unmapped = map_constituents(c, rows)
    assert mapped == []
    assert dict(unmapped)["RELIANCE"].startswith("instrument list has the symbol but not ISIN")
    assert "ambiguous" in dict(unmapped)["DUP"]


def test_parse_scrip_master_reads_icici_header_layout():
    text = ['Token, "ShortName", "Series", "CompanyName", "ISINCode", "Other", "ExchangeCode"',
            '"2885","RELIND","EQ","RELIANCE INDUSTRIES","INE002A01018","x","RELIANCE"',
            '"1","SHORT"']                                    # short row skipped
    assert parse_scrip_master(text) == [SCRIPS[0]]
    with pytest.raises(ValueError, match="ExchangeCode"):
        parse_scrip_master(['Token, "ShortName"'])


# ---------------- 2. config rewrite with sectors ----------------
def test_block_round_trips_through_yaml_with_ampersands():
    mapped, _ = map_constituents(read_constituents(CSV), SCRIPS)
    entries = yaml.safe_load("w:\n" + render_block(mapped))["w"]
    assert entries[1] == {"symbol": "M&M", "breeze_code": "MAHMAH", "kind": "stock",
                          "sector": "Automobile and Auto Components"}
    assert entries[0]["sector"] == "Oil Gas & Consumable Fuels"


def test_update_config_rewrites_only_the_block_and_keeps_indices(tmp_path):
    path = small_config(tmp_path)
    before = path.read_text()
    rep = update_config(path, CSV, SCRIPS)
    assert rep["written"] and rep["unmapped"] == [("NEWCO", rep["unmapped"][0][1])]
    assert rep["added"] == ["HDFCBANK", "M&M", "RELIANCE"] and rep["removed"] == []
    s = Settings.model_validate(yaml.safe_load(path.read_text()))
    assert [i.symbol for i in s.watchlist] == ["NIFTY", "INDIAVIX", "RELIANCE", "M&M", "HDFCBANK", "TCS"]
    assert s.watchlist[0].sector is None and s.watchlist[3].sector == "Automobile and Auto Components"
    after = path.read_text()
    assert after.split(BEGIN)[0] == before.split(BEGIN)[0]          # indices untouched
    assert after.split(END)[1] == before.split(END)[1]              # TCS line after the block untouched
    assert b"\r\n" not in path.read_bytes()                          # line endings kept
    # running again changes nothing
    assert update_config(path, CSV, SCRIPS)["written"] is False


def test_update_config_dry_run_writes_nothing(tmp_path):
    path = small_config(tmp_path)
    before = path.read_bytes()
    rep = update_config(path, CSV, SCRIPS, write=False)
    assert rep["written"] is False and path.read_bytes() == before and len(rep["mapped"]) == 3


def test_update_config_refuses_invalid_result_and_missing_markers(tmp_path):
    path = small_config(tmp_path)
    before = path.read_bytes()
    clash = "Company Name,Industry,Symbol\nTata Consultancy,IT,TCS\n"     # TCS is already outside the block
    with pytest.raises(ValueError, match="duplicate"):
        update_config(path, clash, [scrip("1", "TCS", "EQ", "TCS", "", "TCS")])
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="marker"):
        replace_block("watchlist: []\n", render_block([]))


def test_config_sector_optional_and_duplicates_rejected():
    raw = load_settings().model_dump()
    assert {i["symbol"]: i.get("sector") for i in raw["watchlist"]}["TCS"] == "Information Technology"
    raw["watchlist"].append(dict(raw["watchlist"][2]))
    with pytest.raises(ValueError, match="duplicate symbol"):
        Settings.model_validate(raw)


def test_real_config_has_all_nifty50_stocks_with_sectors():
    """The real config.yaml (not the fixture): every stock has a sector and indices are kept."""
    from app.config import load_settings as real
    s = real()
    stocks = [i for i in s.watchlist if i.kind == "stock"]
    assert stocks and all(i.sector for i in stocks)
    assert {"NIFTY", "BANKNIFTY", "INDIAVIX"} <= {i.symbol for i in s.watchlist if i.kind == "index"}


# ---------------- 3. live feed subscriptions ----------------
def test_subscriptions_are_paced_and_summarised(caplog):
    sleeps = []
    sdk = FakeBreezeSDK()
    feed = BreezeFeed(load_settings().watchlist, subscribe_pause_seconds=0.25, sleep=sleeps.append)
    feed._login = lambda: sdk
    with caplog.at_level(logging.INFO, logger="breeze_feed"):
        feed.start(lambda t: None)
    n = len(FIXTURE_WATCHLIST)
    assert sleeps == [0.25] * (n - 1)                     # a pause between, not before the first
    assert feed.subscription_summary["subscribed"] == [i["symbol"] for i in FIXTURE_WATCHLIST]
    assert f"subscribed {n} of {n} symbols" in caplog.text


class FlakySDK(FakeBreezeSDK):
    """INFY's token is unknown, TCS's subscribe returns the SDK's error string, HDFCBANK's raises."""
    def subscribe_feeds(self, stock_token="", interval="", **kw):
        self.interval = interval
        if stock_token.endswith(TOKENS["TCS"]):
            return "Exception while subscribing to feeds boom"
        if stock_token.endswith(TOKENS["HDFBAN"]):
            raise RuntimeError("socket closed")
        return super().subscribe_feeds(stock_token, interval)


def test_failed_subscriptions_are_listed_and_others_continue(caplog):
    sdk = FlakySDK(tokens={k: v for k, v in TOKENS.items() if k != "INFTEC"})
    feed = BreezeFeed(load_settings().watchlist)
    feed._login = lambda: sdk
    got = []
    with caplog.at_level(logging.INFO, logger="breeze_feed"):
        feed.start(got.append)
    summ = feed.subscription_summary
    assert [s for s, _, _ in summ["failed"]] == ["HDFCBANK", "INFY", "TCS"]
    assert summ["subscribed"] == ["NIFTY", "BANKNIFTY", "RELIANCE", "ICICIBANK", "INDIAVIX"]
    assert "subscribed 5 of 8 symbols" in caplog.text
    assert "3 failed: HDFCBANK (HDFBAN), INFY (INFTEC), TCS (TCS)" in caplog.text
    # failed symbols are not routed: a tick for TCS's token is ignored
    sdk.on_ticks({"symbol": "4.1!" + TOKENS["TCS"], "quotes": "Quotes Data", "last": 1})
    assert got == []


def test_feed_handles_53_subscriptions():
    from app.config import Instrument
    watch = [Instrument(symbol=f"S{i:02d}", breeze_code=f"C{i:02d}", kind="stock") for i in range(53)]
    sdk = FakeBreezeSDK(tokens={f"C{i:02d}": str(1000 + i) for i in range(53)})
    sleeps = []
    feed = BreezeFeed(watch, subscribe_pause_seconds=0.1, sleep=sleeps.append)
    feed._login = lambda: sdk
    feed.start(lambda t: None)
    assert len(sdk.subscribed) == 53 and len(set(sdk.subscribed)) == 53
    assert len(feed.subscription_summary["subscribed"]) == 53 and len(sleeps) == 52


# ---------------- 4. history: pause, retry on empty, skip what is stored ----------------
class ScriptedSource:
    """Remote-like source: returns sim rows, except the first `empty_first` calls per key are empty."""
    name = "breeze"
    remote = True

    def __init__(self, empty_first=0, always_empty=()):
        self.sim = SimHistorySource()
        self.calls = []
        self.empty_first = empty_first
        self.always_empty = set(always_empty)
        self._seen = {}

    def fetch(self, inst, interval, start, end):
        self.calls.append((inst.symbol, interval, start, end))
        key = (inst.symbol, interval, start)
        self._seen[key] = self._seen.get(key, 0) + 1
        if inst.symbol in self.always_empty or self._seen[key] <= self.empty_first:
            return []
        return self.sim.fetch(inst, interval, start, end)


def one_symbol_settings(symbol="RELIANCE"):
    raw = load_settings().model_dump()
    raw["watchlist"] = [i for i in raw["watchlist"] if i["kind"] == "index" or i["symbol"] == symbol]
    return Settings.model_validate(raw)


def test_breeze_source_is_remote_and_sim_is_not():
    assert BreezeHistorySource(None).remote is True and SimHistorySource.remote is False


def test_remote_requests_are_paced():
    s = load_settings()
    sleeps = []
    src = ScriptedSource()
    HistoryLoader(s, Store(":memory:"), src, sleep=sleeps.append).update(T0)
    assert len(src.calls) > 1
    assert sleeps == [s.history.request_pause_seconds] * (len(src.calls) - 1)


def test_empty_reply_is_retried_then_succeeds():
    s = one_symbol_settings()
    sleeps = []
    src = ScriptedSource(empty_first=1)
    out = HistoryLoader(s, Store(":memory:"), src, sleep=sleeps.append).update(T0, symbols={"RELIANCE"})
    assert out["RELIANCE:1day"]["saved"] > 250 and "empty" not in out["RELIANCE:1day"]
    assert s.history.retry_pause_seconds in sleeps
    daily = [c for c in src.calls if c[1] == DAILY]
    assert len(daily) == 2 and daily[0] == daily[1]          # same request asked twice


def test_empty_after_all_retries_is_reported_not_fatal():
    s = one_symbol_settings()
    src = ScriptedSource(always_empty={"RELIANCE"})
    out = HistoryLoader(s, Store(":memory:"), src, sleep=lambda x: None).update(T0)
    daily = [c for c in src.calls if c[0] == "RELIANCE" and c[1] == DAILY]
    assert len(daily) == 1 + s.history.empty_retries
    assert out["RELIANCE:1day"] == {"saved": 0, "rejected": 0, "requests": 1, "empty": True}
    assert out["NIFTY:1day"]["saved"] > 250                  # other symbols still downloaded
    assert "EMPTY  RELIANCE:1day" in summarize(out)


def test_sim_source_never_sleeps_or_retries():
    sleeps = []
    HistoryLoader(load_settings(), Store(":memory:"), SimHistorySource(), sleep=sleeps.append).update(T0)
    assert sleeps == []


AFTER_CLOSE = datetime(2026, 11, 11, 11, 0, tzinfo=timezone.utc)    # Wed 16:30 IST


def test_already_stored_symbols_are_skipped_without_a_request():
    s = load_settings()
    store = Store(":memory:")
    HistoryLoader(s, store, ScriptedSource(), sleep=lambda x: None).update(AFTER_CLOSE)
    src = ScriptedSource()
    out = HistoryLoader(s, store, src, sleep=lambda x: None).update(AFTER_CLOSE + timedelta(hours=1))
    assert src.calls == []
    assert all(v.get("skipped") == "already stored" for v in out.values())
    assert f"{len(out)} already stored (skipped)" in summarize(out)


def test_new_symbol_is_downloaded_while_stored_ones_are_skipped():
    store = Store(":memory:")
    HistoryLoader(one_symbol_settings("TCS"), store, ScriptedSource(), sleep=lambda x: None).update(AFTER_CLOSE)
    src = ScriptedSource()
    HistoryLoader(load_settings(), store, src, sleep=lambda x: None).update(AFTER_CLOSE)
    fetched = {c[0] for c in src.calls}
    assert "TCS" not in fetched and "NIFTY" not in fetched
    assert {"RELIANCE", "HDFCBANK", "ICICIBANK", "INFY"} <= fetched


def test_not_skipped_when_a_newer_bar_is_due():
    s = load_settings()
    store = Store(":memory:")
    HistoryLoader(s, store, ScriptedSource(), sleep=lambda x: None).update(AFTER_CLOSE)
    src = ScriptedSource()
    # next day during market hours: the daily bar for Wed is stored, so daily is skipped;
    # 5-minute bars keep forming while the market is open, so those are fetched.
    HistoryLoader(s, store, src, sleep=lambda x: None).update(AFTER_CLOSE + timedelta(hours=18))
    assert {c[1] for c in src.calls} == {INTRADAY}
    # the day after that, the Thursday daily bar is due again
    src2 = ScriptedSource()
    HistoryLoader(s, store, src2, sleep=lambda x: None).update(AFTER_CLOSE + timedelta(days=1, hours=1))
    assert DAILY in {c[1] for c in src2.calls}


def test_history_cli_rejects_unknown_symbols(monkeypatch):
    import app.history as h
    monkeypatch.setattr("app.config.load_settings", load_settings)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)     # never touch .env in tests
    with pytest.raises(SystemExit, match="NOTASTOCK"):
        h.main(["NOTASTOCK"])


# ---------------- 5. breadth uses every watchlist stock (60% threshold kept) ----------------
TEN = [f"N{i:02d}" for i in range(10)]


def breadth_service(tmp_path, n_above):
    wl = [i for i in FIXTURE_WATCHLIST if i["kind"] == "index"]
    wl += [{"symbol": s, "breeze_code": s, "kind": "stock", "sector": "Test"} for s in TEN]
    s = settings_with_tmp_data(tmp_path, watchlist=wl)
    store, cal = Store(":memory:"), MarketCalendar()
    manual = ManualData(s, store, cal)
    manual.load_all()
    hub = MarketHub(s, store, "breeze", cal)
    store_history(store, cal, SESSION)                       # NIFTY trend up, VIX calm
    d = SESSION
    for _ in range(30):
        d = cal.previous_trading_day(d)
        store.save_history_bars("bars_1d", [Bar(x, daily_start(d), 1000, 1001, 999, 1000, None) for x in TEN],
                                "breeze")
    live_ticks(hub, T0, nifty=22700.0, skip=tuple(STOCKS))
    for k, sym in enumerate(TEN):
        px = 1100.0 if k < n_above else 900.0
        hub.on_tick(Tick(sym, px, received_at=T0, source="breeze", day_high=px, day_low=px))
    return RegimeService(s, store, hub, cal, manual)


def test_breadth_counts_every_watchlist_stock_and_keeps_60pct(tmp_path):
    svc = breadth_service(tmp_path, n_above=6)
    inputs, _ = svc.gather(T0 + timedelta(seconds=5))
    assert sorted(inputs.stocks) == TEN
    out = svc.evaluate(T0 + timedelta(seconds=5))
    assert out["values"]["breadth_pct"] == 60.0 and out["regime"] == "TRENDING_UP", out["reasons"]
    assert any("of 10 stocks" in r for r in out["reasons"])


def test_breadth_below_60pct_is_not_trending(tmp_path):
    out = breadth_service(tmp_path, n_above=5).evaluate(T0 + timedelta(seconds=5))
    assert out["values"]["breadth_pct"] == 50.0 and out["regime"] == "RANGE", out["reasons"]
