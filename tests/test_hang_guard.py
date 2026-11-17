"""The suite's per-test time limit (tests/conftest.py): a hung test fails instead of blocking the suite."""
import time

import pytest

from tests.conftest import TestTimeout


@pytest.mark.timeout(1)
def test_a_test_that_hangs_fails_with_testtimeout():
    with pytest.raises(TestTimeout):
        end = time.monotonic() + 30
        while time.monotonic() < end:                # a busy hang in Python code
            time.sleep(0.01)
