"""PATS second entry (Mack's Price Action Trading System), ported from PATS_Second_Entry_MES.pine.

Trades real MES contracts with real tick values, per-contract fees and futures margin, instead of
the SPY-share proxy the old Alpaca runner needed.
"""

from __future__ import annotations

import math
from collections import deque

from sim.indicators import EMA
from sim.strategy import Bar, Param, Strategy


class PatsSecondEntry(Strategy):
    """Second-entry pullback in the direction of the EMA-21 trend, on 5-minute bars.

    Trend: more than half of the last `trend_bars` closes on one side of EMA 21 and the EMA sloping the
    same way over `slope_bars`. In an uptrend the setup counts a pullback leg (lower low), a push
    (higher high), a second pullback (lower low) and then enters on a strong signal bar that breaks the
    previous high. Entry is a buy stop one tick above the signal bar; stop and target are fixed ticks
    from the entry price, and the stop trails each later bar's low by `tighten_ticks`. Entries that do
    not fill on the next bar are cancelled. Max `max_trades` per day, entries 08:35-14:30 CT, flat at
    the cutoff. Shorts mirror the rules.
    """

    timeframe = "5m"
    symbol = "MES"
    warmup = 300
    params = {
        "contracts": Param(2, min=1, max=50, label="Contracts (fixed sizing)"),
        "sizing": Param("fixed", choices=["fixed", "risk"], label="Sizing mode"),
        "risk_pct": Param(1.0, min=0.1, max=5.0, step=0.1, label="Risk % of equity (risk sizing)"),
        "target_ticks": Param(6, min=1, max=100),
        "stop_ticks": Param(8, min=1, max=100),
        "tighten": Param(True, label="Trail stop bar by bar"),
        "tighten_ticks": Param(2, min=1, max=20),
        "trend_bars": Param(12, min=2, max=100),
        "slope_bars": Param(3, min=1, max=50),
        "signal_quality": Param(0.55, min=0.5, max=1.0, step=0.05, label="Signal bar close quality"),
        "require_ema_touch": Param(False),
        "enable_longs": Param(True),
        "enable_shorts": Param(True),
        "max_trades": Param(5, min=1, max=50, label="Max trades per day"),
        "session_start": Param("08:35"),
        "cutoff": Param("14:30"),
        "flatten_at_cutoff": Param(True),
    }

    def on_start(self):
        self.ema = self.indicator(EMA(21))
        self.ema_hist: deque[float] = deque(maxlen=self.p.slope_bars + 1)
        self.above: deque[bool] = deque(maxlen=self.p.trend_bars)
        self.below: deque[bool] = deque(maxlen=self.p.trend_bars)
        self.prev: Bar | None = None
        self.day: str | None = None
        self.trades_today = 0
        self._reset_long()
        self._reset_short()
        self.entry = None          # working entry order
        self.entry_bar = 0         # bar count when the entry was placed
        self.sl_order = None
        self.stop_price = None
        self.filled_bar = None
        self.n = 0
        h, m = map(int, self.p.session_start.split(":"))
        self.start_min = h * 60 + m
        h, m = map(int, self.p.cutoff.split(":"))
        self.cutoff_min = h * 60 + m

    def _reset_long(self):
        self.l_swing, self.l_phase, self.l_touch = None, 0, False

    def _reset_short(self):
        self.s_swing, self.s_phase, self.s_touch = None, 0, False

    # ---------------------------------------------------------------- signal (port of pats_engine.py)
    def signal(self, bar: Bar) -> str | None:
        p = self.p
        ema = self.ema.value
        self.ema_hist.append(ema)
        ready = len(self.ema_hist) > p.slope_bars
        rising = ready and ema > self.ema_hist[0]
        falling = ready and ema < self.ema_hist[0]
        self.above.append(bar.close > ema)
        self.below.append(bar.close < ema)
        bias_ready = len(self.above) == p.trend_bars
        up = bias_ready and sum(self.above) / p.trend_bars > 0.5 and rising
        down = bias_ready and sum(self.below) / p.trend_bars > 0.5 and falling

        prev, self.prev = self.prev, bar
        day = self.trade_date(bar)
        new_day = self.day is not None and day != self.day
        self.day = day
        if prev is None:
            return None

        touched = bar.low <= ema <= bar.high
        inside = bar.high <= prev.high and bar.low >= prev.low
        rng = bar.high - bar.low
        valid_long = rng > 0 and (bar.close - bar.low) / rng >= p.signal_quality and bar.close > bar.open
        valid_short = rng > 0 and (bar.high - bar.close) / rng >= p.signal_quality and bar.close < bar.open

        if new_day or down:
            self._reset_long()
        if new_day or up:
            self._reset_short()

        long_sig = short_sig = False
        if up and not inside:
            if self.l_swing is None or bar.high > self.l_swing:
                self.l_swing, self.l_phase, self.l_touch = bar.high, 0, False
            elif self.l_phase == 0 and bar.low < prev.low:
                self.l_phase, self.l_touch = 1, touched
            elif self.l_phase == 1:
                self.l_touch = self.l_touch or touched
                if bar.high > prev.high:
                    self.l_phase = 2
            elif self.l_phase == 2:
                self.l_touch = self.l_touch or touched
                if bar.low < prev.low:
                    self.l_phase = 3
            elif self.l_phase == 3:
                self.l_touch = self.l_touch or touched
                if valid_long and bar.high > prev.high and (not p.require_ema_touch or self.l_touch):
                    long_sig = True
                    self._reset_long()
        if down and not inside:
            if self.s_swing is None or bar.low < self.s_swing:
                self.s_swing, self.s_phase, self.s_touch = bar.low, 0, False
            elif self.s_phase == 0 and bar.high > prev.high:
                self.s_phase, self.s_touch = 1, touched
            elif self.s_phase == 1:
                self.s_touch = self.s_touch or touched
                if bar.low < prev.low:
                    self.s_phase = 2
            elif self.s_phase == 2:
                self.s_touch = self.s_touch or touched
                if bar.high > prev.high:
                    self.s_phase = 3
            elif self.s_phase == 3:
                self.s_touch = self.s_touch or touched
                if valid_short and bar.low < prev.low and (not p.require_ema_touch or self.s_touch):
                    short_sig = True
                    self._reset_short()
        if long_sig and p.enable_longs:
            return "buy"
        if short_sig and p.enable_shorts:
            return "sell"
        return None

    # ---------------------------------------------------------------- orders
    def qty(self) -> int:
        if self.p.sizing == "risk":
            risk = self.equity * self.p.risk_pct / 100
            q = max(1, math.floor(risk / (self.p.stop_ticks * self.spec.tick_value)))
            margin = self.spec.margin_per_contract(self.last_price or 0, True) or 1
            return max(1, min(q, math.floor(self.equity * 0.85 / margin)))
        return self.p.contracts

    def on_bar(self, bar: Bar):
        self.n += 1
        if self.day is not None and self.trade_date(bar) != self.day:
            self.trades_today = 0
        sig = self.signal(bar)  # always update the setup state (also during warm-up)
        if self.ctx.warming:
            return

        open_min = self.minute_of_day(bar.ts)
        close_min = open_min + self.ctx.tf_seconds // 60
        weekday = self.local_time.weekday() < 5
        in_window = weekday and open_min >= self.start_min and close_min < self.cutoff_min

        # cancel an entry that did not fill on the bar after the signal
        if self.entry is not None and self.entry.status.is_open and self.position == 0:
            if self.n > self.entry_bar or not in_window:
                self.cancel(self.entry)
                self.entry = None

        # trail the stop bar by bar after the entry bar
        pos = self.position
        if pos and self.p.tighten and self.sl_order is not None and self.sl_order.status.is_open \
                and self.filled_bar is not None and self.n > self.filled_bar:
            t = self.p.tighten_ticks * self.tick
            if pos > 0:
                new = self.round_price(bar.low - t)
                if new > self.stop_price:
                    self.stop_price = new
                    self.modify(self.sl_order, stop=new)
            else:
                new = self.round_price(bar.high + t)
                if new < self.stop_price:
                    self.stop_price = new
                    self.modify(self.sl_order, stop=new)

        if self.p.flatten_at_cutoff and pos and close_min >= self.cutoff_min and weekday:
            self.close(tag="cutoff")
            return

        can_trade = in_window and self.trades_today < self.p.max_trades and pos == 0 and \
            (self.entry is None or not self.entry.status.is_open)
        if not can_trade or sig is None:
            return
        q = self.qty()
        if sig == "buy":
            price = self.round_price(bar.high + self.tick)
            sl = price - self.p.stop_ticks * self.tick
            tp = price + self.p.target_ticks * self.tick
            self.entry = self.buy(q, stop=price, sl=sl, tp=tp, tag="pats long")
        else:
            price = self.round_price(bar.low - self.tick)
            sl = price + self.p.stop_ticks * self.tick
            tp = price - self.p.target_ticks * self.tick
            self.entry = self.sell(q, stop=price, sl=sl, tp=tp, tag="pats short")
        if self.entry is not None and self.entry.status.is_open:
            self.trades_today += 1
            self.entry_bar = self.n
            self.stop_price = sl
            self.sl_order = self.ctx.engine.orders[self.entry.child_ids[1]]

    def on_fill(self, fill):
        if self.entry is not None and fill.order_id == self.entry.id:
            self.filled_bar = self.n + 1  # fills happen while the next bar is forming
