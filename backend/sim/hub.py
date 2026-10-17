"""WebSocket hub: chart subscriptions (bars + indicators) and event broadcasting."""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect

from .contracts import registry
from .data.bars import BarBuilder, Bars, bucket_keys
from .data.store import store
from .indicators import CATALOG, Indicator, make_indicator

FLUSH_INTERVAL = 0.05


def _clean(v: float) -> float | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), 6)


@dataclass
class IndicatorState:
    id: str
    type: str
    params: dict
    obj: Indicator


@dataclass
class Subscription:
    symbol: str
    tf: str
    builder: BarBuilder
    indicators: list[IndicatorState] = field(default_factory=list)
    dirty: dict[int, dict] = field(default_factory=dict)
    ind_dirty: dict[str, dict[str, dict[int, float | None]]] = field(default_factory=dict)
    last_price: float | None = None


class Client:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=5000)
        self.sub: Subscription | None = None

    def send(self, msg: dict) -> None:
        try:
            self.queue.put_nowait(json.dumps(msg, separators=(",", ":"), default=_json_default))
        except asyncio.QueueFull:
            pass


def _json_default(o: Any):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    raise TypeError(type(o))


def compute_indicators(bars: Bars, specs: list[dict], symbol: str) -> dict:
    """Vectorised indicator values aligned with `bars` (used by REST and snapshots)."""
    session = registry().get(symbol).session
    out = {}
    for s in specs:
        kind = s.get("type")
        if kind not in CATALOG:
            continue
        ind = make_indicator(kind, s.get("params") or {}, session)
        vals = ind.batch(bars) if len(bars) else {k: np.zeros(0) for k in CATALOG[kind].outputs}
        out[s["id"]] = {k: [_clean(x) for x in v] for k, v in vals.items()}
    return out


