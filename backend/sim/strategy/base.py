"""Strategy API.

Write a strategy by subclassing `Strategy` in a file inside the `strategies/` folder:

    from sim.strategy import Strategy, Param
    from sim.indicators import EMA

    class EmaCross(Strategy):
        timeframe = "5m"
        params = {"fast": Param(9, min=2, max=50), "slow": Param(21, min=5, max=200)}

        def on_start(self):
            self.fast = self.indicator(EMA(self.p.fast))
            self.slow = self.indicator(EMA(self.p.slow))

        def on_bar(self, bar):
            if self.fast.value > self.slow.value and self.position <= 0:
                self.buy(1, sl_ticks=16, tp_ticks=32)

The same class runs unchanged in backtests, bar replay and live paper trading. Orders submitted in
`on_bar` (which is called when a bar CLOSES) can fill from the next price onwards.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import numpy as np

from ..contracts import ContractSpec
from ..engine import Engine, Order
from ..engine.types import Fill
from ..indicators import Indicator


@dataclass
class Param:
    default: Any
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: list | None = None
    label: str | None = None
    help: str | None = None

    def to_dict(self, name: str) -> dict:
        kind = "bool" if isinstance(self.default, bool) else "int" if isinstance(self.default, int) else \
            "float" if isinstance(self.default, float) else "choice" if self.choices else "str"
        return {"name": name, "default": self.default, "min": self.min, "max": self.max, "step": self.step,
                "choices": self.choices, "label": self.label or name.replace("_", " "), "help": self.help, "type": kind}

    def coerce(self, v: Any) -> Any:
        if isinstance(self.default, bool):
            if isinstance(v, str):
                return v.strip().lower() in ("1", "true", "yes", "on")
            return bool(v)
        if isinstance(self.default, int):
            return int(float(v))
        if isinstance(self.default, float):
            return float(v)
        return v


@dataclass
class Bar:
    ts: int          # bar open, UTC epoch seconds
    open: float
    high: float
    low: float
    close: float
    volume: float
    tf: str = ""

    @property
    def range(self) -> float:
        return self.high - self.low


class BarHistory:
    """Closed bars of the strategy timeframe; index -1 is the latest closed bar."""

    def __init__(self, maxlen: int = 5000) -> None:
        self.maxlen = maxlen
        self._bars: list[Bar] = []

    def append(self, b: Bar) -> None:
        self._bars.append(b)
        if len(self._bars) > self.maxlen * 2:
            del self._bars[: len(self._bars) - self.maxlen]

    def __len__(self) -> int:
        return len(self._bars)

    def __getitem__(self, i):
        return self._bars[i]

    def _col(self, name: str, n: int | None) -> np.ndarray:
        src = self._bars if n is None else self._bars[-n:]
        return np.fromiter((getattr(b, name) for b in src), float, len(src))

    def close(self, n: int | None = None) -> np.ndarray:
        return self._col("close", n)

    def high(self, n: int | None = None) -> np.ndarray:
        return self._col("high", n)

    def low(self, n: int | None = None) -> np.ndarray:
        return self._col("low", n)

    def open(self, n: int | None = None) -> np.ndarray:
        return self._col("open", n)

    def volume(self, n: int | None = None) -> np.ndarray:
        return self._col("volume", n)


class Strategy:
    """Base class for strategies. Override the on_* hooks you need."""

    name: str = ""
    symbol: str | None = None
    timeframe: str = "5m"
    params: dict[str, Any] = {}
    warmup: int = 200  # bars fed before the start date (indicators/state only, no trading)

    def __init__(self, ctx: "StrategyContext", params: dict | None = None) -> None:
        self.ctx = ctx
        resolved = {}
        for k, spec in self.param_specs().items():
            v = (params or {}).get(k, spec.default)
            resolved[k] = spec.coerce(v)
        self.p = SimpleNamespace(**resolved)
        self._indicators: list[Indicator] = []

    @classmethod
    def param_specs(cls) -> dict[str, Param]:
        return {k: (v if isinstance(v, Param) else Param(v)) for k, v in cls.params.items()}

    @classmethod
    def display_name(cls) -> str:
        return cls.name or cls.__name__

    # ------------------------------------------------------------------ hooks
    def on_start(self) -> None: ...
    def on_bar(self, bar: Bar) -> None: ...
    def on_fill(self, fill: Fill) -> None: ...
    def on_order(self, order: Order) -> None: ...
    def on_day_end(self, trade_date: str) -> None: ...
    def on_stop(self) -> None: ...

    # ------------------------------------------------------------------ indicators & data
    def indicator(self, ind: Indicator) -> Indicator:
        """Register an incremental indicator; it is updated with every closed bar before on_bar."""
        self._indicators.append(ind)
        return ind

    @property
    def bars(self) -> BarHistory:
        return self.ctx.history

    @property
    def spec(self) -> ContractSpec:
        return self.ctx.spec

    @property
    def tick(self) -> float:
        return self.ctx.spec.tick_size

    @property
    def time(self) -> int:
        """UTC epoch seconds of the current price (bar close time in on_bar)."""
        return self.ctx.engine.last_ts

    @property
    def local_time(self) -> datetime:
        return self.ctx.local(self.time)

    def bar_close_local(self, bar: Bar) -> datetime:
        return self.ctx.local(bar.ts + self.ctx.tf_seconds)

    def minute_of_day(self, ts: int) -> int:
        """Exchange-time minute of the day (0-1439) for a UTC timestamp."""
        return self.ctx.minute_of_day(ts)

    def in_window(self, bar: Bar, start: str, end: str) -> bool:
        """True if the bar opens at/after `start` and closes at/before `end` (exchange time, 'HH:MM')."""
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))
        o = self.minute_of_day(bar.ts)
        return o >= sh * 60 + sm and o + self.ctx.tf_seconds // 60 <= eh * 60 + em

    def trade_date(self, bar: Bar | None = None) -> str:
        """Trade date (ISO) of a bar or of the current time; CME sessions belong to the day they close."""
        return self.ctx.trade_date(bar.ts if bar else self.time)

    def round_price(self, p: float) -> float:
        return self.ctx.spec.round_price(p)

    def log(self, *msg: Any) -> None:
        self.ctx.log(" ".join(str(m) for m in msg))

    # ------------------------------------------------------------------ account
    @property
    def position(self) -> int:
        return self.ctx.position_qty()

    @property
    def entry_price(self) -> float | None:
        return self.ctx.position_avg()

    @property
    def equity(self) -> float:
        return self.ctx.engine.equity(self.ctx.account)

    @property
    def open_orders(self) -> list[Order]:
        """This strategy's working orders."""
        return [o for o in self.ctx.engine.open_orders(self.ctx.account, self.ctx.symbol) if o.id in self.ctx.my_orders]

    @property
    def last_price(self) -> float | None:
        return self.ctx.engine.last.get(self.ctx.symbol)

    # ------------------------------------------------------------------ orders
    def buy(self, qty: int = 1, price: float | None = None, stop: float | None = None, **kw) -> Order | None:
        """Market by default; `price` = limit, `stop` = stop, both = stop-limit.
        Bracket: tp/sl (prices) or tp_ticks/sl_ticks (from the fill). Other kw: tif, trail_ticks, oco, tag."""
        return self.ctx.order("buy", qty, price, stop, **kw)

    def sell(self, qty: int = 1, price: float | None = None, stop: float | None = None, **kw) -> Order | None:
        return self.ctx.order("sell", qty, price, stop, **kw)

    def close(self, tag: str = "close") -> None:
        """Cancel this strategy's orders and close the position at market."""
        self.ctx.flatten(tag)

    def cancel(self, order: Order | int) -> None:
        self.ctx.engine.cancel(order.id if isinstance(order, Order) else int(order))

    def cancel_all(self) -> None:
        """Cancel this strategy's working orders (manual orders on the same account are left alone)."""
        for o in self.open_orders:
            self.ctx.engine.cancel(o.id)

    def modify(self, order: Order | int, price: float | None = None, stop: float | None = None,
               qty: int | None = None) -> Order | None:
        oid = order.id if isinstance(order, Order) else int(order)
        o = self.ctx.engine.orders.get(oid)
        if o is None or not o.status.is_open:
            return None
        return self.ctx.engine.modify(oid, price=price, stop=stop, qty=qty)


