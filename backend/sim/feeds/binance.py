"""Binance USD-M perpetual futures: free public trades (aggTrade) and top of book (bookTicker)."""

from __future__ import annotations

import asyncio
import json
import time

from ..contracts import registry
from ..data.importers import BINANCE_FAPI, parse_binance_klines
from .base import LiveAdapter

WS_URL = "wss://fstream.binance.com/stream?streams="


class BinanceFeed(LiveAdapter):
    source = "binance"
    label = "Binance USD-M perpetuals (free, real time)"

    async def backfill(self) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=20) as c:
            for s in self.symbols:
                try:
                    r = await c.get(f"{BINANCE_FAPI}/fapi/v1/klines", params={"symbol": s, "interval": "1m", "limit": 1000})
                    r.raise_for_status()
                    rows = r.json()
                except Exception as e:  # history is optional; trading works without it
                    self.detail = f"history unavailable: {e}"
                    continue
                now_min = int(time.time()) // 60 * 60
                df = parse_binance_klines(rows, registry().get(s).point_value)
                for row in df.itertuples():
                    if int(row.ts) < now_min:  # the current minute is still forming
                        self.store_closed(s, {"time": int(row.ts), "open": row.open, "high": row.high,
                                              "low": row.low, "close": row.close, "volume": row.volume})
                if self.bars[s]:
                    last = self.bars[s][-1]
                    self.session.engine.on_price(s, last["time"] + 59, last["close"], False)

    async def run(self) -> None:
        import websockets

        streams = "/".join(f"{s.lower()}@aggTrade/{s.lower()}@bookTicker" for s in self.symbols)
        pv = {s: registry().get(s).point_value for s in self.symbols}
        backoff = 1.0
        while self._running:
            try:
                self.set_status("connecting")
                async with websockets.connect(WS_URL + streams, ping_interval=20, ping_timeout=20,
                                              max_queue=4096) as ws:
                    self.set_status("live", "")
                    backoff = 1.0
                    async for raw in ws:
                        d = json.loads(raw).get("data", {})
                        e = d.get("e")
                        if e == "aggTrade":
                            self.on_trade(d["s"], d["T"] / 1000, float(d["p"]), float(d["q"]) / pv[d["s"]])
                        elif e == "bookTicker" or ("b" in d and "a" in d and "s" in d):
                            self.on_quote(d["s"], d.get("T", time.time() * 1000) / 1000, float(d["b"]), float(d["a"]))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.set_status("reconnecting", f"{type(e).__name__}: {e}"[:200])
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)
