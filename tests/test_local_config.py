"""config.local.yaml (the machine's own overrides, never committed) applies to the app and never to the tests; and the
Data status page reads the download progress file from its configured path (tests isolate it like other data)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import app.config as config
from app.config import NO_LOCAL_ENV, load_settings


def _local(tmp_path, text):
    p = tmp_path / "config.local.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_the_suite_reads_only_the_committed_settings(tmp_path, monkeypatch):
    assert os.environ.get(NO_LOCAL_ENV) == "1"                         # set by tests/conftest.py
    monkeypatch.setattr(config, "LOCAL_CONFIG", _local(tmp_path, "alerts:\n  telegram_enabled: true\n"))
    assert load_settings().alerts.telegram_enabled is False             # the committed config.yaml


@pytest.mark.usefixtures("fixed_clock")
def test_the_local_override_applies_to_the_app(tmp_path, monkeypatch):
    from app.main import create_app
    from tests.test_step1 import FakeFeed
    monkeypatch.delenv(NO_LOCAL_ENV)                                    # how the app runs: no test switch
    monkeypatch.setattr(config, "LOCAL_CONFIG", _local(tmp_path, "alerts:\n  telegram_enabled: true\n"
                                                                 "feed:\n  mode: sim\n"))
    committed = load_settings(config.ROOT / "config.yaml")             # an explicit path: never merged
    s = load_settings()
    assert committed.alerts.telegram_enabled is False and s.alerts.telegram_enabled is True
    assert s.alerts.top_per_bar == committed.alerts.top_per_bar         # sibling keys of the section are kept
    assert s.account == committed.account and s.risk == committed.risk
    app = create_app(feed=FakeFeed(), db_path=":memory:", background=False)
    assert app.state.engine.s.alerts.telegram_enabled is True
    assert app.state.alerts.s.alerts.telegram_enabled is True


def test_a_bad_local_file_fails_like_a_bad_config(tmp_path, monkeypatch):
    monkeypatch.delenv(NO_LOCAL_ENV)
    monkeypatch.setattr(config, "LOCAL_CONFIG", _local(tmp_path, "alerts:\n  telegram_enabeld: true\n"))
    with pytest.raises(ValueError, match="telegram_enabeld"):
        load_settings()                                                 # a typo is reported, never ignored
    monkeypatch.setattr(config, "LOCAL_CONFIG", _local(tmp_path, "- just a list\n"))
    with pytest.raises(ValueError):
        load_settings()
    monkeypatch.setattr(config, "LOCAL_CONFIG", tmp_path / "absent.yaml")
    assert load_settings().alerts.telegram_enabled is False


# ---------------- the download progress file: configured path, check unchanged ----------------
def _status_rows(tmp_path, updated):
    from app.calendar import MarketCalendar
    from app.data_status import data_status
    from tests.test_data_import import NOW, manual_in
    s, store, m = manual_in(tmp_path)
    m.load_all()
    if updated is not None:
        (tmp_path / "option_download_status.json").write_text(
            json.dumps({"updated": updated.isoformat(), "complete": {}, "runs": []}), encoding="utf-8")
    rows = {r["source"]: r for r in data_status(s, store, MarketCalendar(), "sim", manual=m, now=NOW)["rows"]}
    return rows["Breeze option history (stock options)"], NOW


def test_data_status_reads_the_configured_download_file_not_the_machines_real_one(tmp_path):
    row, now = _status_rows(tmp_path, None)                             # nothing in the test's own path
    assert row["status"] == "MISSING" and row["last_updated_iso"] is None
    assert "future_dated" not in row["detail"]


def test_a_download_progress_record_dated_after_now_is_still_flagged(tmp_path):
    from tests.test_data_import import NOW
    row, _ = _status_rows(tmp_path, NOW + timedelta(hours=26))
    assert row["status"] == "DATA_ERROR" and row["colour"] == "red"
    assert row["detail"]["future_dated"] == [(NOW + timedelta(hours=26)).isoformat()]
    (tmp_path / "b").mkdir()
    ok, _ = _status_rows(tmp_path / "b", NOW - timedelta(hours=2))
    assert ok["status"] != "DATA_ERROR" and ok["last_updated_iso"] is not None


def test_the_download_command_reads_and_writes_the_configured_file(tmp_path):
    from app.options.stock_backtest import read_status, write_status
    from tests.test_data_import import manual_in
    s, _, _ = manual_in(tmp_path)
    assert write_status(s, {"updated": "2026-11-15T08:00:00+00:00", "complete": {}}) == tmp_path / "option_download_status.json"
    assert read_status(s)["updated"] == "2026-11-15T08:00:00+00:00"