class StrategyContext:
    """Connects one strategy instance to an engine account and a symbol."""

    def __init__(self, engine: Engine, account: str, symbol: str, spec: ContractSpec, tf_seconds: int,
                 tag: str) -> None:
        self.engine = engine
        self.account = account
        self.symbol = symbol
        self.spec = spec
        self.tf_seconds = tf_seconds
        self.tag = tag
        self.history = BarHistory()
        self.warming = False
        self.logs: list[tuple[int, str]] = []
        self.strategy: Strategy | None = None
        self.my_orders: set[int] = set()  # orders placed by this strategy (and their bracket legs)
        self._tz = spec.session.zone
        self._offsets: dict[int, int] = {}
        self._dates: dict[int, str] = {}
        self._open_s = int(spec.session.open.total_seconds())

    def local(self, ts: int) -> datetime:
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(self._tz)

    def _offset(self, ts: int) -> int:
        h = ts // 3600
        off = self._offsets.get(h)
        if off is None:
            off = int(self.local(h * 3600).utcoffset().total_seconds())
            self._offsets[h] = off
        return off

    def minute_of_day(self, ts: int) -> int:
        return ((ts + self._offset(ts)) % 86400) // 60

    def trade_date(self, ts: int) -> str:
        sess = self.spec.session
        loc = ts + self._offset(ts)
        day = loc // 86400
        if sess.overnight and (loc % 86400) >= self._open_s:
            day += 1
        s = self._dates.get(day)
        if s is None:
            s = self._dates[day] = (date(1970, 1, 1) + timedelta(days=int(day))).isoformat()
        return s

    def log(self, msg: str) -> None:
        self.logs.append((self.engine.last_ts, msg))
        if len(self.logs) > 5000:
            del self.logs[:1000]

    def position_qty(self) -> int:
        p = self.engine.accounts[self.account].positions.get(self.symbol)
        return p.qty if p else 0

    def position_avg(self) -> float | None:
        p = self.engine.accounts[self.account].positions.get(self.symbol)
        return p.avg_price if p and p.qty else None

    def order(self, side: str, qty: int, price: float | None, stop: float | None, **kw) -> Order | None:
        if self.warming:
            return None
        if price is not None and stop is not None:
            otype = "stop_limit"
        elif kw.get("trail_ticks"):
            otype = "trailing_stop"
        elif price is not None:
            otype = "limit"
        elif stop is not None:
            otype = "stop"
        else:
            otype = "market"
        kw.setdefault("tag", self.tag)
        kw.setdefault("tif", "gtc")
        o = self.engine.place(self.account, self.symbol, side, int(qty), otype, price=price, stop=stop, **kw)
        self.my_orders.add(o.id)
        self.my_orders.update(o.child_ids)
        return o

    def flatten(self, tag: str) -> None:
        if self.warming:
            return
        for o in self.engine.open_orders(self.account, self.symbol):
            if o.id in self.my_orders:
                self.engine.cancel(o.id, "strategy close")
        pos = self.position_qty()
        if pos:
            o = self.engine.place(self.account, self.symbol, "sell" if pos > 0 else "buy", abs(pos), "market",
                                  reduce_only=True, tag=tag)
            self.my_orders.add(o.id)

    def deliver(self, bar: Bar) -> None:
        """A strategy-timeframe bar closed."""
        s = self.strategy
        self.history.append(bar)
        for ind in s._indicators:
            ind.update(bar)
        s.on_bar(bar)

    def on_engine(self, kind: str, obj) -> None:
        s = self.strategy
        if s is None:
            return
        if kind == "fill" and obj.order_id in self.my_orders:
            s.on_fill(obj)
        elif kind == "order" and obj.id in self.my_orders:
            s.on_order(obj)


def finite(x: float) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


__all__ = ["Strategy", "Param", "Bar", "BarHistory", "StrategyContext", "finite", "timedelta"]
