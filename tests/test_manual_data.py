from datetime import date, datetime, timedelta, timezone

from app.calendar import MarketCalendar
from app.hub import MarketHub
from app.manual_data import ManualData, parse_events, parse_fii_dii, parse_holidays
from app.models import Tick
from app.quality import freshness, is_market_hours
from app.store import Store
from tests.phase2_fixtures import settings_with_tmp_data

T0 = datetime(2026, 11, 11, 5, 0, tzinfo=timezone.utc)   # Wed 10:30 IST


# ---------------- events ----------------
def test_parse_events_valid_and_comments_ignored():
    text = "# comment\ndate,type,note,symbol\n2026-12-04,RBI_POLICY,policy,\n2026-12-05,results,Q3,infy\n"
    events, errors = parse_events(text)
    assert errors == []
    assert [(e.date, e.type, e.symbol) for e in events] == [
        (date(2026, 12, 4), "RBI_POLICY", ""), (date(2026, 12, 5), "RESULTS", "INFY")]


def test_parse_events_reports_bad_rows():
    _, errors = parse_events("date,type,note\n2026-13-01,BUDGET,x\n2026-12-01,PARTY,x\n")
    assert len(errors) == 2 and "line 2" in errors[0] and "PARTY" in errors[1]
    assert parse_events("when,what\n2026-12-01,BUDGET\n")[1]       # bad header


# ---------------- holidays ----------------
def test_parse_holidays_nse_layout():
    text = "Sr. No,Date,Day,Description\n1,26-Jan-2026,Monday,Republic Day\n2,2026-12-25,Friday,Christmas\n"
    hol, errors = parse_holidays(text)
    assert errors == [] and hol == {date(2026, 1, 26): "Republic Day", date(2026, 12, 25): "Christmas"}


def test_holiday_shows_closed():
    hol = {date(2026, 11, 11): "test holiday"}
    assert is_market_hours(T0) and not is_market_hours(T0, hol)
    assert freshness(T0, T0, 30, holidays=hol) == "CLOSED"
    cal = MarketCalendar(hol)
    assert not cal.is_trading_day(date(2026, 11, 11))
    assert cal.previous_trading_day(date(2026, 11, 12)) == date(2026, 11, 10)
    assert cal.next_trading_day(date(2026, 11, 10)) == date(2026, 11, 12)


def test_hub_marks_symbols_closed_on_holiday(tmp_path):
    s = settings_with_tmp_data(tmp_path)
    cal = MarketCalendar({date(2026, 11, 11): "test holiday"})
    hub = MarketHub(s, Store(":memory:"), "breeze", cal)
    hub.on_tick(Tick("NIFTY", 23500.0, received_at=T0))
    row = next(r for r in hub.snapshot(T0) if r["symbol"] == "NIFTY")
    assert row["status"] == "CLOSED" and not row["usable"]
    assert hub.live_quote("NIFTY", T0) is None
    cal.set_holidays({})
    assert next(r for r in hub.snapshot(T0) if r["symbol"] == "NIFTY")["status"] == "LIVE"


# ---------------- FII / DII ----------------
def test_parse_fii_dii_own_layout():
    rows, errors = parse_fii_dii("date,fii_net_cr,dii_net_cr\n2026-11-10,-1234.5,987\n2026-11-09,200,\n")
    assert errors == []
    assert rows[date(2026, 11, 10)].fii_net_cr == -1234.5 and rows[date(2026, 11, 9)].dii_net_cr is None


def test_parse_fii_dii_nse_paste():
    text = ('Category,Date,Buy Value(Rs Crores),Sell Value (Rs Crores),Net Value (Rs Crores)\n'
            '"DII **",10-Nov-2026,"12,345.67","10,000.00","2,345.67"\n'
            '"FII/FPI *",10-Nov-2026,"9,000.00","11,500.25","-2,500.25"\n')
    rows, errors = parse_fii_dii(text)
    assert errors == []
    r = rows[date(2026, 11, 10)]
    assert r.fii_net_cr == -2500.25 and r.dii_net_cr == 2345.67


def test_parse_fii_dii_rejects_dii_without_fii():
    _, errors = parse_fii_dii("Category,Date,Buy,Sell,Net Value\nDII,10-Nov-2026,1,1,5\n")
    assert errors and "no matching FII" in errors[0]


# ---------------- import: all-or-nothing, files written ----------------
def manager(tmp_path):
    s = settings_with_tmp_data(tmp_path)
    store = Store(":memory:")
    cal = MarketCalendar()
    m = ManualData(s, store, cal)
    m.load_all()
    return m, store, cal


def test_fii_import_merges_by_date(tmp_path):
    m, store, _ = manager(tmp_path)
    assert m.import_text("fii_dii", "date,fii_net_cr,dii_net_cr\n2026-11-09,-10,5\n2026-11-10,-20,5\n")["ok"]
    res = m.import_text("fii_dii", "date,fii_net_cr,dii_net_cr\n2026-11-10,-25,5\n2026-11-11,30,1\n")
    assert res["ok"] and res["rows_total"] == 3
    got = {r["date"]: r["fii_net_cr"] for r in store.fii_dii_before(date(2026, 11, 12), 10)}
    assert got == {date(2026, 11, 9): -10, date(2026, 11, 10): -25, date(2026, 11, 11): 30}
    assert "2026-11-10,-25.0" in (tmp_path / "fii_dii.csv").read_text()


def test_invalid_import_changes_nothing(tmp_path):
    m, store, _ = manager(tmp_path)
    m.import_text("events", "date,type,note\n2026-12-04,RBI_POLICY,policy\n")
    before = (tmp_path / "events.csv").read_text()
    res = m.import_text("events", "date,type,note\n2026-12-05,BUDGET,x\nbad-date,BUDGET,y\n")
    assert not res["ok"] and res["rows_saved"] == 0
    assert (tmp_path / "events.csv").read_text() == before
    assert [e["type"] for e in store.events_on(date(2026, 12, 4))] == ["RBI_POLICY"]
    assert store.events_on(date(2026, 12, 5)) == []
    assert not m.import_text("weather", "x")["ok"]


def test_holiday_import_updates_calendar(tmp_path):
    m, _, cal = manager(tmp_path)
    assert m.import_text("holidays", "date,note\n2026-11-11,test holiday\n")["ok"]
    assert not cal.is_trading_day(date(2026, 11, 11))


def test_invalid_file_on_disk_is_not_loaded(tmp_path):
    (tmp_path / "events.csv").write_text("date,type,note\n2026-12-04,NOPE,x\n")
    (tmp_path / "holidays.csv").write_text("date,note\nnot-a-date,x\n")
    m, store, cal = manager(tmp_path)
    assert m.errors["events"] and m.errors["holidays"]
    assert cal.holidays == frozenset()
