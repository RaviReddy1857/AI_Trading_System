import pytest

from app.indicators import atr, breadth, daily_returns, ema, last, percentile, sma, true_range


def test_sma_known_values():
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]
    assert sma([1, 2], 3) == [None, None]


def test_ema_known_values():
    # alpha = 0.5; seed = mean(1,2,3) = 2; then 0.5*4+0.5*2 = 3; 0.5*5+0.5*3 = 4
    assert ema([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]
    out = ema([10, 10, 10, 20], 3)
    assert out[3] == pytest.approx(15.0)


def test_true_range_uses_previous_close():
    # bar 2: high-low = 2, |high - prev close| = |12-8| = 4, |low - prev close| = 2
    assert true_range([10, 12], [8, 10], [8, 11]) == [2, 4]


def test_atr_wilder_by_hand():
    highs = [10, 11, 12, 13]
    lows = [9, 10, 11, 10]
    closes = [9.5, 10.5, 11.5, 11]
    # TR = [1, 1.5, 1.5, 3]; ATR(3) first = mean(1,1.5,1.5) = 4/3; next = (4/3*2 + 3)/3 = 17/9
    out = atr(highs, lows, closes, 3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(4 / 3)
    assert out[3] == pytest.approx(17 / 9)


def test_daily_returns():
    out = daily_returns([100, 110, 99])
    assert out[0] is None
    assert out[1] == pytest.approx(10.0)
    assert out[2] == pytest.approx(-10.0)


def test_percentile_linear_interpolation():
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile(list(range(1, 11)), 80) == pytest.approx(8.2)
    assert percentile([], 80) is None
    with pytest.raises(ValueError):
        percentile([1], 101)


def test_breadth_counts_and_missing():
    closes = [100.0 + i for i in range(20)]         # average 109.5
    b = breadth({"A": (closes, 120.0), "B": (closes, 100.0), "C": (closes, None),
                 "D": (closes[:5], 120.0)}, 20)
    assert b.above == ["A"] and b.below == ["B"]
    assert b.missing == ["C", "D"]                   # no price / short history: never counted
    assert b.pct_above == 50.0 and b.counted == 2
    assert breadth({"A": (closes, None)}, 20).pct_above is None


def test_bad_period_rejected():
    with pytest.raises(ValueError):
        sma([1, 2], 0)
    assert last([]) is None