class Hub:
    def __init__(self) -> None:
        self.clients: set[Client] = set()
        self._flusher: asyncio.Task | None = None
        self.session_provider = None  # callable returning the active TradingSession (or None)

    # ---------------------------------------------------------------- connection
    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        client = Client(ws)
        self.clients.add(client)
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.create_task(self._flush_loop())
        sender = asyncio.create_task(self._sender(client))
        sess = self.session_provider() if self.session_provider else None
        client.send({"type": "hello", "ts": int(time.time()),
                     "session": sess.state_dict() if sess else None,
                     "state": sess.trading_state() if sess else None})
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._on_message(client, msg)
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            pass
        finally:
            self.clients.discard(client)
            sender.cancel()

    async def _sender(self, client: Client) -> None:
        try:
            while True:
                msg = await client.queue.get()
                await client.ws.send_text(msg)
        except Exception:
            pass

    async def _on_message(self, client: Client, msg: dict) -> None:
        op = msg.get("op")
        if op == "subscribe":
            self.subscribe(client, msg["symbol"], msg.get("tf", "5m"), msg.get("indicators") or [],
                           int(msg.get("limit", 2000)))
        elif op == "indicators" and client.sub:
            self.subscribe(client, client.sub.symbol, client.sub.tf, msg.get("indicators") or [],
                           int(msg.get("limit", 2000)))
        elif op == "unsubscribe":
            client.sub = None
        elif op == "ping":
            client.send({"type": "pong", "ts": int(time.time())})

    # ---------------------------------------------------------------- subscriptions
    def subscribe(self, client: Client, symbol: str, tf: str, indicators: list[dict], limit: int) -> None:
        symbol = symbol.upper()
        spec = registry().get(symbol)
        sess = self.session_provider() if self.session_provider else None
        builder = BarBuilder(spec.session, tf)
        if sess and symbol in sess.symbols:
            bars, forming = sess.history(symbol, tf, limit)
        else:
            bars, forming = store().bars(symbol, tf, limit=limit), None
        sub = Subscription(symbol=symbol, tf=tf, builder=builder)
        all_bars = bars
        if forming:
            all_bars = _append(bars, forming)
        dec = spec.price_decimals
        for s in indicators:
            kind = s.get("type")
            if kind not in CATALOG:
                continue
            obj = make_indicator(kind, s.get("params") or {}, spec.session)
            for i in range(len(bars)):
                obj.update(SimpleNamespace(ts=int(bars.ts[i]), open=bars.open[i], high=bars.high[i],
                                           low=bars.low[i], close=bars.close[i], volume=bars.volume[i]))
            sub.indicators.append(IndicatorState(s["id"], kind, s.get("params") or {}, obj))
        builder.seed(forming)
        client.sub = sub
        client.send({
            "type": "snapshot", "symbol": symbol, "tf": tf,
            "bars": all_bars.to_columns(dec),
            "indicators": compute_indicators(all_bars, indicators, symbol),
            "live": bool(sess and symbol in sess.symbols),
        })

    # ---------------------------------------------------------------- feed from the session
    def on_point(self, symbol: str, ts: int, price: float, volume: float) -> None:
        for c in self.clients:
            sub = c.sub
            if sub is None or sub.symbol != symbol:
                continue
            closed, cur = sub.builder.point(ts, price, volume)
            if closed is not None:
                sub.dirty[closed["time"]] = dict(closed)
                for ind in sub.indicators:
                    ind.obj.update(SimpleNamespace(ts=closed["time"], **{k: closed[k] for k in
                                                                         ("open", "high", "low", "close", "volume")}))
                    self._mark_ind(sub, ind, closed["time"], ind.obj.values, last=True)
            sub.dirty[cur["time"]] = cur
            sub.last_price = price

    def _mark_ind(self, sub: Subscription, ind: IndicatorState, t: int, values, last: bool) -> None:
        d = sub.ind_dirty.setdefault(ind.id, {})
        if isinstance(values, dict) and last:
            for k, arr in values.items():
                d.setdefault(k, {})[t] = _clean(arr[-1]) if arr else None
        elif isinstance(values, dict):
            for k, v in values.items():
                d.setdefault(k, {})[t] = _clean(v)
        else:
            d.setdefault("value", {})[t] = _clean(values)

    def broadcast(self, msg: dict) -> None:
        for c in self.clients:
            c.send(msg)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            if not self.clients:
                continue
            for c in list(self.clients):
                sub = c.sub
                if sub is None or not sub.dirty:
                    continue
                cur = sub.builder.current
                # forming bar indicator values (peek)
                if cur is not None:
                    for ind in sub.indicators:
                        v = ind.obj.peek(SimpleNamespace(ts=cur["time"], **{k: cur[k] for k in
                                                                            ("open", "high", "low", "close", "volume")}))
                        self._mark_ind(sub, ind, cur["time"], v, last=False)
                dec = registry().get(sub.symbol).price_decimals
                bars = [{k: (round(v, dec) if k in ("open", "high", "low", "close") else v)
                         for k, v in b.items()} for _, b in sorted(sub.dirty.items())]
                ind_out = {iid: {k: sorted(v.items()) for k, v in outs.items()} for iid, outs in sub.ind_dirty.items()}
                c.send({"type": "bars", "symbol": sub.symbol, "tf": sub.tf, "bars": bars,
                        "indicators": ind_out, "price": sub.last_price})
                sub.dirty = {}
                sub.ind_dirty = {}


def _append(bars: Bars, b: dict) -> Bars:
    return Bars(
        ts=np.r_[bars.ts, b["time"]].astype("int64"),
        open=np.r_[bars.open, b["open"]], high=np.r_[bars.high, b["high"]],
        low=np.r_[bars.low, b["low"]], close=np.r_[bars.close, b["close"]],
        volume=np.r_[bars.volume, b["volume"]],
    )


hub = Hub()


def tf_bucket(symbol: str, tf: str, ts: int) -> int:
    return int(bucket_keys(np.array([ts], dtype="int64"), registry().get(symbol).session, tf)[0])
