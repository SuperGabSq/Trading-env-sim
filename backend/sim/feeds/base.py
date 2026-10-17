"""Live feed adapter base: turns trades/quotes from a source into session price points and keeps the
in-memory minute bars that extend stored history on the chart."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import numpy as np

from ..contracts import registry
from ..data.bars import Bars, resample
from ..data.store import store
from ..hub import hub, tf_bucket

if TYPE_CHECKING:  # pragma: no cover
    from ..session import TradingSession

MINUTE_GRACE = 2.0     # seconds after a minute ends before it is closed without a new trade
QUOTE_PUSH = 0.25      # seconds between quote updates sent to the UI


class LiveAdapter:
    source = "base"
    label = "Live feed"

    def __init__(self, session: "TradingSession", symbols: list[str]) -> None:
        self.session = session
        self.symbols = symbols
        self.status = "connecting"
        self.detail = ""
        self.bars: dict[str, list[dict]] = {s: [] for s in symbols}  # closed 1m bars (backfill + live)
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._last_quote_push: dict[str, float] = {}
        self.last_tick = 0.0

    # ---------------------------------------------------------------- lifecycle
    def info(self) -> dict:
        return {"source": self.source, "label": self.label, "status": self.status, "detail": self.detail,
                "symbols": self.symbols, "last_tick": self.last_tick}

    def set_status(self, status: str, detail: str = "") -> None:
        if (status, detail) != (self.status, self.detail):
            self.status, self.detail = status, detail
            self.session.broadcast_state()

    async def start(self) -> None:
        self._running = True
        await self.backfill()
        self._tasks.append(asyncio.create_task(self._heartbeat()))
        self._tasks.append(asyncio.create_task(self.run()))

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        await self.close()

    async def backfill(self) -> None:
        """Load recent closed minutes so charts and strategies start with context."""

    async def run(self) -> None:  # pragma: no cover - implemented by sources
        raise NotImplementedError

    async def close(self) -> None:
        pass

    # ---------------------------------------------------------------- inbound data
    def on_trade(self, symbol: str, ts: float, price: float, size: float = 0.0) -> None:
        self.last_tick = ts
        self.session.process_point(symbol, int(ts), float(price), False, float(size))

    def on_quote(self, symbol: str, ts: float, bid: float, ask: float) -> None:
        self.session.engine.on_quote(symbol, int(ts), float(bid), float(ask))
        now = time.monotonic()
        if now - self._last_quote_push.get(symbol, 0) >= QUOTE_PUSH:
            self._last_quote_push[symbol] = now
            hub.broadcast({"type": "quote", "data": {"symbol": symbol, "bid": bid, "ask": ask, "ts": ts}})

    async def _heartbeat(self) -> None:
        """Close quiet minutes on time and let the engine see the clock (session ends, DAY orders)."""
        while self._running:
            await asyncio.sleep(1.0)
            now = self.clock()
            self.session.engine.on_time(int(now))
            for s in self.symbols:
                p = self.session.partial.get(s)
                if p and not p.get("closed") and now >= p["time"] + 60 + MINUTE_GRACE:
                    self.session.close_minute(s)
            self.session.after_live_tick()

    def clock(self) -> float:
        """Current time of the feed (wall clock for real sources)."""
        return time.time()

    # ---------------------------------------------------------------- history for charts
    def store_closed(self, symbol: str, bar: dict) -> None:
        lst = self.bars.setdefault(symbol, [])
        b = {k: bar[k] for k in ("time", "open", "high", "low", "close", "volume")}
        if lst and lst[-1]["time"] == b["time"]:
            lst[-1] = b
        elif not lst or b["time"] > lst[-1]["time"]:
            lst.append(b)
        if len(lst) > 200_000:
            del lst[:50_000]

    def mem_bars(self, symbol: str) -> Bars:
        lst = self.bars.get(symbol) or []
        if not lst:
            return Bars.empty()
        a = np.array([[b["time"], b["open"], b["high"], b["low"], b["close"], b["volume"]] for b in lst], dtype=float)
        return Bars(a[:, 0].astype("int64"), a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5])

    def store_cap(self) -> int | None:
        """Stored bars at/after this time are not known yet (simulated feeds replay stored data)."""
        return None

    def history(self, symbol: str, tf: str, limit: int, partial: dict | None) -> tuple[Bars, dict | None]:
        from ..session import merge_forming
        spec = registry().get(symbol)
        mem = self.mem_bars(symbol)
        cands = [x for x in (int(mem.ts[0]) if len(mem) else None, self.store_cap()) if x is not None]
        seam = min(cands) if cands else None
        have_store = store().has(symbol)
        k = tf_bucket(symbol, tf, partial["time"]) if partial else None
        # stored bars up to the first in-memory minute (bucket aligned), then rebuilt from 1m data
        parts = []
        if have_store:
            seam_bucket = tf_bucket(symbol, tf, seam) if seam else None
            end = seam_bucket if seam_bucket is not None else k
            parts.append(store().bars(symbol, tf, end=end, limit=limit))
            if seam_bucket is not None:
                head = store().bars(symbol, "1m", start=seam_bucket, end=seam)
                mem = _concat(head, mem)
        if len(mem):
            m = mem.between(None, k) if k is not None else mem
            parts.append(resample(m, spec.session, tf) if tf != "1m" else m)
        closed = _concat(*parts).tail(limit) if parts else Bars.empty()
        if partial is None:
            # the bucket holding the current time is still filling: hand it over as the forming bar
            if len(closed) and int(closed.ts[-1]) == tf_bucket(symbol, tf, int(self.clock())):
                n = len(closed)
                last = {"time": int(closed.ts[-1]), "open": float(closed.open[-1]), "high": float(closed.high[-1]),
                        "low": float(closed.low[-1]), "close": float(closed.close[-1]), "volume": float(closed.volume[-1])}
                return closed.slice(0, n - 1), last
            return closed, None
        base = mem.between(k, partial["time"]) if len(mem) else Bars.empty()
        return closed, merge_forming(k, base, partial)


def _concat(*parts: Bars) -> Bars:
    parts = [p for p in parts if p is not None and len(p)]
    if not parts:
        return Bars.empty()
    if len(parts) == 1:
        return parts[0]
    out = Bars(
        ts=np.concatenate([p.ts for p in parts]).astype("int64"),
        open=np.concatenate([p.open for p in parts]), high=np.concatenate([p.high for p in parts]),
        low=np.concatenate([p.low for p in parts]), close=np.concatenate([p.close for p in parts]),
        volume=np.concatenate([p.volume for p in parts]))
    # keep the last value for duplicated timestamps
    _, idx = np.unique(out.ts[::-1], return_index=True)
    keep = np.sort(len(out.ts) - 1 - idx)
    return out.slice(0, len(out)) if len(keep) == len(out) else Bars(out.ts[keep], out.open[keep], out.high[keep],
                                                                       out.low[keep], out.close[keep], out.volume[keep])
