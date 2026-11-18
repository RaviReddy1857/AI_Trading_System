"""Two writers on one SQLite file (the app and the option download, 18-Nov-2026: "database is locked" in
save_option_bars): the store waits for the other writer (busy timeout, 30 s) instead of failing, and file stores use WAL
so readers never block a writer. Every file here is in tmp_path: the suite never opens data/ (tests/conftest.py)."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, datetime, timezone

import pytest

from app.store import BUSY_TIMEOUT_MS, Store

FETCHED = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
EXPIRY = date(2026, 11, 24)


def _bars(n: int, day: int = 1) -> list[tuple]:
    return [(f"2026-10-{day:02d}T04:{m:02d}:00+00:00", 10.0, 11.0, 9.0, 10.5, 100, 1000) for m in range(n)]


def test_a_file_store_uses_wal_and_waits_for_other_writers(tmp_path):
    s = Store(tmp_path / "x.db")
    assert s.journal_mode == "wal"
    assert s._db.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS == 30000
    assert Store(":memory:").journal_mode == "memory"


def test_a_second_writer_waits_for_the_first_instead_of_database_is_locked(tmp_path):
    path = tmp_path / "market_live.db"
    app, download = Store(path, live=True), Store(path, live=True)
    other = sqlite3.connect(str(path), check_same_thread=False)        # a third process, mid-write
    other.execute("BEGIN IMMEDIATE")
    other.execute("INSERT INTO option_fetch_log VALUES ('NIFTY','2026-11-24',24000.0,'CE','1day','breeze','x',0)")
    released = threading.Event()

    def finish_later():
        time.sleep(1.0)
        other.commit()
        released.set()

    threading.Thread(target=finish_later, daemon=True).start()
    t0 = time.monotonic()
    n = download.save_option_bars("breeze", "NIFTY", EXPIRY, 24000.0, "PE", "1day", _bars(5), FETCHED)
    waited = time.monotonic() - t0
    assert n == 5 and released.is_set() and waited >= 0.8                # it waited for the other write
    assert len(app.option_bars("breeze", "NIFTY")[(EXPIRY, 24000.0, "PE")]) == 5
    other.close()


def test_two_stores_writing_at_once_both_succeed(tmp_path):
    path = tmp_path / "market_live.db"
    a, b = Store(path, live=True), Store(path, live=True)
    errors: list[BaseException] = []

    def write(store: Store, strike: float):
        try:
            for day in range(1, 31):
                store.save_option_bars("breeze", "NIFTY", EXPIRY, strike, "CE", "1day", _bars(30, day), FETCHED)
        except BaseException as e:                                        # noqa: BLE001 - reported below
            errors.append(e)

    threads = [threading.Thread(target=write, args=(a, 24000.0)), threading.Thread(target=write, args=(b, 24100.0))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    got = a.option_bars("breeze", "NIFTY")
    assert len(got[(EXPIRY, 24000.0, "CE")]) == len(got[(EXPIRY, 24100.0, "CE")]) == 900


def test_a_reader_does_not_block_a_writer_under_wal(tmp_path):
    path = tmp_path / "x.db"
    w = Store(path)
    w.save_option_bars("breeze", "NIFTY", EXPIRY, 24000.0, "CE", "1day", _bars(3), FETCHED)
    reader = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    reader.execute("BEGIN")
    assert reader.execute("SELECT COUNT(*) FROM option_bars").fetchone()[0] == 3     # an open read transaction
    t0 = time.monotonic()
    w.save_option_bars("breeze", "NIFTY", EXPIRY, 24100.0, "CE", "1day", _bars(3), FETCHED)
    assert time.monotonic() - t0 < 5
    reader.close()


def test_the_suite_refuses_to_open_the_real_data_folder():
    from tests.conftest import REAL_DATA, RealDataAccess
    with pytest.raises(RealDataAccess):
        sqlite3.connect(str(REAL_DATA / "market_live.db"))
    with pytest.raises(RealDataAccess):
        sqlite3.connect(f"file:{(REAL_DATA / 'market_live.db').as_posix()}?mode=ro", uri=True)
    target = REAL_DATA / "backtests" / "never_written_by_a_test.json"
    for mode in ("w", "a", "x", "r+"):
        with pytest.raises(RealDataAccess):
            open(target, mode)
    assert not target.exists()
