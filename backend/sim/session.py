"""Trading sessions: bar replay (historical clock) and live paper trading (wall clock).

A session owns one Engine and one paper account, receives price points from a feed, pushes them
to the engine (fills) and to the hub (charts), and exposes order commands for the UI and bots.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict
from types import SimpleNamespace

import numpy as np

from .contracts import registry
from .data.bars import Bars, tf_minutes
from .data.store import store
from .db import db
from .engine import Engine, EngineError, FillConfig, RiskConfig
from .hub import hub, tf_bucket

ACCOUNT = "SIM"
POINT_SECONDS = 15  # market time per intrabar path point (4 points per 1-minute bar)
SPEEDS = [1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600, 0]  # 0 = max


class ReplayFeed:
    """Historical base bars for one or more symbols, merged on one timeline."""

    def __init__(self, symbols: list[str], start_ts: int, end_ts: int | None = None) -> None:
        self.symbols = symbols
        self.data: dict[str, Bars] = {}
        self.rolls: dict[str, list] = {}
        stamps = []
        for s in symbols:
            bars, meta = store().load(s)
            if meta.base_tf != "1m":
                raise ValueError(f"Replay needs 1-minute data; {s} is {meta.base_tf}")
            sel = bars.between(start_ts, end_ts)
            self.data[s] = sel
            self.rolls[s] = [r for r in meta.rolls if r.ts >= start_ts]
            stamps.append(sel.ts)
        self.timeline = np.unique(np.concatenate(stamps)) if stamps else np.zeros(0, dtype="int64")
        self.ptr = {s: 0 for s in symbols}
        self.i = 0

    @property
    def done(self) -> bool:
        return self.i >= len(self.timeline)

    @property
    def next_ts(self) -> int | None:
        return None if self.done else int(self.timeline[self.i])

    def pop_minute(self) -> tuple[int, list[tuple[str, int]]]:
        """Advance one minute; returns (minute_ts, [(symbol, index_in_data)])."""
        t = int(self.timeline[self.i])
        self.i += 1
        out = []
        for s in self.symbols:
            d = self.data[s]
            j = self.ptr[s]
            if j < len(d) and int(d.ts[j]) == t:
                out.append((s, j))
                self.ptr[s] = j + 1
        return t, out


class TradingSession:
    def __init__(self, mode: str, symbols: list[str], start_ts: int | None = None, balance: float = 50_000,
                 fill: FillConfig | None = None, risk: RiskConfig | None = None, speed: int = 60) -> None:
        self.id = uuid.uuid4().hex[:10]
        self.mode = mode
        self.symbols = [s.upper() for s in symbols]
        for s in self.symbols:
            registry().get(s)
        self.engine = Engine(fill or FillConfig(), risk or RiskConfig())
        self.engine.add_account(ACCOUNT, balance)
        self.engine.subscribe(self._on_engine_event)
        self.engine.hooks[ACCOUNT] = self._strategy_hook
        self.balance = balance
        self.state = "paused"
        self.speed = speed
        self.start_ts = start_ts
        self.time: int | None = None
        self.partial: dict[str, dict] = {}
        self.feed: ReplayFeed | None = None
        self._task: asyncio.Task | None = None
        self._events: list[tuple] = []
        self._seq = 0
        self._last_account_push = 0.0
        self.created = time.time()
        self.strategies: list = []  # StrategyRunner instances trading this session's account
        self.live = None            # LiveAdapter for live sessions
        self._last_save = 0.0
        if mode == "replay":
            self.feed = ReplayFeed(self.symbols, start_ts)
            for s in self.symbols:  # prime last prices so the first market order has a reference
                bars, _ = store().load(s)
                prev = bars.between(None, start_ts)
                if len(prev):
                    self.engine.on_price(s, int(prev.ts[-1]) + 59, float(prev.close[-1]), False)
            self.time = start_ts

    # ------------------------------------------------------------------ state
    def state_dict(self) -> dict:
        return {
            "id": self.id, "mode": self.mode, "symbols": self.symbols, "state": self.state,
            "speed": self.speed, "time": self.time, "start": self.start_ts,
            "next": self.feed.next_ts if self.feed else None,
            "end": int(self.feed.timeline[-1]) if self.feed is not None and len(self.feed.timeline) else None,
            "fill": asdict(self.engine.cfg), "risk": asdict(self.engine.risk),
            "account": self.engine.account_summary(ACCOUNT),
            "live": self.live.info() if self.live else None,
            "strategies": [r.info() for r in self.strategies],
        }

    def trading_state(self) -> dict:
        acct = self.engine.accounts[ACCOUNT]
        return {
            "account": self.engine.account_summary(ACCOUNT),
            "positions": [{"account": ACCOUNT, **p.to_dict(),
                           "unrealized": round(self.engine.unrealized(ACCOUNT, s), 2),
                           "last": self.engine.last.get(s)}
                          for s, p in acct.positions.items() if p.qty],
            "orders": [o.to_dict() for o in self.engine.open_orders(ACCOUNT)],
            "fills": [f.to_dict() for f in acct.fills[-300:]],
            "trades": [t.to_dict() for t in acct.trades[-500:]],
            "days": acct.days[-60:],
            "last": dict(self.engine.last),
        }

    def broadcast_state(self) -> None:
        hub.broadcast({"type": "session", "data": self.state_dict()})

    # ------------------------------------------------------------------ chart history (no look-ahead)
    def history(self, symbol: str, tf: str, limit: int) -> tuple[Bars, dict | None]:
        """Closed bars + the forming bar, using only data the session clock has reached."""
        if self.live:
            return self.live.history(symbol, tf, limit, self.partial.get(symbol))
        part = self.partial.get(symbol)
        if part is None:  # nothing replayed yet: the bucket holding the start is still forming
            k = tf_bucket(symbol, tf, self.start_ts)
            closed = store().bars(symbol, tf, end=k, limit=limit)
            base = store().bars(symbol, "1m", start=k, end=self.start_ts)
            if not len(base):
                return closed, None
            return closed, {"time": k, "open": float(base.open[0]), "high": float(base.high.max()),
                            "low": float(base.low.min()), "close": float(base.close[-1]),
                            "volume": float(base.volume.sum())}
        k = tf_bucket(symbol, tf, part["time"])
        closed = store().bars(symbol, tf, end=k, limit=limit)
        base = store().bars(symbol, "1m", start=k, end=part["time"])
        return closed, merge_forming(k, base, part)

    # ------------------------------------------------------------------ feeding prices
    def process_point(self, symbol: str, ts: int, price: float, continuous: bool, volume: float) -> None:
        minute = ts - ts % 60
        p = self.partial.get(symbol)
        if p is not None and p["time"] != minute and not p.get("closed"):
            self.close_minute(symbol)  # a trade in a new minute closes the previous one
        self.engine.on_price(symbol, ts, price, continuous, volume)
        hub.on_point(symbol, ts, price, volume)
        p = self.partial.get(symbol)
        if p is None or p["time"] != minute:
            self.partial[symbol] = {"time": minute, "open": price, "high": price, "low": price,
                                    "close": price, "volume": volume}
        else:
            p["high"] = max(p["high"], price)
            p["low"] = min(p["low"], price)
            p["close"] = price
            p["volume"] += volume
        if self.time is None or ts > self.time:
            self.time = ts

    def close_minute(self, symbol: str) -> None:
        """The current minute bar of `symbol` is complete: store it and let strategies see it."""
        p = self.partial.get(symbol)
        if p is None or p.get("closed"):
            return
        p["closed"] = True
        self._on_minute_closed(symbol, p)

    def _on_minute_closed(self, symbol: str, bar: dict) -> None:
        if self.live:
            self.live.store_closed(symbol, bar)
        for r in list(self.strategies):
            r.on_base_bar(symbol, bar)

    def _strategy_hook(self, kind: str, obj) -> None:
        for r in self.strategies:
            r.on_engine(kind, obj)

    def _replay_minute(self) -> int:
        """Process one replay minute (4 path points per symbol). Returns points processed."""
        feed = self.feed
        t, items = feed.pop_minute()
        n = 0
        for s, j in items:
            for r in list(feed.rolls[s]):
                if r.ts <= t:
                    self.engine.on_roll(s, t, r.from_contract, r.to_contract, r.gap)
                    feed.rolls[s].remove(r)
        paths = []
        for s, j in items:
            d = feed.data[s]
            pts = self.engine.bar_path(s, t, float(d.open[j]), float(d.high[j]), float(d.low[j]), float(d.close[j]))
            paths.append((s, pts, float(d.volume[j]) / 4))
        for k in range(4):
            for s, pts, v in paths:
                ts, price, cont = pts[k]
                self.process_point(s, ts, price, cont, v)
                n += 1
        for s, _ in items:  # the bar is complete: strategies act before the next minute opens
            self.close_minute(s)
        return n

    # ------------------------------------------------------------------ replay controls
    async def play(self) -> None:
        if self.mode != "replay" or self.state == "playing" or self.feed.done:
            return
        self.state = "playing"
        self.broadcast_state()
        self._task = asyncio.create_task(self._run())

    def pause(self) -> None:
        if self.state == "playing":
            self.state = "paused"
            if self._task:
                self._task.cancel()
            self.flush_events()
            self.broadcast_state()

    def set_speed(self, speed: int) -> None:
        self.speed = int(speed)
        self.broadcast_state()

    def step(self, tf: str | None = None, count: int = 1) -> None:
        if self.mode != "replay" or self.feed.done:
            return
        for _ in range(max(1, count)):
            if self.feed.done:
                break
            if tf and tf_minutes(tf) > 1:
                sym = self.symbols[0]
                first = self.feed.next_ts
                k = tf_bucket(sym, tf, first)
                while not self.feed.done and tf_bucket(sym, tf, self.feed.next_ts) == k:
                    self._replay_minute()
            else:
                self._replay_minute()
        self._after_batch(force=True)

    def jump(self, ts: int) -> None:
        """Fast-forward (orders keep working) until the replay clock reaches ts."""
        if self.mode != "replay":
            return
        while not self.feed.done and self.feed.next_ts < ts:
            self._replay_minute()
        self._after_batch(force=True)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        budget = 0.0
        try:
            while self.state == "playing" and not self.feed.done:
                now = loop.time()
                if self.speed == 0:
                    for _ in range(120):
                        if self.feed.done or self.state != "playing":
                            break
                        self._replay_minute()
                    self._after_batch()
                    await asyncio.sleep(0.005)
                    continue
                budget += (now - last) * self.speed
                last = now
                minutes = 0
                while budget >= 60 and not self.feed.done and minutes < 600:
                    self._replay_minute()
                    budget -= 60
                    minutes += 1
                if minutes:
                    self._after_batch()
                wait = max(0.01, min(0.25, (60 - budget) / max(self.speed, 1e-9)))
                await asyncio.sleep(wait)
            if self.feed.done:
                self.state = "ended"
                self._after_batch(force=True)
        except asyncio.CancelledError:
            pass

    def _after_batch(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_account_push > 0.25:
            self._last_account_push = now
            hub.broadcast({"type": "account", "data": self.engine.account_summary(ACCOUNT)})
            hub.broadcast({"type": "clock", "data": {"time": self.time, "state": self.state,
                                                     "next": self.feed.next_ts if self.feed else None,
                                                     "last": self.engine.last}})
        if force:
            self.broadcast_state()
        if len(self._events) > 500:
            self.flush_events()

    async def stop(self) -> None:
        self.pause()
        if self.live:
            await self.live.stop()
        for r in self.strategies:
            r.stop()
        self.state = "stopped"
        self.flush_events()
        db().set(f"session:{self.id}:summary", {"state": self.state_dict(), "trading": self.trading_state()})
        if self.mode == "live":
            db().delete("session:live")

    # ------------------------------------------------------------------ live sessions
    async def start_live(self, adapter) -> None:
        self.live = adapter
        self.state = "running"
        await adapter.start()
        self.save()

    def after_live_tick(self) -> None:
        self._after_batch()
        if time.monotonic() - self._last_save > 10:
            self.save()

    def to_state(self) -> dict:
        return {"id": self.id, "mode": self.mode, "symbols": self.symbols, "balance": self.balance,
                "source": self.live.source if self.live else None,
                "start": getattr(self.live, "start_ts", None), "engine": self.engine.snapshot(),
                "strategies": [r.state() for r in self.strategies if r.status == "running"]}

    def save(self) -> None:
        """Persist a live session so a restart resumes the account, positions and working orders."""
        if self.mode != "live":
            return
        self._last_save = time.monotonic()
        try:
            db().set("session:live", self.to_state())
        except Exception:
            pass

    # ------------------------------------------------------------------ strategies in the session
    def attach_strategy(self, ref: str, symbol: str | None = None, timeframe: str | None = None,
                        params: dict | None = None, my_orders: list[int] | None = None, warmup: int | None = None):
        from .strategy.live import StrategyRunner
        from .strategy.runner import load_strategy
        cls = load_strategy(ref)
        sym = (symbol or cls.symbol or self.symbols[0]).upper()
        if sym not in self.symbols:
            raise ValueError(f"{sym} is not part of this session ({', '.join(self.symbols)})")
        r = StrategyRunner(self, cls, ref, sym, timeframe or cls.timeframe, params, ACCOUNT)
        if my_orders:
            r.ctx.my_orders.update(my_orders)
        r.start(warmup)
        self.strategies.append(r)
        self.broadcast_state()
        self.save()
        return r

    def detach_strategy(self, rid: str, flatten: bool = False) -> None:
        for r in list(self.strategies):
            if r.id == rid:
                r.stop(flatten)
                self.strategies.remove(r)
        self.broadcast_state()
        self.save()

    # ------------------------------------------------------------------ orders
    def place(self, **kw):
        kw.setdefault("tag", "manual")
        return self.engine.place(ACCOUNT, **kw)

    def modify(self, order_id: int, **kw):
        return self.engine.modify(order_id, **kw)

    def cancel(self, order_id: int):
        return self.engine.cancel(order_id)

    def cancel_all(self, symbol: str | None = None) -> int:
        return self.engine.cancel_all(ACCOUNT, symbol)

    def flatten(self, symbol: str | None = None) -> None:
        self.engine.flatten(ACCOUNT, symbol)

    def reverse(self, symbol: str) -> None:
        self.engine.reverse(ACCOUNT, symbol)

    # ------------------------------------------------------------------ events
    def _on_engine_event(self, kind: str, payload: dict) -> None:
        hub.broadcast({"type": kind, "data": payload})
        self._seq += 1
        self._events.append((self.id, self._seq, self.engine.last_ts, kind, _dumps(payload)))

    def flush_events(self) -> None:
        if not self._events:
            return
        rows, self._events = self._events, []
        try:
            db().executemany("INSERT OR REPLACE INTO events(session, seq, ts, type, data) VALUES(?,?,?,?,?)", rows)
        except Exception:
            pass


def _dumps(o) -> str:
    import json
    return json.dumps(o, separators=(",", ":"), default=str)


def merge_forming(bucket: int, base: Bars, part: dict) -> dict:
    """Forming bar of a timeframe bucket from its closed base bars plus the partial minute."""
    forming = dict(part)
    forming["time"] = bucket
    if len(base):
        forming["open"] = float(base.open[0])
        forming["high"] = max(float(base.high.max()), part["high"])
        forming["low"] = min(float(base.low.min()), part["low"])
        forming["volume"] = float(base.volume.sum()) + part["volume"]
    return forming


class SessionManager:
    def __init__(self) -> None:
        self.current: TradingSession | None = None

    async def start(self, session: TradingSession) -> TradingSession:
        if self.current:
            await self.current.stop()
        self.current = session
        session.broadcast_state()
        hub.broadcast({"type": "state", "data": session.trading_state()})
        return session

    async def stop(self) -> None:
        if self.current:
            await self.current.stop()
            old = self.current
            self.current = None
            hub.broadcast({"type": "session", "data": None})
            return old

    def require(self) -> TradingSession:
        if not self.current:
            raise EngineError("No active session. Start a replay or live session first.")
        return self.current


manager = SessionManager()
hub.session_provider = lambda: manager.current
