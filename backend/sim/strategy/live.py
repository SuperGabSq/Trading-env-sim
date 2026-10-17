"""Runs a Strategy inside a replay or live session, on bars built from the session's closed minutes."""

from __future__ import annotations

import traceback
import uuid
from typing import TYPE_CHECKING

from ..contracts import registry
from ..data.bars import tf_minutes
from ..hub import tf_bucket
from .base import Bar, Strategy, StrategyContext

if TYPE_CHECKING:  # pragma: no cover
    from ..session import TradingSession


class StrategyRunner:
    def __init__(self, session: "TradingSession", cls: type[Strategy], ref: str, symbol: str, timeframe: str,
                 params: dict | None, account: str) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.session = session
        self.cls = cls
        self.ref = ref
        self.symbol = symbol.upper()
        self.timeframe = timeframe
        self.account = account
        self.spec = registry().get(self.symbol)
        self.tf_seconds = tf_minutes(timeframe) * 60
        self.ctx = StrategyContext(session.engine, account, self.symbol, self.spec, self.tf_seconds,
                                   tag=f"strategy:{cls.display_name()}")
        self.strategy = cls(self.ctx, params or {})
        self.ctx.strategy = self.strategy
        self._cur_key: int | None = None
        self.status = "running"
        self.error = ""
        self.bars_seen = 0

    def start(self, warmup: int | None = None) -> None:
        self.strategy.on_start()
        n = self.cls.warmup if warmup is None else warmup
        closed, _ = self.session.history(self.symbol, self.timeframe, max(n, 1))
        if n and len(closed):
            self.ctx.warming = True
            try:
                for i in range(len(closed)):
                    self.ctx.deliver(Bar(int(closed.ts[i]), float(closed.open[i]), float(closed.high[i]),
                                         float(closed.low[i]), float(closed.close[i]), float(closed.volume[i]),
                                         self.timeframe))
            finally:
                self.ctx.warming = False
        part = self.session.partial.get(self.symbol)
        self._cur_key = tf_bucket(self.symbol, self.timeframe, part["time"]) if part else None

    def _bucket_end(self, key: int, minute_ts: int) -> int:
        return self._range(key)[1]

    def _range(self, key: int) -> tuple[int, int]:
        """UTC range of the 1-minute bars that make up a bucket."""
        if self.tf_seconds < 86400:
            return key, key + self.tf_seconds
        from datetime import date, timedelta
        td = date(1970, 1, 1) + timedelta(days=key // 86400)
        sess = self.spec.session
        return sess.session_start(td), sess.session_end(td)

    def _base_between(self, a: int, b: int):
        from ..data.store import store
        from ..feeds.base import _concat
        if not self.session.live:
            return store().bars(self.symbol, "1m", start=a, end=b)
        mem = self.session.live.mem_bars(self.symbol)
        first = int(mem.ts[0]) if len(mem) else None
        parts = []
        if store().has(self.symbol) and (first is None or first > a):
            parts.append(store().bars(self.symbol, "1m", start=a, end=b if first is None else min(b, first)))
        parts.append(mem.between(a, b))
        return _concat(*parts)

    def _bucket_bar(self, key: int) -> dict | None:
        """Exact bar for a bucket, built from the session's closed 1-minute data."""
        a, b = self._range(key)
        sel = self._base_between(a, b)
        part = self.session.partial.get(self.symbol)
        if part is not None and not part.get("closed"):
            sel = sel.between(None, part["time"])
        if not len(sel):
            return None
        return {"time": key, "open": float(sel.open[0]), "high": float(sel.high.max()), "low": float(sel.low.min()),
                "close": float(sel.close[-1]), "volume": float(sel.volume.sum())}

    def on_base_bar(self, symbol: str, bar: dict) -> None:
        """A 1-minute bar closed in the session."""
        if symbol != self.symbol or self.status != "running":
            return
        k = tf_bucket(self.symbol, self.timeframe, bar["time"])
        if self._cur_key is not None and k != self._cur_key:
            b = self._bucket_bar(self._cur_key)
            self._cur_key = None
            if b:
                self._deliver(b)
        if bar["time"] + 60 >= self._bucket_end(k, bar["time"]):
            b = self._bucket_bar(k)
            self._cur_key = None
            if b:
                self._deliver(b)
        else:
            self._cur_key = k

    def _deliver(self, b: dict) -> None:
        try:
            self.bars_seen += 1
            self.ctx.deliver(Bar(int(b["time"]), b["open"], b["high"], b["low"], b["close"], b["volume"], self.timeframe))
        except Exception as e:
            self.status = "error"
            self.error = f"{type(e).__name__}: {e}"
            self.ctx.log(traceback.format_exc())
            self.session.engine._message(self.account, "error", f"{self.cls.display_name()} stopped: {self.error}")
            self.cancel_orders()
            self.session.broadcast_state()

    def on_engine(self, kind: str, obj) -> None:
        if self.status == "running":
            try:
                self.ctx.on_engine(kind, obj)
            except Exception as e:
                self.status = "error"
                self.error = f"{type(e).__name__}: {e}"

    def cancel_orders(self) -> None:
        for o in self.session.engine.open_orders(self.account, self.symbol):
            if o.id in self.ctx.my_orders:
                self.session.engine.cancel(o.id, "strategy stopped")

    def stop(self, flatten: bool = False) -> None:
        if self.status == "running":
            try:
                self.strategy.on_stop()
            except Exception:
                pass
        self.status = "stopped"
        self.cancel_orders()
        if flatten:
            pos = self.ctx.position_qty()
            if pos:
                self.session.engine.place(self.account, self.symbol, "sell" if pos > 0 else "buy", abs(pos), "market",
                                          reduce_only=True, tag="strategy stopped")

    def info(self) -> dict:
        return {"id": self.id, "name": self.cls.display_name(), "ref": self.ref, "symbol": self.symbol,
                "timeframe": self.timeframe, "params": vars(self.strategy.p), "status": self.status,
                "error": self.error, "account": self.account, "bars": self.bars_seen,
                "open_orders": len([o for o in self.session.engine.open_orders(self.account, self.symbol)
                                    if o.id in self.ctx.my_orders]),
                "logs": [[t, m] for t, m in self.ctx.logs[-20:]]}

    def state(self) -> dict:
        return {"ref": self.ref, "symbol": self.symbol, "timeframe": self.timeframe, "params": vars(self.strategy.p),
                "my_orders": sorted(self.ctx.my_orders), "status": self.status}
