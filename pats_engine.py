"""Stateful translation of the attached PATS second-entry signal rules."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


CHICAGO = ZoneInfo("America/Chicago")


@dataclass(frozen=True)
class Bar:
    timestamp: datetime  # bar-open time; must be timezone-aware
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("Bar timestamp must include a timezone")


@dataclass(frozen=True)
class Signal:
    side: str  # "buy" or "sell"
    bar: Bar


@dataclass(frozen=True)
class StrategyConfig:
    trend_bars: int = 12
    slope_bars: int = 3
    signal_bar_min_extreme: float = 0.55
    require_ema_touch: bool = False
    enable_longs: bool = True
    enable_shorts: bool = True
    session_start: time = time(8, 35)
    session_cutoff: time = time(14, 30)


class PatsSecondEntryEngine:
    """Calculate confirmed-bar PATS second-entry signals without broker logic."""

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or StrategyConfig()
        self._alpha = 2.0 / 22.0  # ta.ema(close, 21)
        self._ema: float | None = None
        self._ema_history: deque[float] = deque(maxlen=self.config.slope_bars + 1)
        self._above_history: deque[bool] = deque(maxlen=self.config.trend_bars)
        self._below_history: deque[bool] = deque(maxlen=self.config.trend_bars)
        self._previous_bar: Bar | None = None
        self._session_date = None

        self._long_swing_high: float | None = None
        self._long_phase = 0
        self._long_touched_ema = False
        self._short_swing_low: float | None = None
        self._short_phase = 0
        self._short_touched_ema = False

    def in_entry_window(self, bar: Bar) -> bool:
        local = bar.timestamp.astimezone(CHICAGO)
        close_time = (bar.timestamp + timedelta(minutes=5)).astimezone(CHICAGO).time()
        return (
            local.weekday() < 5
            and local.time() >= self.config.session_start
            and close_time < self.config.session_cutoff
        )

    def on_bar(self, bar: Bar) -> Signal | None:
        """Consume one new, fully closed bar and return its signal, if any."""
        if self._previous_bar and bar.timestamp <= self._previous_bar.timestamp:
            raise ValueError("Bars must be supplied once, in chronological order")

        local_date = bar.timestamp.astimezone(CHICAGO).date()
        new_day = self._session_date is not None and local_date != self._session_date
        self._session_date = local_date

        self._ema = bar.close if self._ema is None else self._alpha * bar.close + (1.0 - self._alpha) * self._ema
        self._ema_history.append(self._ema)
        ema_slope_ready = len(self._ema_history) > self.config.slope_bars
        ema_rising = ema_slope_ready and self._ema > list(self._ema_history)[-1 - self.config.slope_bars]
        ema_falling = ema_slope_ready and self._ema < list(self._ema_history)[-1 - self.config.slope_bars]

        above = bar.close > self._ema
        below = bar.close < self._ema
        self._above_history.append(above)
        self._below_history.append(below)
        bias_ready = len(self._above_history) == self.config.trend_bars
        up_bias = bias_ready and sum(self._above_history) / self.config.trend_bars > 0.50 and ema_rising
        down_bias = bias_ready and sum(self._below_history) / self.config.trend_bars > 0.50 and ema_falling

        previous = self._previous_bar
        self._previous_bar = bar
        if previous is None:
            return None

        ema_touched = bar.low <= self._ema <= bar.high
        inside = bar.high <= previous.high and bar.low >= previous.low
        candle_range = bar.high - bar.low
        valid_long = (
            candle_range > 0
            and (bar.close - bar.low) / candle_range >= self.config.signal_bar_min_extreme
            and bar.close > bar.open
        )
        valid_short = (
            candle_range > 0
            and (bar.high - bar.close) / candle_range >= self.config.signal_bar_min_extreme
            and bar.close < bar.open
        )

        if new_day or down_bias:
            self._long_swing_high = None
            self._long_phase = 0
            self._long_touched_ema = False
        if new_day or up_bias:
            self._short_swing_low = None
            self._short_phase = 0
            self._short_touched_ema = False

        long_signal = False
        short_signal = False

        if up_bias and not inside:
            if self._long_swing_high is None or bar.high > self._long_swing_high:
                self._long_swing_high = bar.high
                self._long_phase = 0
                self._long_touched_ema = False
            elif self._long_phase == 0 and bar.low < previous.low:
                self._long_phase = 1
                self._long_touched_ema = ema_touched
            elif self._long_phase == 1:
                self._long_touched_ema = self._long_touched_ema or ema_touched
                if bar.high > previous.high:
                    self._long_phase = 2
            elif self._long_phase == 2:
                self._long_touched_ema = self._long_touched_ema or ema_touched
                if bar.low < previous.low:
                    self._long_phase = 3
            elif self._long_phase == 3:
                self._long_touched_ema = self._long_touched_ema or ema_touched
                if (
                    valid_long
                    and bar.high > previous.high
                    and (not self.config.require_ema_touch or self._long_touched_ema)
                ):
                    long_signal = True
                    self._long_phase = 0
                    self._long_touched_ema = False
                    self._long_swing_high = None

        if down_bias and not inside:
            if self._short_swing_low is None or bar.low < self._short_swing_low:
                self._short_swing_low = bar.low
                self._short_phase = 0
                self._short_touched_ema = False
            elif self._short_phase == 0 and bar.high > previous.high:
                self._short_phase = 1
                self._short_touched_ema = ema_touched
            elif self._short_phase == 1:
                self._short_touched_ema = self._short_touched_ema or ema_touched
                if bar.low < previous.low:
                    self._short_phase = 2
            elif self._short_phase == 2:
                self._short_touched_ema = self._short_touched_ema or ema_touched
                if bar.high > previous.high:
                    self._short_phase = 3
            elif self._short_phase == 3:
                self._short_touched_ema = self._short_touched_ema or ema_touched
                if (
                    valid_short
                    and bar.low < previous.low
                    and (not self.config.require_ema_touch or self._short_touched_ema)
                ):
                    short_signal = True
                    self._short_phase = 0
                    self._short_touched_ema = False
                    self._short_swing_low = None

        if long_signal and self.config.enable_longs:
            return Signal("buy", bar)
        if short_signal and self.config.enable_shorts:
            return Signal("sell", bar)
        return None
