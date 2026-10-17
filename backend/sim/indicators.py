"""Indicators shared by the chart and by strategies.

Every indicator has two implementations that must agree (they are tested against each other):

* `batch(bars)`  vectorised, used by the chart for whole series
* `update(bar)`  incremental, used by strategies bar by bar (and `peek(bar)` for a forming bar)

Definitions follow TradingView Pine conventions (EMA seeded with the first value, RMA/RSI/ATR seeded
with a simple average, population standard deviation for Bollinger Bands, session-anchored VWAP).
"""

from __future__ import annotations

import copy
import math
from collections import deque
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .data.bars import Bars

SOURCES = ("open", "high", "low", "close", "hl2", "hlc3", "ohlc4")


def source_array(bars: Bars, source: str) -> np.ndarray:
    if source == "hl2":
        return (bars.high + bars.low) / 2
    if source == "hlc3":
        return (bars.high + bars.low + bars.close) / 3
    if source == "ohlc4":
        return (bars.open + bars.high + bars.low + bars.close) / 4
    return getattr(bars, source)


def source_value(bar, source: str) -> float:
    if source == "hl2":
        return (bar.high + bar.low) / 2
    if source == "hlc3":
        return (bar.high + bar.low + bar.close) / 3
    if source == "ohlc4":
        return (bar.open + bar.high + bar.low + bar.close) / 4
    return getattr(bar, source)


def _rma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    alpha = 1.0 / n
    v = float(np.mean(x[:n]))
    out[n - 1] = v
    for i in range(n, len(x)):
        v = alpha * x[i] + (1 - alpha) * v
        out[i] = v
    return out


class Indicator:
    """Base class. Subclasses implement `_step(bar)` (mutating) and `batch(bars)`."""

    outputs: tuple[str, ...] = ("value",)
    overlay = True  # drawn on the price pane

    def __init__(self) -> None:
        self.values: dict[str, list[float]] = {k: [] for k in self.outputs}

    # incremental API ---------------------------------------------------------------------
    def update(self, bar) -> float | dict | None:
        res = self._step(bar)
        if isinstance(res, dict):
            for k in self.outputs:
                self.values[k].append(res[k])
            return res
        self.values["value"].append(res)
        return res

    def peek(self, bar):
        """Value if `bar` closed now, without changing state (for a forming bar)."""
        return copy.deepcopy(self)._step(bar)

    @property
    def value(self) -> float:
        v = self.values[self.outputs[0]]
        return v[-1] if v else math.nan

    def __getitem__(self, i: int) -> float:
        return self.values[self.outputs[0]][i]

    def __len__(self) -> int:
        return len(self.values[self.outputs[0]])

    def _step(self, bar):  # pragma: no cover - abstract
        raise NotImplementedError

    def batch(self, bars: Bars) -> dict[str, np.ndarray]:  # pragma: no cover - abstract
        raise NotImplementedError


class EMA(Indicator):
    def __init__(self, period: int = 21, source: str = "close") -> None:
        super().__init__()
        self.period, self.source = int(period), source
        self.alpha = 2.0 / (self.period + 1)
        self._v: float | None = None

    def _step(self, bar):
        x = source_value(bar, self.source)
        self._v = x if self._v is None else self.alpha * x + (1 - self.alpha) * self._v
        return self._v

    def batch(self, bars):
        s = pd.Series(source_array(bars, self.source))
        return {"value": s.ewm(alpha=self.alpha, adjust=False).mean().to_numpy()}


class SMA(Indicator):
    def __init__(self, period: int = 20, source: str = "close") -> None:
        super().__init__()
        self.period, self.source = int(period), source
        self._win: deque[float] = deque(maxlen=self.period)
        self._sum = 0.0

    def _step(self, bar):
        x = source_value(bar, self.source)
        if len(self._win) == self.period:
            self._sum -= self._win[0]
        self._win.append(x)
        self._sum += x
        return self._sum / self.period if len(self._win) == self.period else math.nan

    def batch(self, bars):
        s = pd.Series(source_array(bars, self.source))
        return {"value": s.rolling(self.period).mean().to_numpy()}


class RSI(Indicator):
    overlay = False

    def __init__(self, period: int = 14, source: str = "close") -> None:
        super().__init__()
        self.period, self.source = int(period), source
        self._prev: float | None = None
        self._gains: list[float] = []
        self._losses: list[float] = []
        self._ag: float | None = None
        self._al: float | None = None

    def _step(self, bar):
        x = source_value(bar, self.source)
        if self._prev is None:
            self._prev = x
            return math.nan
        ch = x - self._prev
        self._prev = x
        g, l = max(ch, 0.0), max(-ch, 0.0)
        n = self.period
        if self._ag is None:
            self._gains.append(g)
            self._losses.append(l)
            if len(self._gains) < n:
                return math.nan
            self._ag, self._al = sum(self._gains) / n, sum(self._losses) / n
        else:
            self._ag = (g + (n - 1) * self._ag) / n
            self._al = (l + (n - 1) * self._al) / n
        if self._al == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + self._ag / self._al)

    def batch(self, bars):
        x = source_array(bars, self.source)
        ch = np.diff(x, prepend=np.nan)
        g = np.where(ch > 0, ch, 0.0)[1:]
        l = np.where(ch < 0, -ch, 0.0)[1:]
        ag, al = _rma(g, self.period), _rma(l, self.period)
        with np.errstate(divide="ignore", invalid="ignore"):
            rsi = np.where(al == 0, 100.0, 100.0 - 100.0 / (1.0 + ag / al))
        rsi = np.where(np.isnan(ag), np.nan, rsi)
        return {"value": np.r_[np.nan, rsi]}


