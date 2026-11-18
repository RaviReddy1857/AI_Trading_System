"""The test suite reads only the committed settings: config.yaml, never the machine's own config.local.yaml.

It never depends on the machine's clock either (tests/clock.py): a test that depends on today's date or the current
time uses the shared `fixed_clock` fixture (FIXED_NOW, Monday 16-Nov-2026 10:30 IST, the fixtures' trading day).
`--fake-clock=<ISO time>` runs the whole suite as if the machine's clock read that time, e.g.
`python -m pytest -q --fake-clock=2026-11-21T23:59+05:30`."""
import ctypes
import faulthandler
import os
import sys
import threading
from pathlib import Path

from app.config import NO_LOCAL_ENV
from tests import clock

import pytest

# Every test has a time limit (`TEST_TIMEOUT_SECONDS`, default 300; a test may set its own with
# @pytest.mark.timeout(seconds)). On expiry the stacks of every thread are printed and the test fails with
# TestTimeout. A test stuck inside a C call (a lock, a socket) cannot take that exception: then, 60 seconds later,
# the run ends with the stacks printed (faulthandler), so a hung test never blocks the suite silently.
DEFAULT_TIMEOUT = float(os.environ.get("TEST_TIMEOUT_SECONDS", "300"))


class TestTimeout(Exception):
    pass

os.environ[NO_LOCAL_ENV] = "1"
clock.install()

# The machine's real data (data/) is never touched by the suite's databases: a test that opens an SQLite file under
# data/ fails at once (RealDataAccess), so a test run can never hold a lock on data/market_live.db while the app or a
# download writes to it ("database is locked", 18-Nov-2026). TRADING_AUDIT_REAL_DATA=1 allows it, for the opt-in
# audits of the real files (tests/test_safety.py, tests/test_review6_fixes.py). Opening any file under data/ for
# writing fails the same way (a test once overwrote data/backtests/*_latest.json). Files only read under data/ are
# recorded per test (--data-access-report=FILE).
REAL_DATA = (Path(__file__).resolve().parent.parent / "data").resolve()
AUDIT_REAL_DATA_ENV = "TRADING_AUDIT_REAL_DATA"
data_reads: dict[str, set[str]] = {}


class RealDataAccess(RuntimeError):
    pass


def _under_real_data(target) -> Path | None:
    try:
        s = os.fsdecode(target)
    except TypeError:
        return None
    if not s or s == ":memory:":
        return None
    if s.startswith("file:"):
        s = s[5:].split("?", 1)[0]
    try:
        p = Path(s).resolve()
    except (OSError, ValueError):
        return None
    return p if p == REAL_DATA or REAL_DATA in p.parents else None


def _audit(event, args):
    if event == "sqlite3.connect":
        p = _under_real_data(args[0])
        if p is not None and os.environ.get(AUDIT_REAL_DATA_ENV) != "1":
            raise RealDataAccess(f"a test opened the real database {p}: use tmp_path or ':memory:' "
                                 f"(or set {AUDIT_REAL_DATA_ENV}=1 for an opt-in audit of the real files)")
    elif event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        p = _under_real_data(args[0])
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or bool(
            flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC))
        if p is not None and writing and os.environ.get(AUDIT_REAL_DATA_ENV) != "1":
            raise RealDataAccess(f"a test opened {p} for writing: write to tmp_path, never to data/")
        if p is not None:
            data_reads.setdefault(os.environ.get("PYTEST_CURRENT_TEST", "(collection)").split(" ")[0],
                                  set()).add(str(p.relative_to(REAL_DATA.parent)))


sys.addaudithook(_audit)


def pytest_addoption(parser):
    parser.addoption("--fake-clock", default=None,
                     help="run the suite as if the machine's clock read this ISO time (with an offset)")
    parser.addoption("--clock-report", default=None,
                     help="write the tests that read the clock without the fixed_clock fixture to this file")
    parser.addoption("--data-access-report", default=None,
                     help="write every test that opened a file under data/ (and which files) to this file")


def pytest_sessionfinish(session):
    path = session.config.getoption("--clock-report")
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(t + "\n" for t in sorted(clock.unfixed_reads)))
    path = session.config.getoption("--data-access-report")
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(f"{t}\t{', '.join(sorted(v))}\n" for t, v in sorted(data_reads.items())))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    m = item.get_closest_marker("timeout")
    limit = float(m.args[0]) if m and m.args else DEFAULT_TIMEOUT
    main = threading.main_thread().ident

    def expire():
        sys.stderr.write(f"\n*** TIMEOUT: {item.nodeid} ran longer than {limit:g} s; stacks of every thread:\n")
        faulthandler.dump_traceback(all_threads=True)
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(main), ctypes.py_object(TestTimeout))
    timer = threading.Timer(limit, expire)
    timer.daemon = True
    timer.start()
    faulthandler.dump_traceback_later(limit + 60, exit=True)      # last resort: stuck in a C call
    try:
        yield
    finally:
        timer.cancel()
        faulthandler.cancel_dump_traceback_later()


def pytest_configure(config):
    config.addinivalue_line("markers", "timeout(seconds): this test's time limit (default TEST_TIMEOUT_SECONDS, 300)")
    at = config.getoption("--fake-clock")
    if at:
        t = clock.REAL_DATETIME.fromisoformat(at)
        if t.tzinfo is None:
            raise pytest.UsageError("--fake-clock needs a time with its offset, e.g. 2026-11-21T09:00+05:30")
        clock.push(t)


@pytest.fixture(autouse=True)
def _test_clock_types(request):
    """Modules imported since the last test get the clock's types; notes which test is running (--clock-report)."""
    clock.patch_loaded_modules()
    clock.current_test = request.node.nodeid
    yield
    clock.current_test = None


@pytest.fixture
def fixed_clock():
    """The shared fixed clock: FIXED_NOW (ticking from there). Use it in every test that depends on today's date or
    the current time - directly, or for a whole module with `pytestmark = pytest.mark.usefixtures("fixed_clock")`."""
    clock.patch_loaded_modules()
    with clock.travel(clock.FIXED_NOW) as at:
        yield at
