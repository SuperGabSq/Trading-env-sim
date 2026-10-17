"""Example: opening range breakout with OCO stop entries."""

from sim.strategy import Bar, Param, Strategy


class OpeningRangeBreakout(Strategy):
    """Builds the range of the first `range_minutes` after the cash open (08:30 CT for equity index
    futures), then places a buy stop above and a sell stop below it as an OCO pair. The stop loss is the
    other side of the range (capped at `max_stop_ticks`), the target is `target_r` times the risk.
    One trade per day; unfilled entries are cancelled and positions closed at `flat_at`.
    """

    timeframe = "1m"
    symbol = "MES"
    warmup = 0
    params = {
        "range_minutes": Param(15, min=1, max=120),
        "buffer_ticks": Param(1, min=0, max=20),
        "max_stop_ticks": Param(40, min=4, max=400),
        "target_r": Param(1.5, min=0.25, max=10, step=0.25, label="Target (multiples of risk)"),
        "contracts": Param(1, min=1, max=50),
        "flat_at": Param("14:55"),
    }

    def on_start(self):
        self.day = None
        self.hi = self.lo = None
        self.placed = False
        self.rth_open = int(self.spec.session.rth[0].total_seconds() // 60)

    def on_bar(self, bar: Bar):
        day = self.trade_date(bar)
        if day != self.day:
            self.day, self.hi, self.lo, self.placed = day, None, None, False
        m = self.minute_of_day(bar.ts)
        fh, fm = map(int, self.p.flat_at.split(":"))
        flat_min = fh * 60 + fm
        end_range = self.rth_open + self.p.range_minutes

        if m + 1 >= flat_min and m < flat_min + 5:
            if self.position or self.open_orders:
                self.close(tag="session end")
            return
        if self.rth_open <= m < end_range:
            self.hi = bar.high if self.hi is None else max(self.hi, bar.high)
            self.lo = bar.low if self.lo is None else min(self.lo, bar.low)
            if m + 1 == end_range and not self.placed and self.hi is not None:
                self.place_orders()

    def place_orders(self):
        b = self.p.buffer_ticks * self.tick
        long_entry = self.round_price(self.hi + b)
        short_entry = self.round_price(self.lo - b)
        risk = min(long_entry - short_entry, self.p.max_stop_ticks * self.tick)
        risk = max(risk, self.tick)
        group = f"orb-{self.day}"
        self.buy(self.p.contracts, stop=long_entry, sl=long_entry - risk, tp=long_entry + self.p.target_r * risk,
                 oco=group, tag="orb long")
        self.sell(self.p.contracts, stop=short_entry, sl=short_entry + risk, tp=short_entry - self.p.target_r * risk,
                  oco=group, tag="orb short")
        self.placed = True
