"""Incremental (strategy) and vectorised (chart) indicators must agree exactly."""

from dataclasses import dataclass
from datetime import date

import numpy as np
import pytest

from sim.contracts import registry
from sim.data.bars import Bars, resample
from sim.data.synthetic import generate
from sim.indicators import ATR, EMA, RSI, SMA, VWAP, BBands, Highest, Lowest


@dataclass
class B:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@pytest.fixture(scope="module")
def bars():
    df, _ = generate(["MES"], date(2026, 8, 1), date(2026, 8, 20))["MES"]
    b = Bars(df.ts.to_numpy(), df.open.to_numpy(), df.high.to_numpy(), df.low.to_numpy(),
             df.close.to_numpy(), df.volume.to_numpy())
    return resample(b, registry().get("MES").session, "5m")


def _incremental(ind, bars):
    rows = [B(int(t), o, h, l, c, v) for t, o, h, l, c, v in
            zip(bars.ts, bars.open, bars.high, bars.low, bars.close, bars.volume)]
    out = [ind.update(r) for r in rows]
    return out


@pytest.mark.parametrize("make", [lambda: EMA(21), lambda: SMA(20), lambda: RSI(14), lambda: ATR(14),
                                  lambda: Highest(10), lambda: Lowest(10), lambda: EMA(9, "hl2")])
def test_single_output(make, bars):
    inc = np.array(_incremental(make(), bars), dtype=float)
    vec = make().batch(bars)["value"]
    assert np.allclose(inc, vec, equal_nan=True, atol=1e-9)


def test_bbands(bars):
    inc = _incremental(BBands(20, 2), bars)
    vec = BBands(20, 2).batch(bars)
    for k in ("middle", "upper", "lower"):
        assert np.allclose([r[k] for r in inc], vec[k], equal_nan=True, atol=1e-8)


def test_vwap_resets_each_session(bars):
    s = registry().get("MES").session
    inc = np.array(_incremental(VWAP(s), bars))
    vec = VWAP(s).batch(bars)["value"]
    assert np.allclose(inc, vec)
    d = s.trade_dates_ns(bars.ts)
    first = np.flatnonzero(np.r_[True, d[1:] != d[:-1]])
    tp = (bars.high + bars.low + bars.close) / 3
    assert np.allclose(vec[first], tp[first])


def test_peek_does_not_mutate(bars):
    e = EMA(5)
    for i in range(10):
        e.update(B(int(bars.ts[i]), bars.open[i], bars.high[i], bars.low[i], bars.close[i], bars.volume[i]))
    before = e.value
    b = B(0, 1, 1, 1, 1, 1)
    p = e.peek(b)
    assert e.value == before and len(e) == 10
    assert p == pytest.approx(e.alpha * 1 + (1 - e.alpha) * before)
