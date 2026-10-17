"""Example: EMA crossover with ATR-based bracket. A compact template for your own strategies."""

from sim.indicators import ATR, EMA
from sim.strategy import Bar, Param, Strategy


class EmaCrossover(Strategy):
    """Go long when the fast EMA crosses above the slow EMA, short on the opposite cross.

    Each entry is a market order with a stop `stop_atr` x ATR away and a target `target_atr` x ATR away.
    Optionally trades only during regular trading hours and goes flat before the close.
    """

    timeframe = "15m"
    symbol = "MES"
    warmup = 300
    params = {
        "fast": Param(9, min=2, max=100),
        "slow": Param(21, min=3, max=300),
        "atr_len": Param(14, min=2, max=100),
        "stop_atr": Param(1.5, min=0.25, max=10, step=0.25),
        "target_atr": Param(3.0, min=0.25, max=20, step=0.25),
        "contracts": Param(1, min=1, max=50),
        "rth_only": Param(True, label="Regular trading hours only"),
        "flat_at": Param("14:55", label="Go flat at (exchange time)"),
    }

    def on_start(self):
        self.fast = self.indicator(EMA(self.p.fast))
        self.slow = self.indicator(EMA(self.p.slow))
        self.atr = self.indicator(ATR(self.p.atr_len))

    def on_bar(self, bar: Bar):
        if len(self.fast) < 2 or self.atr.value != self.atr.value:  # need two values and a valid ATR
            return
        rth_start, rth_end = self.spec.session.rth
        open_min = self.minute_of_day(bar.ts)
        close_min = open_min + self.ctx.tf_seconds // 60
        fh, fm = map(int, self.p.flat_at.split(":"))
        if self.p.rth_only:
            if self.position and close_min >= fh * 60 + fm:
                self.close(tag="session end")
                return
            if not (rth_start.total_seconds() // 60 <= open_min and close_min <= fh * 60 + fm):
                return

        crossed_up = self.fast[-2] <= self.slow[-2] and self.fast[-1] > self.slow[-1]
        crossed_dn = self.fast[-2] >= self.slow[-2] and self.fast[-1] < self.slow[-1]
        sl_ticks = max(1, round(self.p.stop_atr * self.atr.value / self.tick))
        tp_ticks = max(1, round(self.p.target_atr * self.atr.value / self.tick))

        if crossed_up and self.position <= 0:
            if self.position < 0:
                self.close(tag="reverse")
            self.buy(self.p.contracts, sl_ticks=sl_ticks, tp_ticks=tp_ticks)
        elif crossed_dn and self.position >= 0:
            if self.position > 0:
                self.close(tag="reverse")
            self.sell(self.p.contracts, sl_ticks=sl_ticks, tp_ticks=tp_ticks)
