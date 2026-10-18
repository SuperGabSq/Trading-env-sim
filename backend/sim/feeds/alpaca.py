"""Alpaca market data: free real-time US stock and ETF trades and quotes (IEX feed).

Needs a free Alpaca account: put ALPACA_API_KEY and ALPACA_API_SECRET in .env. The free plan streams
the IEX exchange only, so prices are real time but volume is a small share (about 2-3%) of the
consolidated tape. Set ALPACA_FEED=sip if your plan includes the full feed. Orders stay in the simulator.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

from ..contracts import registry
from ..data.free_sources import ALPACA, alpaca_keys, parse_alpaca_bars
from .base import LiveAdapter


def available() -> bool:
    k, s = alpaca_keys()
    return bool(k and s)


def parse_ts(t: str) -> float:
    """RFC 3339 with up to nanoseconds, e.g. 2026-10-14T13:30:00.123456789Z."""
    t = t.rstrip("Z")
    if "." in t:
        head, frac = t.split(".", 1)
        frac = "".join(ch for ch in frac if ch.isdigit())[:6].ljust(6, "0")
        t = f"{head}.{frac}"
    return datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp()


class AlpacaFeed(LiveAdapter):
    source = "alpaca"
    label = "Alpaca US stocks and ETFs (free IEX feed, real time)"

    def __init__(self, session, symbols: list[str]) -> None:
        super().__init__(session, symbols)
        self.key, self.secret = alpaca_keys()
        self.feed = os.environ.get("ALPACA_FEED", "iex")
        self.tickers = {(registry().get(s).data or {}).get("ticker", s): s for s in symbols}

    async def backfill(self) -> None:
        import httpx

        start = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        headers = {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}
        async with httpx.AsyncClient(timeout=20) as c:
            for ticker, s in self.tickers.items():
                try:
                    rows, token = [], None
                    while True:
                        params = {"symbols": ticker, "timeframe": "1Min", "start": start, "limit": 10000,
                                  "adjustment": "split", "feed": self.feed}
                        if token:
                            params["page_token"] = token
                        r = await c.get(ALPACA, params=params, headers=headers)
                        r.raise_for_status()
                        body = r.json()
                        rows += (body.get("bars") or {}).get(ticker) or []
                        token = body.get("next_page_token")
                        if not token:
                            break
                except Exception as e:  # history is optional
                    self.detail = f"history unavailable: {e}"
                    continue
                now_min = int(time.time()) // 60 * 60
                for row in parse_alpaca_bars(rows).itertuples():
                    if int(row.ts) < now_min:
                        self.store_closed(s, {"time": int(row.ts), "open": row.open, "high": row.high,
                                              "low": row.low, "close": row.close, "volume": row.volume})
                if self.bars[s]:
                    last = self.bars[s][-1]
                    self.session.engine.on_price(s, last["time"] + 59, last["close"], False)

    async def run(self) -> None:
        import websockets

        if not (self.key and self.secret):
            self.set_status("error", "set ALPACA_API_KEY and ALPACA_API_SECRET in .env")
            return
        url = f"wss://stream.data.alpaca.markets/v2/{self.feed}"
        backoff = 1.0
        while self._running:
            try:
                self.set_status("connecting")
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_queue=4096) as ws:
                    await ws.send(json.dumps({"action": "auth", "key": self.key, "secret": self.secret}))
                    tickers = list(self.tickers)
                    await ws.send(json.dumps({"action": "subscribe", "trades": tickers, "quotes": tickers}))
                    async for raw in ws:
                        for m in json.loads(raw):
                            kind = m.get("T")
                            if kind == "t":
                                s = self.tickers.get(m["S"])
                                if s:
                                    self.on_trade(s, parse_ts(m["t"]), float(m["p"]), float(m.get("s", 0)))
                            elif kind == "q":
                                s = self.tickers.get(m["S"])
                                if s and m.get("bp") and m.get("ap"):
                                    self.on_quote(s, parse_ts(m["t"]), float(m["bp"]), float(m["ap"]))
                            elif kind == "subscription":
                                self.set_status("live", f"Alpaca {self.feed.upper()}")
                                backoff = 1.0
                            elif kind == "error":
                                code, msg = m.get("code"), m.get("msg", "")
                                if code in (401, 402, 403, 404, 409):  # auth / plan problems: retrying won't help
                                    self.set_status("error", f"Alpaca: {msg} ({code})")
                                    return
                                raise RuntimeError(f"Alpaca: {msg} ({code})")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.set_status("reconnecting", f"{type(e).__name__}: {e}"[:200])
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)
