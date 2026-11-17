"""HTTP routes added in phase 2: /api/regime and POST /data/import/{kind}."""
import pytest
from datetime import timedelta

from fastapi.testclient import TestClient

from app.calendar import MarketCalendar
from app.main import create_app
from app.models import Tick, utcnow
from app.quality import IST
from app.regime import REGIMES
from tests.phase2_fixtures import settings_with_tmp_data


class SeededFakeFeed:
    """Sim-named feed that sends one LIVE tick per symbol at the last simulated close."""
    name = "sim"

    def __init__(self):
        self.prices = {}

    def set_start_prices(self, prices):
        self.prices = dict(prices)

    def start(self, on_tick):
        for sym, px in self.prices.items():
            on_tick(Tick(sym, px, day_high=px * 1.002, day_low=px * 0.998))

    def stop(self):
        pass


def client(tmp_path, seed=True):
    s = settings_with_tmp_data(tmp_path, history={"sim_seed_on_start": seed})
    return TestClient(create_app(s, feed=SeededFakeFeed(), db_path=":memory:", background=False),
                      base_url="http://127.0.0.1:8000")


@pytest.mark.usefixtures("fixed_clock")
def test_regime_route_in_sim_mode(tmp_path):
    with client(tmp_path) as c:
        r = c.get("/api/regime").json()
        assert r["regime"] in REGIMES and r["regime"] != "UNKNOWN", r["reasons"]
        assert r["basis"] == "LIVE" and r["source"] == "sim" and r["reasons"]
        assert r["bias"] == "UNKNOWN"              # no FII data yet
        assert r["rules_version"].startswith("regime_v1/")
        assert len(r["history"]) == 1
        assert r["history_bars"]["INDIAVIX"]["bars_1d"] > 200
        # same data, same answer
        again = c.get("/api/regime").json()
        assert (again["regime"], again["reasons"]) == (r["regime"], r["reasons"])
        assert len(again["history"]) == 1          # unchanged regime is not logged twice


@pytest.mark.usefixtures("fixed_clock")
def test_regime_unknown_without_history(tmp_path):
    with client(tmp_path, seed=False) as c:
        assert c.get("/api/regime").json()["regime"] == "UNKNOWN"


@pytest.mark.usefixtures("fixed_clock")
def test_fii_import_sets_bias(tmp_path):
    cal = MarketCalendar()
    d = utcnow().astimezone(IST).date()            # sim mode classifies today's session
    days = []
    for _ in range(3):
        d = cal.previous_trading_day(d)
        days.append(d)
    csv = "date,fii_net_cr,dii_net_cr\n" + "".join(f"{x.isoformat()},-1500,900\n" for x in days)
    with client(tmp_path) as c:
        res = c.post("/data/import/fii_dii", json={"text": csv})
        assert res.status_code == 200 and res.json()["rows_saved"] == 3
        r = c.get("/api/regime").json()
        assert r["bias"] == "BEARISH" and "net sellers for 3" in r["bias_reasons"][0]


@pytest.mark.usefixtures("fixed_clock")
def test_event_import_forces_event_regime(tmp_path):
    today = utcnow().astimezone(IST).date().isoformat()
    with client(tmp_path) as c:
        res = c.post("/data/import/events", json={"text": f"date,type,note\n{today},BUDGET,Union Budget\n"})
        assert res.status_code == 200
        assert c.get("/api/regime").json()["regime"] == "EVENT"


@pytest.mark.usefixtures("fixed_clock")
def test_bad_import_is_rejected(tmp_path):
    with client(tmp_path, seed=False) as c:
        res = c.post("/data/import/fii_dii", json={"text": "date,fii_net_cr\nyesterday,abc\n"})
        assert res.status_code == 400 and res.json()["errors"]
        assert c.post("/data/import/orders", json={"text": "x"}).status_code == 404
        assert c.post("/data/import/events", json={"text": "x" * 300_000}).status_code == 413


@pytest.mark.usefixtures("fixed_clock")
def test_dashboard_has_regime_badge(tmp_path):
    with client(tmp_path, seed=False) as c:
        html = c.get("/").text
        assert 'id="regime"' in html and "FII bias" in html and "/data/import/" in html
