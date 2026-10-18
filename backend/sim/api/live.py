"""Live paper trading: data sources, starting a live session, strategies inside sessions, resume on restart."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..contracts import registry
from ..data.store import store
from ..db import db
from ..engine import FillConfig, RiskConfig
from ..feeds import alpaca, ibkr
from ..feeds.alpaca import AlpacaFeed
from ..feeds.binance import BinanceFeed
from ..feeds.ibkr import IBKRFeed
from ..feeds.simulated import SimulatedFeed
from ..session import TradingSession, manager
from .trading import FillReq, RiskReq

router = APIRouter(prefix="/api")
log = logging.getLogger("tsim")


def _sources() -> list[dict]:
    minute = [s for s in store().symbols() if s in registry() and store().meta(s).base_tf == "1m"]
    perps = [c.symbol for c in registry().all() if c.exchange == "BINANCE"]
    ib = [c.symbol for c in registry().all() if c.asset_class in ("future", "stock", "etf", "forex")]
    stocks = [c.symbol for c in registry().all() if c.asset_class in ("stock", "etf")]
    return [
        {"id": "simulated", "label": "Simulated live: stored data replayed at real-time speed", "symbols": minute,
         "available": bool(minute),
         "note": "Ticks and quotes generated along stored minute bars, paced at real time. Good for testing bots "
                 "at any hour without a data subscription. Starts 30 minutes before the last stored session close "
                 "unless you choose a start."},
        {"id": "binance", "label": "Binance perpetual futures (free, real time)", "symbols": perps, "available": True,
         "note": "Public trade and order-book streams from Binance USD-M futures; no account or key needed. "
                 "Needs internet access to fstream.binance.com."},
        {"id": "alpaca", "label": "Alpaca: US stocks and ETFs (free account key, real time)", "symbols": stocks,
         "available": alpaca.available(),
         "note": ("Real-time trades and quotes from the free IEX feed (prices are live; volume is IEX only, a few "
                  "percent of the market). Orders stay in this simulator." if alpaca.available() else
                  "Create a free account at alpaca.markets and put ALPACA_API_KEY and ALPACA_API_SECRET in .env, "
                  "then restart.")},
        {"id": "ibkr", "label": "Interactive Brokers: futures, stocks, ETFs, forex, gold (needs TWS / IB Gateway)",
         "symbols": ib, "available": ibkr.available(),
         "note": ("Live trades and bid/ask through your running TWS or IB Gateway (API enabled, market data "
                  "subscriptions for the instruments; forex quotes are free). Orders stay in this simulator."
                  if ibkr.available() else
                  "Install the connector first:  .venv\\Scripts\\pip install ib_async  then restart.")},
    ]


@router.get("/live/sources")
def sources():
    return _sources()


class LiveReq(BaseModel):
    source: str
    symbols: list[str]
    balance: float = 50_000
    start: int | None = None          # simulated source: UTC epoch to start from
    speed: float = 1.0                # simulated source only
    fill: FillReq = Field(default_factory=FillReq)
    risk: RiskReq = Field(default_factory=RiskReq)


def _adapter(source: str, sess: TradingSession, symbols: list[str], start: int | None, speed: float):
    if source == "simulated":
        if start is None:
            last = min(store().meta(s).last_ts for s in symbols)
            start = last - 30 * 60
        return SimulatedFeed(sess, symbols, start, speed)
    if source == "binance":
        return BinanceFeed(sess, symbols)
    if source == "ibkr":
        if not ibkr.available():
            raise HTTPException(400, "The ib_async package is not installed")
        return IBKRFeed(sess, symbols)
    if source == "alpaca":
        if not alpaca.available():
            raise HTTPException(400, "Set ALPACA_API_KEY and ALPACA_API_SECRET in .env first")
        return AlpacaFeed(sess, symbols)
    raise HTTPException(400, f"Unknown source {source}")


@router.post("/session/live")
async def session_live(req: LiveReq):
    src = next((s for s in _sources() if s["id"] == req.source), None)
    if src is None:
        raise HTTPException(400, f"Unknown source {req.source}")
    symbols = [s.upper() for s in req.symbols]
    bad = [s for s in symbols if s not in src["symbols"]]
    if bad or not symbols:
        raise HTTPException(400, f"{', '.join(bad) or 'No symbols'} not available from {req.source}")
    sess = TradingSession("live", symbols, None, req.balance, FillConfig(**req.fill.model_dump()),
                          RiskConfig(**req.risk.model_dump()))
    adapter = _adapter(req.source, sess, symbols, req.start, req.speed)
    await manager.start(sess)
    await sess.start_live(adapter)
    return sess.state_dict()


# ---- strategies inside the current session ----------------------------------------------------------

class AttachReq(BaseModel):
    strategy: str
    symbol: str | None = None
    timeframe: str | None = None
    params: dict = Field(default_factory=dict)


@router.post("/session/strategies")
def strategy_attach(req: AttachReq):
    sess = manager.current
    if sess is None:
        raise HTTPException(409, "Start a replay or live session first")
    try:
        r = sess.attach_strategy(req.strategy, req.symbol, req.timeframe, req.params)
    except (ValueError, FileNotFoundError, ImportError) as e:
        raise HTTPException(400, str(e))
    return r.info()


@router.get("/session/strategies")
def strategy_list():
    sess = manager.current
    return [r.info() for r in sess.strategies] if sess else []


@router.delete("/session/strategies/{rid}")
def strategy_detach(rid: str, flatten: bool = False):
    sess = manager.current
    if sess is None:
        raise HTTPException(409, "No session")
    sess.detach_strategy(rid, flatten)
    return {"ok": True}


# ---- resume after a restart ------------------------------------------------------------------------

async def restore_on_startup() -> None:
    state = db().get("session:live")
    if not state:
        return
    try:
        eng = state["engine"]
        fill = FillConfig(**eng.get("fill_cfg", {}))
        risk = RiskConfig(**eng.get("risk", {}))
        sess = TradingSession("live", state["symbols"], None, state.get("balance", 50_000), fill, risk)
        sess.engine.restore(eng)
        sess.engine.hooks["SIM"] = sess._strategy_hook
        sess.id = state.get("id", sess.id)
        adapter = _adapter(state["source"], sess, state["symbols"], state.get("start"), 1.0)
        await manager.start(sess)
        await sess.start_live(adapter)
        for st in state.get("strategies", []):
            try:
                sess.attach_strategy(st["ref"], st["symbol"], st["timeframe"], st["params"], st.get("my_orders"))
            except Exception as e:  # keep the session even if a strategy file changed
                log.warning("could not re-attach strategy %s: %s", st.get("ref"), e)
        log.warning("Resumed live session %s (%s)", sess.id, state["source"])
    except Exception as e:
        log.warning("Could not resume the live session: %s", e)
