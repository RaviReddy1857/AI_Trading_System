"""The test clock (tests/clock.py) reaches the app's clock reads, and its types behave as the real ones."""
from __future__ import annotations

import datetime as real

from tests import clock


def test_fixed_clock_reaches_the_apps_clock_reads(fixed_clock):
    from app import models
    from app.quality import IST
    now = models.utcnow()
    assert abs((now - clock.FIXED_NOW).total_seconds()) < 5                # ticking from FIXED_NOW
    assert now.astimezone(IST).date() == real.date(2026, 11, 16)
    import app.main as main
    assert abs((main.utcnow() - clock.FIXED_NOW).total_seconds()) < 5       # the name imported into another module


def test_travel_nests_and_restores():
    from app import models
    with clock.travel(real.datetime(2026, 11, 21, 18, 29, tzinfo=real.timezone.utc)):
        assert models.utcnow().date() == real.date(2026, 11, 21)
        with clock.travel(clock.FIXED_NOW):
            assert models.utcnow().date() == real.date(2026, 11, 16)
        assert models.utcnow().date() == real.date(2026, 11, 21)


def test_fake_types_are_the_real_types_for_isinstance_and_values(fixed_clock):
    from app import journal                                                 # an app module with date and datetime
    D, DT = journal.date, journal.datetime
    assert D is clock.FakeDate and DT is clock.FakeDatetime
    assert isinstance(real.date(2026, 1, 1), D) and isinstance(D(2026, 1, 1), real.date)
    assert isinstance(real.datetime(2026, 1, 1), DT) and not isinstance(real.date(2026, 1, 1), DT)
    assert D(2026, 11, 16) == real.date(2026, 11, 16) and hash(D(2026, 11, 16)) == hash(real.date(2026, 11, 16))
    assert DT.fromisoformat("2026-11-16T05:00:00+00:00") == clock.FIXED_NOW
    assert D.today() == clock.FIXED_NOW.astimezone().date()
