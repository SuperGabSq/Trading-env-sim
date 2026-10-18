"""Intraday trend breakout (Donchian channel) on 15-minute bars with an EMA trend filter.

Buy when the close breaks the highest high of the last `channel` bars while above the EMA (sell for lows
below it). Stop `stop_atr` x ATR, target `target_atr` x ATR, flat before the close, one position at a time.
"""

from sim.indicators import ATR, EMA, Highest, Lowest
from sim.strategy import Bar, Param, Strategy


class TrendBreakout(Strategy):
    timeframe = "15m"
    symbol = "MES"
    warmup = 300
    params = {
        "contracts": Param(1, min=1, max=50),
        "channel": Param(20, min=5, max=200),
        "ema": Param(50, min=5, max=300),
        "atr_len": Param(14, min=2, max=100),
        "stop_atr": Param(1.5, min=0.25, max=10, step=0.25),
        "target_atr": Param(4.0, min=0.5, max=20, step=0.5),
        "start": Param("09:00"),
        "flat_at": Param("14:30"),
    }

    def on_start(self):
        self.hi = self.indicator(Highest(self.p.channel))
        self.lo = self.indicator(Lowest(self.p.channel))
        self.ema = self.indicator(EMA(self.p.ema))
        self.atr = self.indicator(ATR(self.p.atr_len))
        f = lambda s: int(s[:2]) * 60 + int(s[3:])
        self.t0, self.tf = f(self.p.start), f(self.p.flat_at)

    def on_bar(self, bar: Bar):
        m = self.minute_of_day(bar.ts) + self.ctx.tf_seconds // 60
        if self.position:
            if m >= self.tf:
                self.close(tag="session end")
            return
        if len(self.hi) < 2 or self.atr.value != self.atr.value or not (self.t0 <= m < self.tf):
            return
        prev_hi, prev_lo, a = self.hi[-2], self.lo[-2], self.atr.value
        sl = max(1, round(self.p.stop_atr * a / self.tick)); tp = max(1, round(self.p.target_atr * a / self.tick))
        if bar.close > prev_hi and bar.close > self.ema.value:
            self.buy(self.p.contracts, sl_ticks=sl, tp_ticks=tp, tag="breakout long")
        elif bar.close < prev_lo and bar.close < self.ema.value:
            self.sell(self.p.contracts, sl_ticks=sl, tp_ticks=tp, tag="breakout short")