class ATR(Indicator):
    overlay = False

    def __init__(self, period: int = 14) -> None:
        super().__init__()
        self.period = int(period)
        self._prev_close: float | None = None
        self._trs: list[float] = []
        self._v: float | None = None

    def _step(self, bar):
        if self._prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(bar.high - bar.low, abs(bar.high - self._prev_close), abs(bar.low - self._prev_close))
        self._prev_close = bar.close
        n = self.period
        if self._v is None:
            self._trs.append(tr)
            if len(self._trs) < n:
                return math.nan
            self._v = sum(self._trs) / n
        else:
            self._v = (tr + (n - 1) * self._v) / n
        return self._v

    def batch(self, bars):
        pc = np.r_[np.nan, bars.close[:-1]]
        tr = np.nanmax(np.vstack([bars.high - bars.low, np.abs(bars.high - pc), np.abs(bars.low - pc)]), axis=0)
        return {"value": _rma(tr, self.period)}


class BBands(Indicator):
    outputs = ("middle", "upper", "lower")

    def __init__(self, period: int = 20, mult: float = 2.0, source: str = "close") -> None:
        super().__init__()
        self.period, self.mult, self.source = int(period), float(mult), source
        self._win: deque[float] = deque(maxlen=self.period)

    def _step(self, bar):
        self._win.append(source_value(bar, self.source))
        if len(self._win) < self.period:
            return {"middle": math.nan, "upper": math.nan, "lower": math.nan}
        a = np.fromiter(self._win, float)
        m, sd = a.mean(), a.std()
        return {"middle": m, "upper": m + self.mult * sd, "lower": m - self.mult * sd}

    def batch(self, bars):
        s = pd.Series(source_array(bars, self.source))
        m = s.rolling(self.period).mean()
        sd = s.rolling(self.period).std(ddof=0)
        return {"middle": m.to_numpy(), "upper": (m + self.mult * sd).to_numpy(),
                "lower": (m - self.mult * sd).to_numpy()}


class VWAP(Indicator):
    """Session-anchored VWAP (resets at each trade date). Needs the contract's session."""

    def __init__(self, session, source: str = "hlc3") -> None:
        super().__init__()
        self.session, self.source = session, source
        self._date = None
        self._pv = 0.0
        self._v = 0.0

    def _step(self, bar):
        d = self.session.trade_date(int(bar.ts))
        if d != self._date:
            self._date, self._pv, self._v = d, 0.0, 0.0
        p = source_value(bar, self.source)
        self._pv += p * bar.volume
        self._v += bar.volume
        return self._pv / self._v if self._v > 0 else p

    def batch(self, bars):
        p = source_array(bars, self.source)
        d = self.session.trade_dates_ns(bars.ts)
        df = pd.DataFrame({"d": d, "pv": p * bars.volume, "v": bars.volume})
        g = df.groupby("d", sort=False)
        cpv, cv = g["pv"].cumsum().to_numpy(), g["v"].cumsum().to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            out = np.where(cv > 0, cpv / cv, p)
        return {"value": out}


class Highest(Indicator):
    def __init__(self, period: int = 20, source: str = "high") -> None:
        super().__init__()
        self.period, self.source = int(period), source
        self._win: deque[float] = deque(maxlen=self.period)

    def _step(self, bar):
        self._win.append(source_value(bar, self.source))
        return max(self._win) if len(self._win) == self.period else math.nan

    def batch(self, bars):
        return {"value": pd.Series(source_array(bars, self.source)).rolling(self.period).max().to_numpy()}


class Lowest(Highest):
    def __init__(self, period: int = 20, source: str = "low") -> None:
        super().__init__(period, source)

    def _step(self, bar):
        self._win.append(source_value(bar, self.source))
        return min(self._win) if len(self._win) == self.period else math.nan

    def batch(self, bars):
        return {"value": pd.Series(source_array(bars, self.source)).rolling(self.period).min().to_numpy()}


@dataclass
class IndicatorInfo:
    type: str
    label: str
    params: dict
    outputs: tuple[str, ...]
    overlay: bool


CATALOG: dict[str, IndicatorInfo] = {
    "ema": IndicatorInfo("ema", "EMA", {"period": 21, "source": "close"}, ("value",), True),
    "sma": IndicatorInfo("sma", "SMA", {"period": 50, "source": "close"}, ("value",), True),
    "vwap": IndicatorInfo("vwap", "VWAP (session)", {"source": "hlc3"}, ("value",), True),
    "bb": IndicatorInfo("bb", "Bollinger Bands", {"period": 20, "mult": 2.0, "source": "close"},
                        ("middle", "upper", "lower"), True),
    "rsi": IndicatorInfo("rsi", "RSI", {"period": 14, "source": "close"}, ("value",), False),
    "atr": IndicatorInfo("atr", "ATR", {"period": 14}, ("value",), False),
}


def make_indicator(kind: str, params: dict, session=None) -> Indicator:
    p = {k: v for k, v in (params or {}).items() if k in CATALOG[kind].params}
    if kind == "ema":
        return EMA(**p)
    if kind == "sma":
        return SMA(**p)
    if kind == "rsi":
        return RSI(**p)
    if kind == "atr":
        return ATR(**p)
    if kind == "bb":
        return BBands(**p)
    if kind == "vwap":
        return VWAP(session, **p)
    raise KeyError(kind)
