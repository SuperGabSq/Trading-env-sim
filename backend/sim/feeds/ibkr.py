"""Interactive Brokers: live trades and top of book through TWS or IB Gateway (ib_async) for CME
futures, US stocks and ETFs, and forex/spot gold (IDEALPRO quotes; the mid price is used as the trade).

Requirements: `pip install ib_async`, TWS or IB Gateway running with API access enabled, and the CME
market-data subscription on your IBKR account (CME top of book is about $1.25/month for
non-professionals). Set IBKR_HOST / IBKR_PORT / IBKR_CLIENT_ID in .env (paper TWS uses port 7497,
IB Gateway paper 4002). Orders are never sent to IBKR: fills happen in the local simulator.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timezone

from ..contracts import registry
from .base import LiveAdapter

EXCHANGES = {"CME": "CME", "CBOT": "CBOT", "COMEX": "COMEX", "NYMEX": "NYMEX"}


def available() -> bool:
    try:
        import ib_async  # noqa: F401
        return True
    except ImportError:
        return False


class IBKRFeed(LiveAdapter):
    source = "ibkr"
    label = "Interactive Brokers (CME, needs TWS / IB Gateway)"

    def __init__(self, session, symbols: list[str]) -> None:
        super().__init__(session, symbols)
        self.ib = None
        self.contracts: dict[str, object] = {}
        self.quote_only: set[str] = set()  # forex / spot metals: no trade prints, mid quotes drive the price

    async def _connect(self) -> None:
        from ib_async import IB, ContFuture, Contract, Forex, Stock

        self.ib = IB()
        host = os.environ.get("IBKR_HOST", "127.0.0.1")
        port = int(os.environ.get("IBKR_PORT", "7497"))
        cid = int(os.environ.get("IBKR_CLIENT_ID", "17"))
        await self.ib.connectAsync(host, port, clientId=cid, timeout=10)
        for s in self.symbols:
            spec = registry().get(s)
            if spec.asset_class in ("stock", "etf"):
                cf = Stock((spec.data or {}).get("ticker", s), "SMART", "USD")
            elif spec.asset_class == "forex" and s.startswith("XA"):
                cf = Contract(secType="CMDTY", symbol=s, exchange="SMART", currency="USD")
                self.quote_only.add(s)
            elif spec.asset_class == "forex":
                cf = Forex(s)
                self.quote_only.add(s)
            elif spec.asset_class == "future":
                cf = ContFuture(symbol=s, exchange=EXCHANGES.get(spec.exchange, spec.exchange), currency="USD")
            else:
                raise RuntimeError(f"{s}: use the Binance source for crypto")
            q = await self.ib.qualifyContractsAsync(cf)
            if not q:
                raise RuntimeError(f"IBKR could not find {s}")
            self.contracts[s] = q[0]

    async def backfill(self) -> None:
        try:
            await self._connect()
        except Exception as e:
            self.set_status("error", f"cannot connect to TWS/IB Gateway: {e}")
            return
        for s, c in self.contracts.items():
            try:
                bars = await self.ib.reqHistoricalDataAsync(c, endDateTime="", durationStr="2 D", barSizeSetting="1 min",
                                                            whatToShow="MIDPOINT" if s in self.quote_only else "TRADES",
                                                            useRTH=False, formatDate=2)
            except Exception as e:
                self.detail = f"history unavailable: {e}"
                continue
            for b in bars[:-1]:  # the last bar is still forming
                ts = int(b.date.replace(tzinfo=b.date.tzinfo or timezone.utc).timestamp())
                self.store_closed(s, {"time": ts, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                                      "volume": float(b.volume)})
            if self.bars[s]:
                last = self.bars[s][-1]
                self.session.engine.on_price(s, last["time"] + 59, last["close"], False)

    async def run(self) -> None:
        if self.ib is None or not self.ib.isConnected():
            return
        handlers = []
        for s, c in self.contracts.items():
            trades = None if s in self.quote_only else self.ib.reqTickByTickData(c, "AllLast")
            quotes = self.ib.reqTickByTickData(c, "BidAsk")

            def on_trades(t, sym=s):
                for x in t.tickByTicks or []:
                    if hasattr(x, "price"):
                        self.on_trade(sym, x.time.timestamp(), x.price, float(x.size))
                t.tickByTicks.clear()

            def on_quotes(t, sym=s, mid_as_trade=s in self.quote_only):
                for x in t.tickByTicks or []:
                    if hasattr(x, "bidPrice"):
                        self.on_quote(sym, x.time.timestamp(), x.bidPrice, x.askPrice)
                        if mid_as_trade and x.bidPrice > 0 and x.askPrice > 0:
                            spec = registry().get(sym)
                            self.on_trade(sym, x.time.timestamp(), spec.round_price((x.bidPrice + x.askPrice) / 2), 0.0)
                t.tickByTicks.clear()

            if trades is not None:
                trades.updateEvent += on_trades
            quotes.updateEvent += on_quotes
            handlers.append((trades, on_trades, quotes, on_quotes))
        self.set_status("live", "IBKR")
        while self._running:
            await asyncio.sleep(1)
            if not self.ib.isConnected():
                self.set_status("reconnecting", "connection to TWS lost")
                try:
                    await self._connect()
                    return await self.run()
                except Exception as e:
                    self.set_status("error", str(e))
                    await asyncio.sleep(5)

    async def close(self) -> None:
        if self.ib is not None and self.ib.isConnected():
            self.ib.disconnect()
