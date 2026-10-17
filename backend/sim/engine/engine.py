"""Simulation engine: order matching, positions, P&L, fees, margin, sessions and rolls.

The engine is clock-agnostic. Market data arrives as *price points*:

* ``on_price(symbol, ts, price, continuous)`` - one point. A continuous point means price travelled
  through every level between the previous point and this one (inside a bar); a non-continuous
  point is a jump (new bar open after a gap, or a live tick).
* ``on_bar(...)`` - convenience for bar data: turns O/H/L/C into an intrabar path of four points.

Bar replay, backtests and live paper trading all feed the same method, so a strategy gets identical
fills in every mode for identical prices.

Fill rules (configurable in FillConfig):

* Market orders fill at the first price after submission, crossing the bid-ask spread: the real
  bid/ask when a live feed sends quotes, otherwise half the contract's typical spread for that time
  of day (wider overnight and in the opening minute), plus any extra slippage ticks.
* Stops trigger when price trades at/through the stop and then fill like a market order from the
  stop price, or from the gap price if price jumped through the stop.
* Limits fill at the limit price only once price trades `limit_through_ticks` through it (TradingView
  `backtest_fill_limits_assumption`); on a gap they fill at the better gap price.
* Orders only see prices that arrive after they were submitted or modified (plus latency_ms).
* With max_volume_pct, fills are capped at that share of the volume traded: large orders fill in
  parts and bracket legs grow with each partial fill.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Callable

from ..contracts import ContractRegistry, ContractSpec, registry
from .types import TIF, Fill, Order, OrderType, Position, Side, Status, Trade


QUOTE_TTL = 5  # seconds a live bid/ask quote stays usable for fills


@dataclass
class FillConfig:
    slippage_ticks: float = 0.0        # extra slippage beyond the bid-ask spread (market & stop orders, per side)
    limit_through_ticks: float = 1.0   # price must trade this far through a limit to fill it
    intrabar: str = "tv"               # 'tv' (TradingView heuristic) or 'worst' (adverse move first)
    roll_fees: bool = True             # charge both sides' fees when a position is rolled
    spread_model: bool = True          # market & stop orders cross the bid-ask spread (live quotes or contract config)
    max_volume_pct: float | None = None  # fills limited to this % of traded volume (partial fills); None = unlimited
    latency_ms: int = 0                # new and modified orders reach the market this long after they are sent


@dataclass
class RiskConfig:
    daily_loss_limit: float | None = None   # flatten + lock for the session when day P&L <= -limit
    max_position: int | None = None         # max absolute contracts per symbol
    check_margin: bool = True
    auto_liquidate: bool = True


@dataclass
class Account:
    id: str
    starting_balance: float
    balance: float
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    open_trades: dict[str, Trade] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    day_start_equity: float = 0.0
    day_fees: float = 0.0
    day_realized: float = 0.0
    trade_date: str | None = None
    locked: str = ""
    total_fees: float = 0.0
    days: list[dict] = field(default_factory=list)

    def position(self, symbol: str) -> Position:
        p = self.positions.get(symbol)
        if p is None:
            p = self.positions[symbol] = Position(symbol)
        return p


Listener = Callable[[str, dict], None]


class EngineError(Exception):
    pass


class Engine:
    def __init__(self, fill: FillConfig | None = None, risk: RiskConfig | None = None,
                 contracts: ContractRegistry | None = None, keep_fills: bool = True) -> None:
        self.reg = contracts or registry()
        self.cfg = fill or FillConfig()
        self.risk = risk or RiskConfig()
        self.keep_fills = keep_fills
        self.accounts: dict[str, Account] = {}
        self.orders: dict[int, Order] = {}
        self.working: dict[str, list[Order]] = defaultdict(list)  # open orders per symbol
        self.last: dict[str, float] = {}
        self.quotes: dict[str, tuple[int, float, float]] = {}  # symbol -> (ts, bid, ask)
        self._budget: dict[str, float] = {}     # remaining fillable volume (max_volume_pct)
        self._budget_minute: dict[str, int] = {}
        self._tod: dict[str, tuple[int, float]] = {}
        self.last_ts: int = 0
        self.listeners: list[Listener] = []
        self.hooks: dict[str, Callable[[str, object], None]] = {}  # per-account callbacks (strategies)
        self._next = {"order": 1, "fill": 1, "trade": 1}
        self._specs: dict[str, ContractSpec] = {}
        self._pos_count: dict[str, int] = defaultdict(int)
        self._minute: int | None = None
        self._sessions: dict[str, dict] = {}  # session name -> {open, date, day_margin}
        self._symbols_by_session: dict[str, set[str]] = defaultdict(set)

    # ------------------------------------------------------------------ setup / events
    def _nid(self, kind: str) -> int:
        n = self._next[kind]
        self._next[kind] = n + 1
        return n

    def spec(self, symbol: str) -> ContractSpec:
        s = self._specs.get(symbol)
        if s is None:
            s = self._specs[symbol] = self.reg.get(symbol)
            self._symbols_by_session[s.session.name].add(symbol)
        return s

    def add_account(self, account_id: str = "SIM", balance: float = 50_000.0) -> Account:
        acct = Account(id=account_id, starting_balance=balance, balance=balance, day_start_equity=balance)
        self.accounts[account_id] = acct
        self._emit("account", self.account_summary(account_id))
        return acct

    def subscribe(self, fn: Listener) -> None:
        self.listeners.append(fn)

    def _emit(self, kind: str, payload: dict) -> None:
        for fn in self.listeners:
            fn(kind, payload)

    def _message(self, account: str | None, level: str, text: str) -> None:
        if self.listeners:
            self._emit("message", {"account": account, "level": level, "text": text, "ts": self.last_ts})

    # ------------------------------------------------------------------ order commands
    def place(self, account: str, symbol: str, side: str | Side, qty: int, type: str | OrderType = "market",
              price: float | None = None, stop: float | None = None, trail_ticks: int | None = None,
              tif: str | TIF = TIF.GTC, reduce_only: bool = False, tp: float | None = None,
              sl: float | None = None, tp_ticks: float | None = None, sl_ticks: float | None = None,
              oco: str | None = None, tag: str = "manual", ts: int | None = None) -> Order:
        """Submit an order. tp/sl (prices) or tp_ticks/sl_ticks (offsets from the fill) add a bracket."""
        spec = self.spec(symbol)
        side = Side(side)
        otype = OrderType(type)
        tif = TIF(tif)
        ts = self.last_ts if ts is None else ts
        o = Order(id=self._nid('order'), account=account, symbol=spec.symbol, side=side, qty=int(qty),
                  type=otype, price=self._rp(spec, price), stop=self._rp(spec, stop),
                  trail_ticks=int(trail_ticks) if trail_ticks else None, tif=tif, reduce_only=reduce_only,
                  oco=oco, tag=tag, created_ts=ts, updated_ts=ts)
        if self.cfg.latency_ms:
            o.active_ts = ts + self.cfg.latency_ms / 1000
        self.orders[o.id] = o
        err = self._validate(o)
        if err:
            o.status, o.reason = Status.REJECTED, err
            if self.listeners:
                self._emit("order", o.to_dict())
            self._message(account, "warning", f"Order rejected: {err}")
            hook = self.hooks.get(account)
            if hook:
                hook("order", o)
            return o
        self.working[o.symbol].append(o)
        if self.listeners:
            self._emit("order", o.to_dict())
        if any(v is not None for v in (tp, sl, tp_ticks, sl_ticks)):
            self._attach_bracket(o, tp, sl, tp_ticks, sl_ticks)
        return o

    def bracket(self, account: str, symbol: str, side: str, qty: int, tp_ticks: float | None = None,
                sl_ticks: float | None = None, **kw) -> Order:
        return self.place(account, symbol, side, qty, tp_ticks=tp_ticks, sl_ticks=sl_ticks, **kw)

    def _attach_bracket(self, parent: Order, tp, sl, tp_ticks, sl_ticks) -> None:
        spec = self.spec(parent.symbol)
        ref = parent.price if parent.type == OrderType.LIMIT else parent.stop
        if ref is None:
            ref = self.last.get(parent.symbol)
        sgn = parent.side.sign
        group = f"bracket-{parent.id}"
        legs = []
        if tp is not None or tp_ticks is not None:
            px = tp if tp is not None else (ref + sgn * tp_ticks * spec.tick_size if ref is not None else None)
            legs.append(("tp", OrderType.LIMIT, px, None, tp_ticks if tp is None else None))
        if sl is not None or sl_ticks is not None:
            px = sl if sl is not None else (ref - sgn * sl_ticks * spec.tick_size if ref is not None else None)
            legs.append(("sl", OrderType.STOP, None, px, sl_ticks if sl is None else None))
        for tag, otype, price, stop, offset in legs:
            c = Order(id=self._nid('order'), account=parent.account, symbol=parent.symbol,
                      side=parent.side.opposite, qty=parent.qty, type=otype, price=self._rp(spec, price),
                      stop=self._rp(spec, stop), tif=TIF.GTC, reduce_only=True, parent_id=parent.id,
                      oco=group, tag=tag, status=Status.PENDING, created_ts=parent.created_ts,
                      updated_ts=parent.created_ts, offset_ticks=offset)
            self.orders[c.id] = c
            parent.child_ids.append(c.id)
            self.working[c.symbol].append(c)
            if self.listeners:
                self._emit("order", c.to_dict())

    def modify(self, order_id: int, price: float | None = None, stop: float | None = None,
               qty: int | None = None, trail_ticks: int | None = None) -> Order:
        o = self.orders.get(order_id)
        if o is None or not o.status.is_open:
            raise EngineError(f"Order {order_id} is not open")
        spec = self.spec(o.symbol)
        if price is not None:
            o.price = self._rp(spec, price)
        if stop is not None:
            o.stop = self._rp(spec, stop)
            if o.type == OrderType.TRAILING_STOP and o.trail_anchor is not None and o.trail_ticks:
                o.trail_anchor = o.stop + o.side.sign * o.trail_ticks * spec.tick_size
        if trail_ticks is not None:
            o.trail_ticks = int(trail_ticks)
        if qty is not None:
            if int(qty) <= 0:
                raise EngineError("Quantity must be positive")
            o.qty = int(qty)
        o.offset_ticks = None  # a moved bracket leg keeps its absolute price
        o.armed = False
        o.updated_ts = self.last_ts
        if self.cfg.latency_ms:
            o.active_ts = self.last_ts + self.cfg.latency_ms / 1000
        if self.listeners:
            self._emit("order", o.to_dict())
        return o

    def cancel(self, order_id: int, reason: str = "canceled") -> Order | None:
        o = self.orders.get(order_id)
        if o is None or not o.status.is_open:
            return o
        self._close_order(o, Status.CANCELED, reason)
        for cid in o.child_ids:
            c = self.orders[cid]
            if c.status == Status.PENDING:
                self._close_order(c, Status.CANCELED, "parent canceled")
        return o

    def cancel_all(self, account: str, symbol: str | None = None) -> int:
        n = 0
        for sym, lst in list(self.working.items()):
            if symbol and sym != symbol:
                continue
            for o in list(lst):
                if o.account == account and o.status.is_open:
                    self._close_order(o, Status.CANCELED, "cancel all")
                    n += 1
        return n

    def flatten(self, account: str, symbol: str | None = None, immediate: bool = False,
                tag: str = "flatten") -> None:
        """Cancel orders and close positions. Market exits fill at the next price unless immediate."""
        acct = self.accounts[account]
        self.cancel_all(account, symbol)
        for sym, pos in list(acct.positions.items()):
            if pos.qty == 0 or (symbol and sym != symbol):
                continue
            side = Side.SELL if pos.qty > 0 else Side.BUY
            if immediate:
                self._fill_now(acct, sym, side, abs(pos.qty), tag)
            else:
                self.place(account, sym, side, abs(pos.qty), "market", reduce_only=True, tag=tag)

    def reverse(self, account: str, symbol: str) -> None:
        pos = self.accounts[account].position(symbol)
        if pos.qty:
            side = Side.SELL if pos.qty > 0 else Side.BUY
            self.cancel_all(account, symbol)
            self.place(account, symbol, side, abs(pos.qty) * 2, "market", tag="reverse")

    # ------------------------------------------------------------------ market data
    def on_bar(self, symbol: str, ts: int, o: float, h: float, l: float, c: float, v: float = 0.0) -> None:
        if symbol not in self._specs:
            self.spec(symbol)
        if not self.working.get(symbol) and not self._pos_count.get(symbol):
            self._clock(ts + 59)  # same clock as the last point of the intrabar path
            self.last[symbol] = c
            return
        if self.cfg.max_volume_pct is not None:
            self._budget[symbol] = v * self.cfg.max_volume_pct / 100
            self._budget_minute[symbol] = ts // 60
        for t, p, cont in self.bar_path(symbol, ts, o, h, l, c):
            self.on_price(symbol, t, p, cont)

    def bar_path(self, symbol: str, ts: int, o: float, h: float, l: float, c: float) -> list[tuple[int, float, bool]]:
        if self.cfg.intrabar == "worst":
            net = sum(a.positions[symbol].qty for a in self.accounts.values() if symbol in a.positions)
            if net == 0:
                net = sum(x.side.sign for x in self.working.get(symbol, []) if x.status == Status.WORKING)
            high_first = net < 0
        else:
            high_first = (h - o) < (o - l)
        p1, p2 = (h, l) if high_first else (l, h)
        return [(ts, o, False), (ts + 15, p1, True), (ts + 30, p2, True), (ts + 59, c, True)]

    def on_price(self, symbol: str, ts: int, price: float, continuous: bool = True, volume: float = 0.0) -> None:
        if symbol not in self._specs:
            self.spec(symbol)
        self._clock(ts)
        if volume and self.cfg.max_volume_pct is not None:
            m = ts // 60
            if self._budget_minute.get(symbol) != m:
                self._budget_minute[symbol] = m
                self._budget[symbol] = 0.0
            self._budget[symbol] = self._budget.get(symbol, 0.0) + volume * self.cfg.max_volume_pct / 100
        prev = self.last.get(symbol)
        if self.working.get(symbol):
            if prev is None or not continuous:
                self._gap(symbol, price, ts)
            else:
                self._sweep(symbol, prev, price, ts)
        self.last[symbol] = price
        if self._pos_count.get(symbol):
            self._mark(symbol, price)

    def on_roll(self, symbol: str, ts: int, from_contract: str, to_contract: str, gap: float) -> None:
        """Front month changed: positions and working orders move to the new contract."""
        spec = self.spec(symbol)
        for acct in self.accounts.values():
            pos = acct.positions.get(symbol)
            if pos and pos.qty:
                fee = 0.0
                if self.cfg.roll_fees:
                    fee = 2 * spec.fee(abs(pos.qty), self.last.get(symbol, pos.avg_price))
                    acct.balance -= fee
                    acct.total_fees += fee
                    acct.day_fees += fee
                pos.avg_price += gap
                tr = acct.open_trades.get(symbol)
                if tr:
                    tr.fees += fee
                    tr._entry_value += gap * tr._entry_qty
                    tr.entry_price += gap
                self._emit("position", {"account": acct.id, **pos.to_dict()})
                self._message(acct.id, "info", f"Rolled {pos.qty:+d} {from_contract} -> {to_contract} "
                                               f"(gap {gap:+g}, fees {fee:.2f})")
        for o in self.working.get(symbol, []):
            if o.price is not None:
                o.price = spec.round_price(o.price + gap)
            if o.stop is not None:
                o.stop = spec.round_price(o.stop + gap)
            if o.trail_anchor is not None:
                o.trail_anchor += gap
            self._emit("order", o.to_dict())
        if symbol in self.last:
            self.last[symbol] += gap
        self._emit("roll", {"symbol": symbol, "ts": ts, "from": from_contract, "to": to_contract, "gap": gap})

    def on_quote(self, symbol: str, ts: int, bid: float, ask: float) -> None:
        """Top-of-book quote from a live feed: market and stop orders then fill at the real bid/ask."""
        if bid and ask and ask >= bid:
            self.quotes[symbol] = (ts, bid, ask)

    def on_time(self, ts: int) -> None:
        """Advance the clock without a price (live heartbeat) so session events fire on time."""
        self._clock(ts)

    # ------------------------------------------------------------------ matching
    def _gap(self, symbol: str, p: float, ts: int) -> None:
        blocked: set[int] = set()
        for _ in range(50):
            changed = False
            for o in list(self.working.get(symbol, [])):
                if o.status != Status.WORKING or o.id in blocked:
                    continue
                if o.active_ts and ts < o.active_ts:
                    continue  # still travelling to the exchange
                hit = self._gap_trigger(o, p)
                o.armed = True
                if hit is not None:
                    if self._execute(o, hit, ts):
                        changed = True
                    else:
                        blocked.add(o.id)
                if o.tif == TIF.IOC and o.status == Status.WORKING:
                    self._close_order(o, Status.CANCELED, "IOC not filled" if not o.filled_qty else "IOC remainder canceled")
            if not changed:
                break

    def _sweep(self, symbol: str, a: float, b: float, ts: int) -> None:
        cur = a
        blocked: set[int] = set()
        for _ in range(1000):
            best, best_dist, best_level, best_fill = None, math.inf, None, None
            for o in self.working.get(symbol, []):
                if o.status != Status.WORKING or not o.armed or o.id in blocked:
                    continue
                hit = self._cont_trigger(o, cur, b)
                if hit is None:
                    continue
                level, fill = hit
                dist = abs(level - cur)
                if dist < best_dist:
                    best, best_dist, best_level, best_fill = o, dist, level, fill
            if best is None:
                break
            cur = best_level
            if best_fill is None:  # stop-limit triggered: becomes a limit, keep sweeping
                best.triggered = True
                if self.listeners:
                    self._emit("order", best.to_dict())
                continue
            if not self._execute(best, best_fill, ts) or best.status == Status.WORKING:
                blocked.add(best.id)  # no volume left for it on this segment
        # trailing anchors follow the favourable extreme of the segment
        for o in self.working.get(symbol, []):
            if o.type == OrderType.TRAILING_STOP and o.status == Status.WORKING and o.armed:
                self._trail(o, b)
        # orders submitted/modified since the last point only see this point (as a jump)
        if any(o.status == Status.WORKING and not o.armed for o in self.working.get(symbol, [])):
            pending = [o for o in self.working[symbol] if o.status == Status.WORKING and not o.armed
                       and not (o.active_ts and ts < o.active_ts)]
            for o in pending:
                hit = self._gap_trigger(o, b)
                o.armed = True
                if hit is not None:
                    self._execute(o, hit, ts)
                if o.tif == TIF.IOC and o.status == Status.WORKING:
                    self._close_order(o, Status.CANCELED, "IOC not filled" if not o.filled_qty else "IOC remainder canceled")

    def _trail(self, o: Order, p: float) -> None:
        spec = self.spec(o.symbol)
        dist = (o.trail_ticks or 0) * spec.tick_size
        if o.side == Side.SELL:
            if o.trail_anchor is None or p > o.trail_anchor:
                o.trail_anchor = p
                new = spec.round_price(p - dist)
                if o.stop is None or new > o.stop:
                    o.stop = new
                    if self.listeners:
                        self._emit("order", o.to_dict())
        else:
            if o.trail_anchor is None or p < o.trail_anchor:
                o.trail_anchor = p
                new = spec.round_price(p + dist)
                if o.stop is None or new < o.stop:
                    o.stop = new
                    if self.listeners:
                        self._emit("order", o.to_dict())

    def _spread_ticks(self, spec: ContractSpec) -> float:
        """Quoted bid-ask spread in ticks for the current time (regular hours, overnight, opening minute)."""
        minute = self.last_ts // 60
        hit = self._tod.get(spec.session.name)
        if hit and hit[0] == minute:
            tod = hit[1]
        else:
            tod = spec.session.time_of_day(self.last_ts).total_seconds()
            self._tod[spec.session.name] = (minute, tod)
        a, b = spec.session.rth[0].total_seconds(), spec.session.rth[1].total_seconds()
        if a <= tod < a + 60 * spec.spread_open_minutes:
            return spec.spread_open
        return spec.spread_rth if a <= tod < b else spec.spread_eth

    def _taker(self, symbol: str, buy: bool, ref: float, stop_level: float | None = None) -> float:
        """Price paid by an order that takes liquidity (market, or a triggered stop)."""
        spec = self.spec(symbol)
        tick = spec.tick_size
        slip = self.cfg.slippage_ticks * tick
        q = self.quotes.get(symbol)
        if q is not None and self.last_ts - q[0] <= QUOTE_TTL:
            base = q[2] if buy else q[1]
            if stop_level is not None:
                base = max(base, stop_level) if buy else min(base, stop_level)
        else:
            half = math.ceil(self._spread_ticks(spec) / 2 - 1e-9) * tick if self.cfg.spread_model else 0.0
            base = ref + half if buy else ref - half
        return spec.round_price(base + slip if buy else base - slip)

    def _gap_trigger(self, o: Order, p: float) -> float | None:
        spec = self.spec(o.symbol)
        tick = spec.tick_size
        thr = self.cfg.limit_through_ticks * tick
        buy = o.side == Side.BUY
        t = o.type
        if t == OrderType.MARKET:
            return self._taker(o.symbol, buy, p)
        if t == OrderType.TRAILING_STOP and o.trail_anchor is None:
            self._trail(o, p)
        if t in (OrderType.STOP, OrderType.TRAILING_STOP):
            if o.stop is None:
                return None
            if (buy and p >= o.stop) or (not buy and p <= o.stop):
                return self._taker(o.symbol, buy, p, stop_level=p)
            return None
        if t == OrderType.STOP_LIMIT and not o.triggered:
            if (buy and p >= o.stop) or (not buy and p <= o.stop):
                o.triggered = True
                if self.listeners:
                    self._emit("order", o.to_dict())
            else:
                return None
        # limit (or triggered stop-limit)
        if o.price is None:
            return None
        if buy and p <= o.price - thr + 1e-9:
            return p
        if not buy and p >= o.price + thr - 1e-9:
            return p
        if thr == 0 and ((buy and p <= o.price) or (not buy and p >= o.price)):
            return p
        return None

    def _cont_trigger(self, o: Order, cur: float, b: float) -> tuple[float, float | None] | None:
        if b == cur:
            return None
        spec = self.spec(o.symbol)
        tick = spec.tick_size
        thr = self.cfg.limit_through_ticks * tick
        buy = o.side == Side.BUY
        up = b > cur
        t = o.type
        if t == OrderType.MARKET:  # remainder of a partially filled market order
            return cur, self._taker(o.symbol, buy, cur)
        if t == OrderType.LIMIT or (t == OrderType.STOP_LIMIT and o.triggered):
            if o.price is None:
                return None
            if buy:
                level = o.price - thr
                if cur <= level + 1e-9:
                    return cur, min(cur, o.price)
                if not up and b <= level + 1e-9:
                    return level, o.price
            else:
                level = o.price + thr
                if cur >= level - 1e-9:
                    return cur, max(cur, o.price)
                if up and b >= level - 1e-9:
                    return level, o.price
            return None
        if t in (OrderType.STOP, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP):
            s = o.stop
            if s is None:
                return None
            if buy:
                if cur >= s:
                    level = cur
                elif up and b >= s:
                    level = s
                else:
                    return None
                fill = self._taker(o.symbol, True, level, stop_level=level) if t != OrderType.STOP_LIMIT else None
            else:
                if cur <= s:
                    level = cur
                elif not up and b <= s:
                    level = s
                else:
                    return None
                fill = self._taker(o.symbol, False, level, stop_level=level) if t != OrderType.STOP_LIMIT else None
            if t == OrderType.STOP_LIMIT:
                return level, None
            return level, fill
        return None

    # ------------------------------------------------------------------ execution & accounting
    def _execute(self, o: Order, price: float, ts: int) -> bool:
        """Fill (part of) an order. Returns False when nothing could be filled."""
        acct = self.accounts[o.account]
        spec = self.spec(o.symbol)
        pos = acct.position(o.symbol)
        qty = o.qty - o.filled_qty
        if o.reduce_only:
            if pos.qty == 0 or (pos.qty > 0) == (o.side == Side.BUY):
                self._close_order(o, Status.CANCELED, "reduce-only: nothing to reduce")
                return False
            qty = min(qty, abs(pos.qty))
        if self.cfg.max_volume_pct is not None and o.tag != "liquidation":
            avail = int(self._budget.get(o.symbol, 0.0) + 1e-9)
            if avail <= 0:
                return False
            qty = min(qty, avail)
            self._budget[o.symbol] = self._budget.get(o.symbol, 0.0) - qty
        first_fill = o.filled_qty == 0
        fee = spec.fee(qty, price)
        realized = self._apply_fill(acct, spec, o.side, qty, price, ts, o.tag, fee)
        acct.balance += realized - fee
        acct.total_fees += fee
        acct.day_fees += fee
        acct.day_realized += realized
        o.avg_fill = price if not o.filled_qty else (o.avg_fill * o.filled_qty + price * qty) / (o.filled_qty + qty)
        o.filled_qty += qty
        o.updated_ts = ts
        if o.filled_qty >= o.qty or o.reduce_only and pos.qty == 0:
            o.status = Status.FILLED
            self._remove(o)
        fill = Fill(id=self._nid('fill'), order_id=o.id, account=acct.id, symbol=o.symbol, side=o.side,
                    qty=qty, price=price, fee=round(fee, 6), ts=ts, realized=round(realized, 6),
                    position_after=pos.qty, tag=o.tag)
        if self.keep_fills:
            acct.fills.append(fill)
        if self.listeners:
            self._emit("fill", fill.to_dict())
            self._emit("order", o.to_dict())
        hook = self.hooks.get(acct.id)
        if hook:
            hook("fill", fill)
            hook("order", o)
        if o.oco and first_fill:
            for other in list(self.working.get(o.symbol, [])):
                if other.oco == o.oco and other.id != o.id and other.status.is_open:
                    self._close_order(other, Status.CANCELED, "OCO")
        for cid in o.child_ids:
            c = self.orders[cid]
            if c.status == Status.WORKING and c.parent_id == o.id:
                c.qty = o.filled_qty  # later partial fills grow the protective legs
                continue
            if c.status != Status.PENDING:
                continue
            c.qty = o.filled_qty
            off = c.offset_ticks
            if off is not None:
                d = off * spec.tick_size
                if c.type == OrderType.LIMIT:
                    c.price = spec.round_price(price - c.side.sign * d)
                else:
                    c.stop = spec.round_price(price + c.side.sign * d)
            c.status = Status.WORKING
            c.armed = True
            c.updated_ts = ts
            if self.listeners:
                self._emit("order", c.to_dict())
        if pos.qty == 0:
            for other in list(self.working.get(o.symbol, [])):
                if other.account == acct.id and other.reduce_only and other.status == Status.WORKING:
                    self._close_order(other, Status.CANCELED, "position closed")
        if self.listeners:
            self._emit("position", {"account": acct.id, **pos.to_dict()})
            self._emit("account", self.account_summary(acct.id))
        return True

    def _fill_now(self, acct: Account, symbol: str, side: Side, qty: int, tag: str) -> None:
        spec = self.spec(symbol)
        last = self.last.get(symbol)
        if last is None:
            return
        o = Order(id=self._nid('order'), account=acct.id, symbol=symbol, side=side, qty=qty,
                  type=OrderType.MARKET, reduce_only=True, tag=tag, created_ts=self.last_ts,
                  updated_ts=self.last_ts)
        self.orders[o.id] = o
        self.working[symbol].append(o)
        if not self._execute(o, self._taker(symbol, side == Side.BUY, last), self.last_ts) and o.status.is_open:
            o.armed = True  # no volume right now: keeps working as a market order

    def _apply_fill(self, acct: Account, spec: ContractSpec, side: Side, qty: int, price: float, ts: int,
                    tag: str, fee: float) -> float:
        pos = acct.position(spec.symbol)
        s = side.sign
        realized = 0.0
        if pos.qty == 0 or (pos.qty > 0) == (s > 0):
            if pos.qty == 0:
                self._pos_count[spec.symbol] += 1
                pos.open_ts = ts
                acct.open_trades[spec.symbol] = Trade(
                    id=self._nid('trade'), account=acct.id, symbol=spec.symbol,
                    side="long" if s > 0 else "short", qty=0, entry_price=price, entry_ts=ts, entry_tag=tag)
            new = pos.qty + s * qty
            pos.avg_price = (pos.avg_price * abs(pos.qty) + price * qty) / abs(new)
            pos.qty = new
            tr = acct.open_trades[spec.symbol]
            tr._entry_qty += qty
            tr._entry_value += price * qty
            tr.entry_price = tr._entry_value / tr._entry_qty
            tr.qty = max(tr.qty, abs(new))
            tr.fees += fee
            return 0.0
        close = min(qty, abs(pos.qty))
        realized = (price - pos.avg_price) * close * (1 if pos.qty > 0 else -1) * spec.point_value
        pos.qty += s * close
        pos.realized += realized
        tr = acct.open_trades[spec.symbol]
        tr._exit_qty += close
        tr._exit_value += price * close
        tr.pnl += realized
        tr.fees += fee * close / qty
        remaining = qty - close
        if pos.qty == 0:
            self._pos_count[spec.symbol] -= 1
            tr.exit_price = tr._exit_value / tr._exit_qty
            tr.exit_ts = ts
            tr.exit_reason = tag
            tr.net = tr.pnl - tr.fees
            acct.trades.append(tr)
            del acct.open_trades[spec.symbol]
            if self.listeners:
                self._emit("trade", tr.to_dict())
            pos.avg_price = 0.0
            pos.open_ts = None
        if remaining > 0:  # flip
            self._pos_count[spec.symbol] += 1
            pos.qty = s * remaining
            pos.avg_price = price
            pos.open_ts = ts
            acct.open_trades[spec.symbol] = Trade(
                id=self._nid('trade'), account=acct.id, symbol=spec.symbol,
                side="long" if s > 0 else "short", qty=remaining, entry_price=price, entry_ts=ts,
                entry_tag=tag, fees=fee * remaining / qty, _entry_qty=remaining, _entry_value=price * remaining)
        return realized

    def _mark(self, symbol: str, price: float) -> None:
        spec = self.spec(symbol)
        for acct in self.accounts.values():
            pos = acct.positions.get(symbol)
            if not pos or pos.qty == 0:
                continue
            tr = acct.open_trades.get(symbol)
            if tr:
                exc = (price - pos.avg_price) / spec.tick_size * (1 if pos.qty > 0 else -1)
                if exc > tr.mfe_ticks:
                    tr.mfe_ticks = exc
                if exc < tr.mae_ticks:
                    tr.mae_ticks = exc
            self._risk_checks(acct)

    def _risk_checks(self, acct: Account) -> None:
        if not (self.risk.check_margin or self.risk.daily_loss_limit):
            return
        eq = self.equity(acct.id)
        if self.risk.daily_loss_limit and not acct.locked and eq - acct.day_start_equity <= -self.risk.daily_loss_limit:
            acct.locked = "daily loss limit"
            self._message(acct.id, "error", f"Daily loss limit {self.risk.daily_loss_limit:,.0f} reached: "
                                            f"positions flattened, trading locked until next session")
            self.flatten(acct.id, immediate=True, tag="loss-limit")
            return
        if self.risk.check_margin and self.risk.auto_liquidate:
            maint = self.margin(acct.id, maintenance=True)
            if maint > 0 and eq < maint:
                self._message(acct.id, "error", f"Margin call: equity {eq:,.2f} < maintenance {maint:,.2f}. "
                                                f"Positions liquidated.")
                self.flatten(acct.id, immediate=True, tag="liquidation")

    # ------------------------------------------------------------------ sessions & clock
    def _clock(self, ts: int) -> None:
        if ts > self.last_ts:
            self.last_ts = ts
        minute = ts // 60
        if minute == self._minute:
            return
        self._minute = minute
        for name, syms in list(self._symbols_by_session.items()):
            st = self._sessions.get(name)
            if st is None:
                st = self._sessions[name] = {"open": False, "date": None, "day": None, "until": 0, "from": 0}
            if st["from"] <= ts < st["until"]:
                continue  # nothing can have changed since the last check
            sess = self.spec(next(iter(syms))).session
            in_s, td, day, until = sess.state(ts)
            st["from"], st["until"] = ts, until
            if st["open"] and (not in_s or td != st["date"]):
                self._session_end(name, st["date"])
                st["open"] = False
            if in_s and not st["open"]:
                st["open"], st["date"] = True, td
                self._session_start(name, td)
            if st["day"] is True and day is False and in_s:
                self._overnight_margin_check(name)
            st["day"] = day

    def _session_end(self, name: str, td: str | None) -> None:
        syms = self._symbols_by_session[name]
        for sym in syms:
            for o in list(self.working.get(sym, [])):
                if o.tif == TIF.DAY and o.status.is_open:
                    self._close_order(o, Status.CANCELED, "DAY order expired")
        for acct in self.accounts.values():
            if acct.trade_date == td:
                continue
            eq = self.equity(acct.id)
            acct.days.append({"date": td, "equity": round(eq, 2),
                              "pnl": round(eq - acct.day_start_equity, 2),
                              "realized": round(acct.day_realized, 2), "fees": round(acct.day_fees, 2)})
            acct.trade_date = td
            if self.listeners:
                self._emit("day", {"account": acct.id, **acct.days[-1]})
        self._emit("market_session", {"session": name, "state": "closed", "date": td, "ts": self.last_ts})

    def _session_start(self, name: str, td: str) -> None:
        for acct in self.accounts.values():
            acct.day_start_equity = self.equity(acct.id)
            acct.day_fees = 0.0
            acct.day_realized = 0.0
            if acct.locked == "daily loss limit":
                acct.locked = ""
        self._emit("market_session", {"session": name, "state": "open", "date": td, "ts": self.last_ts})

    def _overnight_margin_check(self, name: str) -> None:
        if not (self.risk.check_margin and self.risk.auto_liquidate):
            return
        syms = self._symbols_by_session[name]
        for acct in self.accounts.values():
            need = self.margin(acct.id, day=False)
            eq = self.equity(acct.id)
            if need > 0 and eq < need:
                self._message(acct.id, "error", f"Overnight margin {need:,.2f} exceeds equity {eq:,.2f}: "
                                                f"positions liquidated at the end of the day-margin window.")
                for sym in syms:
                    self.flatten(acct.id, sym, immediate=True, tag="liquidation")

    # ------------------------------------------------------------------ queries
    def unrealized(self, account: str, symbol: str | None = None) -> float:
        acct = self.accounts[account]
        total = 0.0
        for sym, pos in acct.positions.items():
            if pos.qty == 0 or (symbol and sym != symbol):
                continue
            last = self.last.get(sym, pos.avg_price)
            total += (last - pos.avg_price) * pos.qty * self.spec(sym).point_value
        return total

    def equity(self, account: str) -> float:
        return self.accounts[account].balance + self.unrealized(account)

    def is_day_margin(self, symbol: str) -> bool:
        st = self._sessions.get(self.spec(symbol).session.name)
        if st is None or st["day"] is None:
            return self.spec(symbol).session.is_day_margin(self.last_ts) if self.last_ts else True
        return bool(st["day"])

    def margin(self, account: str, day: bool | None = None, maintenance: bool = False,
               extra: dict[str, int] | None = None) -> float:
        acct = self.accounts[account]
        qtys = {s: p.qty for s, p in acct.positions.items() if p.qty}
        for s, q in (extra or {}).items():
            qtys[s] = qtys.get(s, 0) + q
        total = 0.0
        for sym, q in qtys.items():
            if q == 0:
                continue
            spec = self.spec(sym)
            d = self.is_day_margin(sym) if day is None else day
            px = self.last.get(sym, 0.0)
            per = spec.maintenance_per_contract(px, d) if maintenance else spec.margin_per_contract(px, d)
            total += abs(q) * per
        return total

    def account_summary(self, account: str) -> dict:
        acct = self.accounts[account]
        eq = self.equity(account)
        used = self.margin(account)
        return {
            "id": acct.id,
            "starting_balance": acct.starting_balance,
            "balance": round(acct.balance, 2),
            "equity": round(eq, 2),
            "unrealized": round(eq - acct.balance, 2),
            "day_pnl": round(eq - acct.day_start_equity, 2),
            "day_realized": round(acct.day_realized, 2),
            "day_fees": round(acct.day_fees, 2),
            "total_fees": round(acct.total_fees, 2),
            "margin_used": round(used, 2),
            "overnight_margin": round(self.margin(account, day=False), 2),
            "buying_power": round(eq - used, 2),
            "trades": len(acct.trades),
            "locked": acct.locked,
            "ts": self.last_ts,
        }

    def open_orders(self, account: str | None = None, symbol: str | None = None) -> list[Order]:
        out = []
        for sym, lst in self.working.items():
            if symbol and sym != symbol:
                continue
            out.extend(o for o in lst if o.status.is_open and (account is None or o.account == account))
        return sorted(out, key=lambda o: o.id)

    # ------------------------------------------------------------------ helpers
    def _validate(self, o: Order) -> str:
        if o.account not in self.accounts:
            return f"unknown account {o.account}"
        acct = self.accounts[o.account]
        if o.qty <= 0:
            return "quantity must be positive"
        if o.type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and o.price is None:
            return "limit price required"
        if o.type in (OrderType.STOP, OrderType.STOP_LIMIT) and o.stop is None:
            return "stop price required"
        if o.type == OrderType.TRAILING_STOP and not o.trail_ticks:
            return "trail_ticks required"
        if o.reduce_only:
            return ""
        if acct.locked:
            return f"account locked ({acct.locked})"
        pos = acct.position(o.symbol).qty
        projected = pos + o.side.sign * o.qty
        if self.risk.max_position and abs(projected) > self.risk.max_position and abs(projected) > abs(pos):
            return f"max position {self.risk.max_position} exceeded"
        if self.risk.check_margin and abs(projected) > abs(pos):
            need = self.margin(o.account, extra={o.symbol: o.side.sign * o.qty})
            eq = self.equity(o.account)
            if need > eq:
                return f"insufficient margin (need {need:,.2f}, equity {eq:,.2f})"
        return ""

    def _rp(self, spec: ContractSpec, p: float | None) -> float | None:
        return None if p is None else spec.round_price(float(p))

    def _remove(self, o: Order) -> None:
        lst = self.working.get(o.symbol)
        if lst and o in lst:
            lst.remove(o)

    def _close_order(self, o: Order, status: Status, reason: str) -> None:
        o.status = status
        o.reason = reason
        o.updated_ts = self.last_ts
        self._remove(o)
        if self.listeners:
            self._emit("order", o.to_dict())
        hook = self.hooks.get(o.account)
        if hook:
            hook("order", o)

    # ------------------------------------------------------------------ persistence
    def snapshot(self) -> dict:
        def acct_state(a: Account) -> dict:
            return {
                "id": a.id, "starting_balance": a.starting_balance, "balance": a.balance,
                "positions": {k: v.to_dict() for k, v in a.positions.items() if v.qty},
                "trades": [asdict(t) for t in a.trades[-5000:]],
                "open_trades": {k: asdict(t) for k, t in a.open_trades.items()},
                "fills": [f.to_dict() for f in a.fills[-5000:]],
                "day_start_equity": a.day_start_equity, "day_fees": a.day_fees, "day_realized": a.day_realized,
                "trade_date": a.trade_date, "locked": a.locked, "total_fees": a.total_fees, "days": a.days[-1000:],
            }
        return {
            "accounts": [acct_state(a) for a in self.accounts.values()],
            "orders": [o.to_dict() for o in self.open_orders()],
            "last": self.last, "last_ts": self.last_ts,
            "ids": dict(self._next),
            "fill_cfg": asdict(self.cfg), "risk": asdict(self.risk),
        }

    def restore(self, state: dict) -> None:
        self.cfg = FillConfig(**state.get("fill_cfg", {}))
        self.risk = RiskConfig(**state.get("risk", {}))
        self.accounts.clear()
        for a in state["accounts"]:
            acct = Account(id=a["id"], starting_balance=a["starting_balance"], balance=a["balance"])
            for k, p in a["positions"].items():
                acct.positions[k] = Position(**p)
                self.spec(k)
                if p["qty"]:
                    self._pos_count[k] += 1
            acct.trades = [Trade(**t) for t in a["trades"]]
            acct.open_trades = {k: Trade(**t) for k, t in a["open_trades"].items()}
            acct.fills = [Fill(**{**f, "side": Side(f["side"])}) for f in a["fills"]]
            for k in ("day_start_equity", "day_fees", "day_realized", "trade_date", "locked", "total_fees", "days"):
                setattr(acct, k, a[k])
            self.accounts[acct.id] = acct
        for od in state["orders"]:
            od = {k: v for k, v in od.items() if k in Order.__dataclass_fields__}
            o = Order(**{**od, "side": Side(od["side"]), "type": OrderType(od["type"]), "tif": TIF(od["tif"]),
                         "status": Status(od["status"])})
            o.armed = False
            self.orders[o.id] = o
            self.spec(o.symbol)
            self.working[o.symbol].append(o)
        self.last = dict(state.get("last", {}))
        self.last_ts = state.get("last_ts", 0)
        self._next = dict(state.get("ids", {"order": 1, "fill": 1, "trade": 1}))
