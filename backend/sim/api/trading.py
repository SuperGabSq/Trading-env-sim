"""Session control, orders, positions, account and journal endpoints (used by the UI and bots)."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..contracts import registry
from ..data.store import store
from ..engine import EngineError, FillConfig, RiskConfig
from ..session import ACCOUNT, TradingSession, manager

router = APIRouter(prefix="/api")


class FillReq(BaseModel):
    slippage_ticks: float = 0.0
    limit_through_ticks: float = 1.0
    intrabar: str = "tv"
    roll_fees: bool = True
    spread_model: bool = True
    max_volume_pct: float | None = None
    latency_ms: int = 0


class RiskReq(BaseModel):
    daily_loss_limit: float | None = None
    max_position: int | None = None
    check_margin: bool = True
    auto_liquidate: bool = True


class ReplayReq(BaseModel):
    symbols: list[str]
    start: int                       # UTC epoch seconds
    balance: float = 50_000
    speed: int = 60
    fill: FillReq = Field(default_factory=FillReq)
    risk: RiskReq = Field(default_factory=RiskReq)


class ControlReq(BaseModel):
    action: str                      # play | pause | step | speed | jump
    tf: str | None = None
    count: int = 1
    speed: int | None = None
    ts: int | None = None


class OrderReq(BaseModel):
    symbol: str
    side: str
    qty: int = 1
    type: str = "market"
    price: float | None = None
    stop: float | None = None
    trail_ticks: int | None = None
    tif: str = "gtc"
    reduce_only: bool = False
    tp: float | None = None
    sl: float | None = None
    tp_ticks: float | None = None
    sl_ticks: float | None = None
    oco: str | None = None
    tag: str = "manual"


class ModifyReq(BaseModel):
    price: float | None = None
    stop: float | None = None
    qty: int | None = None
    trail_ticks: int | None = None


class SymbolReq(BaseModel):
    symbol: str | None = None


def _sess():
    try:
        return manager.require()
    except EngineError as e:
        raise HTTPException(409, str(e))


@router.get("/session")
def session_get():
    s = manager.current
    return s.state_dict() if s else None


@router.post("/session/replay")
async def session_replay(req: ReplayReq):
    for s in req.symbols:
        if not store().has(s):
            raise HTTPException(404, f"No data for {s}")
    try:
        sess = TradingSession("replay", req.symbols, req.start, req.balance,
                              FillConfig(**req.fill.model_dump()), RiskConfig(**req.risk.model_dump()), req.speed)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    await manager.start(sess)
    return sess.state_dict()


@router.delete("/session")
async def session_stop():
    old = await manager.stop()
    return {"ok": True, "summary": old.trading_state() if old else None}


@router.post("/session/control")
async def session_control(req: ControlReq):
    s = _sess()
    if req.action == "play":
        await s.play()
    elif req.action == "pause":
        s.pause()
    elif req.action == "step":
        s.pause()
        s.step(req.tf, req.count)
    elif req.action == "speed":
        s.set_speed(req.speed or 60)
    elif req.action == "jump":
        if req.ts is None:
            raise HTTPException(400, "ts required")
        s.pause()
        s.jump(req.ts)
    else:
        raise HTTPException(400, f"Unknown action {req.action}")
    return s.state_dict()


@router.get("/state")
def state():
    s = manager.current
    return s.trading_state() if s else None


@router.get("/account")
def account():
    return _sess().engine.account_summary(ACCOUNT)


@router.get("/positions")
def positions():
    return _sess().trading_state()["positions"]


@router.get("/orders")
def orders(all: bool = False):
    s = _sess()
    if all:
        return [o.to_dict() for o in s.engine.orders.values() if o.account == ACCOUNT]
    return [o.to_dict() for o in s.engine.open_orders(ACCOUNT)]


@router.post("/orders")
def order_place(req: OrderReq):
    s = _sess()
    if req.symbol.upper() not in s.symbols:
        raise HTTPException(400, f"{req.symbol} is not part of this session ({', '.join(s.symbols)})")
    try:
        o = s.place(**{**req.model_dump(), "symbol": req.symbol.upper()})
    except (ValueError, KeyError, EngineError) as e:
        raise HTTPException(400, str(e))
    return o.to_dict()


@router.patch("/orders/{order_id}")
def order_modify(order_id: int, req: ModifyReq):
    s = _sess()
    try:
        return s.modify(order_id, **req.model_dump()).to_dict()
    except EngineError as e:
        raise HTTPException(400, str(e))


@router.delete("/orders/{order_id}")
def order_cancel(order_id: int):
    o = _sess().cancel(order_id)
    if o is None:
        raise HTTPException(404, "Unknown order")
    return o.to_dict()


@router.post("/orders/cancel_all")
def order_cancel_all(req: SymbolReq):
    return {"canceled": _sess().cancel_all(req.symbol.upper() if req.symbol else None)}


@router.post("/flatten")
def flatten(req: SymbolReq):
    _sess().flatten(req.symbol.upper() if req.symbol else None)
    return {"ok": True}


@router.post("/reverse")
def reverse(req: SymbolReq):
    if not req.symbol:
        raise HTTPException(400, "symbol required")
    _sess().reverse(req.symbol.upper())
    return {"ok": True}


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ""


@router.get("/journal.csv")
def journal_csv():
    s = _sess()
    acct = s.engine.accounts[ACCOUNT]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "symbol", "side", "qty", "entry_time_utc", "entry_price", "exit_time_utc", "exit_price",
                "pnl", "fees", "net", "pnl_ticks", "mae_ticks", "mfe_ticks", "entry_tag", "exit_reason"])
    for t in acct.trades:
        spec = registry().get(t.symbol)
        ticks = (t.pnl / (t.qty * spec.tick_value)) if t.qty else 0
        w.writerow([t.id, t.symbol, t.side, t.qty, _iso(t.entry_ts), t.entry_price, _iso(t.exit_ts), t.exit_price,
                    round(t.pnl, 2), round(t.fees, 2), round(t.net, 2), round(ticks, 2), round(t.mae_ticks, 2),
                    round(t.mfe_ticks, 2), t.entry_tag, t.exit_reason])
    buf.seek(0)
    name = f"journal_{s.mode}_{s.id}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/fills.csv")
def fills_csv():
    s = _sess()
    acct = s.engine.accounts[ACCOUNT]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "order_id", "time_utc", "symbol", "side", "qty", "price", "fee", "realized", "position_after", "tag"])
    for f in acct.fills:
        w.writerow([f.id, f.order_id, _iso(f.ts), f.symbol, f.side.value, f.qty, f.price, f.fee, f.realized,
                    f.position_after, f.tag])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="fills_{s.id}.csv"'})
