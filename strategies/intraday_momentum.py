"""Late-session momentum: trade the direction the market has moved since the cash open, into the close.

Based on the published "intraday momentum" effect in index futures (the first part of the day predicts the
last half hour). One trade per day: at `entry_time` go with the move since the RTH open, exit at `exit_time`.
"""

from sim.strategy import Bar, Param, Strategy


class IntradayMomentum(Strategy):
    timeframe = "5m"
    symbol = "MES"
    warmup = 0
    params = {
        "contracts": Param(1, min=1, max=50),
        "entry_time": Param("13:30", label="Enter at (exchange time)"),
        "exit_time": Param("14:00", label="Exit at (exchange time)"),
        "min_move_ticks": Param(20, min=0, max=2000, label="Min move since open (ticks)"),
        "stop_ticks": Param(0, min=0, max=500, label="Stop (ticks, 0 = none)"),
        "mode": Param("momentum", choices=["momentum", "fade"]),
    }

    def on_start(self):
        self.day = None
        self.open_px = None
        self.done = False
        self.rth_open = int(self.spec.session.rth[0].total_seconds() // 60)
        h, m = map(int, self.p.entry_time.split(":")); self.t_in = h * 60 + m
        h, m = map(int, self.p.exit_time.split(":")); self.t_out = h * 60 + m

    def on_bar(self, bar: Bar):
        day = self.trade_date(bar)
        if day != self.day:
            self.day, self.open_px, self.done = day, None, False
        m_open = self.minute_of_day(bar.ts)
        m_close = m_open + self.ctx.tf_seconds // 60
        if m_open == self.rth_open:
            self.open_px = bar.open
        if self.position and m_close >= self.t_out:
            self.close(tag="exit time")
            return
        if self.done or self.position or self.open_px is None or m_close != self.t_in:
            return
        move = (bar.close - self.open_px) / self.tick
        if abs(move) < self.p.min_move_ticks:
            return
        up = move > 0 if self.p.mode == "momentum" else move < 0
        kw = {"sl_ticks": self.p.stop_ticks} if self.p.stop_ticks else {}
        (self.buy if up else self.sell)(self.p.contracts, tag="late momentum", **kw)
        self.done = True
