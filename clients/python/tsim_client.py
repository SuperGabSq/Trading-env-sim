"""Python client for Trading Env Sim (single file; needs `httpx` and `websockets`).

Copy this file next to your bot, or `pip install httpx websockets` and import it from here.

Live / replay session (the app must be running and a session started):

    from tsim_client import TsimClient
    sim = TsimClient("http://127.0.0.1:8000")
    print(sim.account())
    sim.buy("MES", 1, tp_ticks=8, sl_ticks=8)          # market + bracket
    sim.sell("MES", 1, type="limit", price=7250.25)
    for o in sim.orders(): print(o)
    sim.flatten("MES")

    async for msg in sim.events(symbol="MES", tf="1m"):   # bars, fills, orders, account updates
        ...

Lock-step backtest (the simulator waits for your bot after every bar):

    import asyncio
    from tsim_client import Lockstep

    async def main():
        async with Lockstep(symbol="MES", timeframe="5m", start="2026-03-02", end="2026-03-31") as bt:
            async for step in bt:
                if step.position == 0 and step.bar["close"] > step.bar["open"]:
                    bt.buy(1, tp_ticks=8, sl_ticks=8)
        print(bt.summary)

    asyncio.run(main())
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None


class TsimError(Exception):
    pass


class TsimClient:
    def __init__(self, url: str = "http://127.0.0.1:8000", api_key: str | None = None, timeout: float = 30) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self._http = httpx.Client(base_url=self.url, timeout=timeout,
                                  headers={"X-API-Key": api_key} if api_key else {})

    # ------------------------------------------------------------------ plumbing
    def _req(self, method: str, path: str, **kw) -> Any:
        r = self._http.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except Exception:
                detail = r.text
            raise TsimError(f"{r.status_code}: {detail}")
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text

    def get(self, path: str, **params) -> Any:
        return self._req("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, body: dict | None = None) -> Any:
        return self._req("POST", path, json=body or {})

    # ------------------------------------------------------------------ data
    def symbols(self) -> list[dict]:
        return self.get("/api/symbols")

    def bars(self, symbol: str, tf: str = "5m", limit: int = 500, to: int | None = None,
             indicators: list[dict] | None = None) -> list[dict]:
        r = self.get("/api/bars", symbol=symbol, tf=tf, limit=limit, to=to,
                     indicators=json.dumps(indicators) if indicators else None)
        c = r["bars"]
        rows = [dict(time=t, open=o, high=h, low=lo, close=cl, volume=v)
                for t, o, h, lo, cl, v in zip(c["time"], c["open"], c["high"], c["low"], c["close"], c["volume"])]
        for iid, outs in (r.get("indicators") or {}).items():
            for out, vals in outs.items():
                key = iid if out == "value" else f"{iid}.{out}"
                for row, v in zip(rows, vals):
                    row[key] = v
        return rows

    # ------------------------------------------------------------------ session
    def session(self) -> dict | None:
        return self.get("/api/session")

    def start_replay(self, symbols: list[str], start: int, balance: float = 50_000, speed: int = 60, **fill) -> dict:
        return self.post("/api/session/replay", {"symbols": symbols, "start": start, "balance": balance,
                                                 "speed": speed, "fill": fill})

    def start_live(self, source: str, symbols: list[str], balance: float = 50_000, **kw) -> dict:
        return self.post("/api/session/live", {"source": source, "symbols": symbols, "balance": balance, **kw})

    def control(self, action: str, **kw) -> dict:
        """action: play | pause | step (tf=, count=) | speed (speed=) | jump (ts=)"""
        return self.post("/api/session/control", {"action": action, **kw})

    def stop_session(self) -> dict:
        return self._req("DELETE", "/api/session")

    # ------------------------------------------------------------------ account
    def account(self) -> dict:
        return self.get("/api/account")

    def positions(self) -> list[dict]:
        return self.get("/api/positions")

    def position(self, symbol: str) -> int:
        for p in self.positions():
            if p["symbol"] == symbol:
                return p["qty"]
        return 0

    def orders(self) -> list[dict]:
        return self.get("/api/orders")

    def state(self) -> dict:
        return self.get("/api/state")

    # ------------------------------------------------------------------ orders
    def place(self, symbol: str, side: str, qty: int = 1, type: str = "market", **kw) -> dict:
        """kw: price, stop, trail_ticks, tif ('gtc'|'day'|'ioc'), reduce_only, tp, sl, tp_ticks, sl_ticks, oco, tag"""
        kw.setdefault("tag", "bot")
        o = self.post("/api/orders", {"symbol": symbol, "side": side, "qty": qty, "type": type, **kw})
        if o.get("status") == "rejected":
            raise TsimError(f"order rejected: {o.get('reason')}")
        return o

    def buy(self, symbol: str, qty: int = 1, type: str = "market", **kw) -> dict:
        return self.place(symbol, "buy", qty, type, **kw)

    def sell(self, symbol: str, qty: int = 1, type: str = "market", **kw) -> dict:
        return self.place(symbol, "sell", qty, type, **kw)

    def modify(self, order_id: int, **kw) -> dict:
        return self._req("PATCH", f"/api/orders/{order_id}", json=kw)

    def cancel(self, order_id: int) -> dict:
        return self._req("DELETE", f"/api/orders/{order_id}")

    def cancel_all(self, symbol: str | None = None) -> dict:
        return self.post("/api/orders/cancel_all", {"symbol": symbol})

    def flatten(self, symbol: str | None = None) -> dict:
        return self.post("/api/flatten", {"symbol": symbol})

    # ------------------------------------------------------------------ streaming
    async def events(self, symbol: str | None = None, tf: str = "1m", indicators: list[dict] | None = None
                     ) -> AsyncIterator[dict]:
        """Yields every message: hello, snapshot, bars, order, fill, position, account, trade, message, clock..."""
        if websockets is None:
            raise TsimError("pip install websockets")
        ws_url = self.url.replace("http", "ws", 1) + "/ws"
        async with websockets.connect(ws_url, max_size=50_000_000) as ws:
            if symbol:
                await ws.send(json.dumps({"op": "subscribe", "symbol": symbol, "tf": tf, "indicators": indicators or [],
                                          "limit": 300}))
            async for raw in ws:
                yield json.loads(raw)


# ---------------------------------------------------------------------- lock-step backtests

@dataclass
class Step:
    bar: dict
    time: int
    position: int
    avg_price: float | None
    account: dict
    orders: list[dict]
    fills: list[dict]
    results: list[dict]


@dataclass
class Lockstep:
    symbol: str = "MES"
    timeframe: str = "5m"
    start: str | int | None = None
    end: str | int | None = None
    balance: float = 50_000
    warmup: int = 100
    name: str = "external bot"
    url: str = "http://127.0.0.1:8000"
    api_key: str | None = None
    fill: dict = field(default_factory=dict)   # slippage_ticks, limit_through_ticks, intrabar, spread_model, ...
    risk: dict = field(default_factory=dict)   # daily_loss_limit, max_position, check_margin

    def __post_init__(self) -> None:
        self.warmup_bars: list[dict] = []
        self.contract: dict = {}
        self.summary: dict = {}
        self.trades: list[dict] = []
        self.report_id: str | None = None
        self._actions: list[dict] = []
        self._ws = None

    async def __aenter__(self) -> "Lockstep":
        if websockets is None:
            raise TsimError("pip install websockets")
        q = f"?key={self.api_key}" if self.api_key else ""
        self._ws = await websockets.connect(self.url.replace("http", "ws", 1) + "/ws/bot" + q, max_size=50_000_000)
        await self._ws.send(json.dumps({"op": "start", "symbol": self.symbol, "timeframe": self.timeframe,
                                        "start": self.start, "end": self.end, "balance": self.balance,
                                        "warmup": self.warmup, "name": self.name, "fill": self.fill, "risk": self.risk}))
        msg = json.loads(await self._ws.recv())
        if msg["type"] == "error":
            raise TsimError(msg["error"])
        self.contract = msg["contract"]
        self.warmup_bars = json.loads(await self._ws.recv())["bars"]
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ws is not None:
            await self._ws.close()

    def __aiter__(self) -> AsyncIterator[Step]:
        return self._steps()

    async def _steps(self) -> AsyncIterator[Step]:
        while True:
            msg = json.loads(await self._ws.recv())
            if msg["type"] == "done":
                self.summary, self.trades, self.report_id = msg["summary"], msg["trades"], msg.get("report_id")
                return
            if msg["type"] == "error":
                raise TsimError(msg["error"])
            pos = msg["position"]
            yield Step(msg["bar"], msg["time"], pos["qty"], pos["avg_price"], msg["account"], msg["orders"],
                       msg["fills"], msg["results"])
            actions, self._actions = self._actions, []
            await self._ws.send(json.dumps({"op": "step", "actions": actions}))

    async def stop(self) -> None:
        """End the test early (the report covers the bars seen so far)."""
        await self._ws.send(json.dumps({"op": "stop"}))
        msg = json.loads(await self._ws.recv())
        if msg["type"] == "done":
            self.summary, self.trades, self.report_id = msg["summary"], msg["trades"], msg.get("report_id")

    # actions are sent with the reply to the current bar and act from the next price on
    def place(self, side: str, qty: int = 1, type: str = "market", **kw) -> None:
        self._actions.append({"action": "place", "side": side, "qty": qty, "type": type, **kw})

    def buy(self, qty: int = 1, type: str = "market", **kw) -> None:
        self.place("buy", qty, type, **kw)

    def sell(self, qty: int = 1, type: str = "market", **kw) -> None:
        self.place("sell", qty, type, **kw)

    def cancel(self, order_id: int) -> None:
        self._actions.append({"action": "cancel", "id": order_id})

    def modify(self, order_id: int, **kw) -> None:
        self._actions.append({"action": "modify", "id": order_id, **kw})

    def cancel_all(self) -> None:
        self._actions.append({"action": "cancel_all"})

    def flatten(self) -> None:
        self._actions.append({"action": "flatten"})
