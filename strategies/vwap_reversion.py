"""Intraday mean reversion to the session VWAP.

When price stretches more than `entry_atr` x ATR away from the session VWAP during the middle of the day,
fade it with a target at VWAP and a stop `stop_atr` x ATR beyond the entry. Max `max_trades` per day.
"""

from sim.indicators import ATR, VWAP
from sim.strategy import Bar, Param, Strategy


class VwapReversion(Strategy):
    timeframe = "5m"
    symbol = "MES"
    warmup = 100
    params = {
        "contracts": Param(1, min=1, max=50),
        "entry_atr": Param(2.0, min=0.5, max=6, step=0.25),
        "stop_atr": Param(1.5, min=0.25, max=6, step=0.25),
        "atr_len": Param(14, min=2, max=100),
        "start": Param("09:30"),
        "end": Param("13:30"),
        "flat_at": Param("14:30"),
        "max_trades": Param(2, min=1, max=20),
    }

    def on_start(self):
        self.vwap = self.indicator(VWAP(self.spec.session))
        self.atr = self.indicator(ATR(self.p.atr_len))
        self.day = None
        self.n = 0
        f = lambda s: int(s[:2]) * 60 + int(s[3:])
        self.t0, self.t1, self.tf = f(self.p.start), f(self.p.end), f(self.p.flat_at)

    def on_bar(self, bar: Bar):
        day = self.trade_date(bar)
        if day != self.day:
            self.day, self.n = day, 0
        m = self.minute_of_day(bar.ts) + self.ctx.tf_seconds // 60
        if self.position:
            if m >= self.tf:
                self.close(tag="session end")
            return
        v, a = self.vwap.value, self.atr.value
        if v != v or a != a or not (self.t0 <= m <= self.t1) or self.n >= self.p.max_trades:
            return
        dev = bar.close - v
        if abs(dev) < self.p.entry_atr * a:
            return
        stop = self.p.stop_atr * a
        if dev < 0:
            self.buy(self.p.contracts, tp=self.round_price(v), sl=self.round_price(bar.close - stop), tag="vwap long")
        else:
            self.sell(self.p.contracts, tp=self.round_price(v), sl=self.round_price(bar.close + stop), tag="vwap short")
        self.n += 1
