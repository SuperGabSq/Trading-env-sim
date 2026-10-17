"""Simulated live feed: plays stored minute bars at real-time speed as a stream of ticks and quotes.

Use it to test the live pipeline (bots, minute closing, reconnect-free runs) at any hour, including
weekends. Each minute bar becomes ~1 tick per second along its open -> high/low -> close path, and quotes
are generated around the last price using the contract's typical spread.
"""

from __future__ import annotations

import asyncio
import math
import random
import time

from ..contracts import registry
from ..data.store import store
from .base import LiveAdapter


class SimulatedFeed(LiveAdapter):
    source = "simulated"
    label = "Simulated live (stored data at real-time speed)"

    def __init__(self, session, symbols: list[str], start_ts: int, speed: float = 1.0) -> None:
        super().__init__(session, symbols)
        self.start_ts = start_ts
        self.speed = speed
        self._t0_wall = 0.0
        self._t0_mkt = start_ts
        self._mkt_now = float(start_ts)

    def info(self) -> dict:
        return {**super().info(), "start": self.start_ts, "speed": self.speed}

    def clock(self) -> float:
        return self._mkt_now

    def store_cap(self) -> int | None:
        return self.start_ts  # later stored bars arrive through the feed, never from the store

    async def backfill(self) -> None:
        # history before the start comes straight from the store (session.history handles it)
        self.session.time = self.start_ts

    async def run(self) -> None:
        data = {s: store().load(s)[0].between(self.start_ts, None) for s in self.symbols}
        idx = {s: 0 for s in self.symbols}
        rng = random.Random(42)
        self._t0_wall = time.monotonic()
        self.set_status("live", "simulated")
        stamps = sorted({int(t) for d in data.values() for t in d.ts})
        for t in stamps:
            if not self._running:
                return
            # wait until this minute starts in simulated time
            await self._wait_until(t)
            ticks = []
            for s in self.symbols:
                d = data[s]
                j = idx[s]
                if j < len(d) and int(d.ts[j]) == t:
                    idx[s] = j + 1
                    path = self.session.engine.bar_path(s, t, float(d.open[j]), float(d.high[j]), float(d.low[j]),
                                                        float(d.close[j]))
                    ticks.extend(_ticks(s, path, float(d.volume[j]), rng))
            ticks.sort(key=lambda x: x[0])
            for ts, s, price, size in ticks:
                if not self._running:
                    return
                await self._wait_until(ts)
                spec = registry().get(s)
                half = max(1, math.ceil(spec.spread_rth / 2)) * spec.tick_size
                bid = spec.round_price(price - (half if rng.random() < 0.5 else 0))
                self.on_quote(s, ts, bid, spec.round_price(bid + max(spec.spread_rth, 1) * spec.tick_size))
                self.on_trade(s, ts, price, size)
        self.set_status("ended", "end of stored data")

    async def _wait_until(self, mkt_ts: float) -> None:
        while self._running:
            now = self._t0_mkt + (time.monotonic() - self._t0_wall) * self.speed
            self._mkt_now = max(self._mkt_now, min(now, mkt_ts))
            if now >= mkt_ts:
                self._mkt_now = mkt_ts
                return
            await asyncio.sleep(min(0.5, (mkt_ts - now) / self.speed))


def _ticks(symbol: str, path: list[tuple[int, float, bool]], volume: float, rng: random.Random):
    """Walk the bar path tick by tick (prices on the tick grid), about one print per second."""
    spec = registry().get(symbol)
    tick = spec.tick_size
    out = []
    pts = [(t, p) for t, p, _ in path]
    seg_seconds = [max(1, pts[i + 1][0] - pts[i][0]) for i in range(len(pts) - 1)]
    n_total = max(4, len(pts) - 1 + sum(int(abs(pts[i + 1][1] - pts[i][1]) / tick) for i in range(len(pts) - 1)))
    size = max(1.0, volume / n_total)
    out.append((float(pts[0][0]), symbol, pts[0][1], size))
    for i in range(len(pts) - 1):
        (t0, a), (t1, b) = pts[i], pts[i + 1]
        steps = int(round(abs(b - a) / tick))
        if steps == 0:
            out.append((t0 + seg_seconds[i] * 0.5, symbol, b, size))
            continue
        direction = 1 if b > a else -1
        for k in range(1, steps + 1):
            frac = k / steps
            jitter = rng.uniform(-0.3, 0.3) / steps
            out.append((t0 + seg_seconds[i] * min(0.999, max(0.0, frac + jitter)), symbol,
                        spec.round_price(a + direction * k * tick), size))
    return out
