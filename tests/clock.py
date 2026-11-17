"""The test clock. Tests never depend on the machine's date or time.

`install()` (tests/conftest.py; an import hook for every module loaded later) swaps the names bound to
`datetime.datetime` and `datetime.date` in every loaded `app.*` and `tests.*` module for subclasses whose `now()`,
`utcnow()` and `today()` read this clock. Everything else about them is the real type (`isinstance` against the real
types holds both ways), and the app's code is unchanged: it still calls `datetime.now(...)` / `date.today()` /
`app.models.utcnow()`. The `datetime` module itself is not changed, so third-party libraries are not affected; the
one app clock read this does not reach is `app/feeds/breeze_feed.py`'s function-level import, which stamps the time
of an expired login (no test depends on that time).

With nothing pushed the clock is the real one. `travel(at)` pushes a moment that then runs on in real time (tick), so
time still advances for timeouts. Two uses:
- the shared `fixed_clock` fixture (tests/conftest.py): FIXED_NOW, the fixtures' trading day, for every test that
  depends on today's date or the current time;
- `--fake-clock=<ISO time>` (tests/conftest.py): the whole run as if the machine's clock read that time, to prove no
  other test depends on it.
"""
from __future__ import annotations

import datetime as _dt
import sys
import time as _time
from contextlib import contextmanager

REAL_DATETIME, REAL_DATE = _dt.datetime, _dt.date
UTC = _dt.timezone.utc
FIXED_NOW = REAL_DATETIME(2026, 11, 16, 5, 0, tzinfo=UTC)     # Monday 16-Nov-2026 10:30 IST, the fixtures' trading day

_stack: list[tuple[REAL_DATETIME, float]] = []                # (moment, time.monotonic() when it was pushed)


# which tests read the clock without the fixed clock (the --clock-report option of tests/conftest.py)
current_test: str | None = None
fixed_active = False
unfixed_reads: set[str] = set()


def now_utc() -> REAL_DATETIME:
    if current_test is not None and not fixed_active:
        unfixed_reads.add(current_test)
    if not _stack:
        return REAL_DATETIME.now(UTC)
    at, m0 = _stack[-1]
    return at + _dt.timedelta(seconds=_time.monotonic() - m0)


@contextmanager
def travel(at: REAL_DATETIME):
    """The clock reads `at` (an aware datetime) from now on, ticking, until the block ends: a fixed clock for the
    test that uses it (the `fixed_clock` fixture is this at FIXED_NOW)."""
    global fixed_active
    if at.tzinfo is None:
        raise ValueError("travel() needs an aware datetime")
    was = fixed_active
    fixed_active = True
    _stack.append((at.astimezone(UTC), _time.monotonic()))
    try:
        yield at
    finally:
        _stack.pop()
        fixed_active = was


def push(at: REAL_DATETIME) -> None:
    """The base clock of a whole run (--fake-clock): the machine's clock as if it read `at`. Not a fixed clock."""
    _stack.append((at.astimezone(UTC), _time.monotonic()))


class _DateMeta(type):
    """isinstance/issubclass against FakeDate itself mean the real date type; a subclass checks as usual."""
    def __instancecheck__(cls, obj):
        return isinstance(obj, REAL_DATE) if cls is FakeDate else type.__instancecheck__(cls, obj)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, REAL_DATE) if cls is FakeDate else type.__subclasscheck__(cls, sub)


class _DatetimeMeta(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, REAL_DATETIME) if cls is FakeDatetime else type.__instancecheck__(cls, obj)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, REAL_DATETIME) if cls is FakeDatetime else type.__subclasscheck__(cls, sub)


class FakeDate(REAL_DATE, metaclass=_DateMeta):
    @classmethod
    def today(cls):
        return now_utc().astimezone().date()                  # the local date, as date.today()


class FakeDatetime(REAL_DATETIME, metaclass=_DatetimeMeta):
    @classmethod
    def now(cls, tz=None):
        n = now_utc()
        return n.astimezone(tz) if tz is not None else n.astimezone().replace(tzinfo=None)

    @classmethod
    def today(cls):
        return cls.now()

    @classmethod
    def utcnow(cls):
        return now_utc().replace(tzinfo=None)


_patched: set[str] = set()


def _ours(name: str) -> bool:
    if name == __name__:                                      # this module keeps the real types (REAL_DATETIME, ...)
        return False
    return name == "app" or name.startswith("app.") or name == "tests" or name.startswith("tests.")


def _patch(mod) -> None:
    d = getattr(mod, "__dict__", None)
    if d is None:
        return
    for k, v in list(d.items()):
        if v is REAL_DATETIME:
            d[k] = FakeDatetime
        elif v is REAL_DATE:
            d[k] = FakeDate


def patch_loaded_modules() -> None:
    """Swap the real types for the clock's in every loaded app.* and tests.* module not patched yet."""
    for name, mod in list(sys.modules.items()):
        if name in _patched or not _ours(name):
            continue
        _patch(mod)
        _patched.add(name)


class _Loader:
    """Runs a module as its own loader would, then swaps its types (before anything can call it)."""

    def __init__(self, inner):
        self._inner = inner

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module):
        self._inner.exec_module(module)
        _patch(module)
        _patched.add(module.__name__)

    def __getattr__(self, name):                              # get_data, get_filename, ... as the real loader
        return getattr(self._inner, name)


class _Finder:
    """A meta-path finder for app.* and tests.* only: the module is found as usual, its loader wrapped."""

    @staticmethod
    def find_spec(name, path=None, target=None):
        if not _ours(name):
            return None
        from importlib.machinery import PathFinder
        spec = PathFinder.find_spec(name, path, target)
        if spec is not None and spec.loader is not None and hasattr(spec.loader, "exec_module"):
            spec.loader = _Loader(spec.loader)
        return spec


def install() -> None:
    """Swap the types in the app.* and tests.* modules loaded so far, and in every one loaded from now on (an
    import hook). The `datetime` module itself is left alone, so third-party libraries keep the real types."""
    if not any(isinstance(f, _Finder) for f in sys.meta_path):
        sys.meta_path.insert(0, _Finder())
    patch_loaded_modules()
