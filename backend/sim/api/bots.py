"""External bots.

Live / replay sessions: bots use the same REST endpoints as the UI (`/api/orders`, `/api/positions`,
`/api/account`, `/api/bars`, ...) and the `/ws` event stream. See /docs for the full API.

Lock-step backtests (`/ws/bot`): the simulator drives the clock over historical data and waits for the bot
after every bar, so a bot running in another process (any language) can be backtested with exactly the
same fills as the built-in strategies.

    -> {"op": "start", "symbol": "MES", "timeframe": "5m", "start": "2026-03-02", "end": "2026-03-31",
        "balance": 50000, "warmup": 100, "name": "my bot", "fill": {...}, "risk": {...}}
    <- {"type": "started", "contract": {...}}
    <- {"type": "warmup", "bars": [...]}
    <- {"type": "bar", "bar": {...}, "time": ..., "position": {...}, "account": {...}, "orders": [...],
        "fills": [...new fills...], "results": [...results of the previous actions...]}
    -> {"op": "step", "actions": [{"action": "place", "side": "buy", "qty": 1, "type": "market",
                                   "tp_ticks": 8, "sl_ticks": 8}, {"action": "cancel", "id": 3}, ...]}
    ...
    <- {"type": "done", "summary": {...}, "trades": [...], "report_id": "..."}

Actions: place (fields of POST /api/orders), cancel {id}, modify {id, price, stop, qty}, cancel_all, flatten.
Send {"op": "stop"} to end early.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import fields

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import settings
from ..db import db
from ..engine import EngineError
from ..strategy.runner import BacktestConfig, BacktestSim

router = APIRouter(prefix="/api")
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
BOT_ACCOUNT = "BOT"


@router.get("/bots/protocol")
def protocol():
    """Machine-readable summary of the lock-step protocol."""
    return {"websocket": "/ws/bot", "doc": __doc__}


def _bar(b) -> dict:
    return {"time": b.ts, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}


def _apply(sim: BacktestSim, actions: list[dict]) -> list[dict]:
    eng, acct, sym = sim.engine, BOT_ACCOUNT, sim.symbol
    out = []
    for a in actions or []:
        kind = a.get("action", "place")
        try:
            if kind == "place":
                kw = {k: a[k] for k in ("price", "stop", "trail_ticks", "tif", "reduce_only", "tp", "sl", "tp_ticks",
                                        "sl_ticks", "oco") if k in a and a[k] is not None}
                o = eng.place(acct, sym, a["side"], int(a.get("qty", 1)), a.get("type", "market"),
                              tag=a.get("tag", "bot"), **kw)
                out.append({"ok": o.status.value != "rejected", "order": o.to_dict(),
                            **({"error": o.reason} if o.status.value == "rejected" else {})})
            elif kind == "cancel":
                o = eng.cancel(int(a["id"]))
                out.append({"ok": o is not None, "order": o.to_dict() if o else None})
            elif kind == "modify":
                o = eng.modify(int(a["id"]), price=a.get("price"), stop=a.get("stop"), qty=a.get("qty"))
                out.append({"ok": True, "order": o.to_dict()})
            elif kind == "cancel_all":
                out.append({"ok": True, "canceled": eng.cancel_all(acct, sym)})
            elif kind == "flatten":
                eng.flatten(acct, sym, tag=a.get("tag", "bot flatten"))
                out.append({"ok": True})
            else:
                out.append({"ok": False, "error": f"unknown action {kind}"})
        except (EngineError, KeyError, ValueError, TypeError) as e:
            out.append({"ok": False, "error": f"{type(e).__name__}: {e}"})
    return out


async def handle_bot_ws(ws: WebSocket) -> None:
    host = ws.client.host if ws.client else ""
    if settings.api_key and host not in LOOPBACK and ws.query_params.get("key") != settings.api_key:
        await ws.close(code=4401)
        return
    await ws.accept()
    try:
        start = await ws.receive_json()
        if start.get("op") != "start":
            await ws.send_json({"type": "error", "error": 'first message must be {"op": "start", ...}'})
            await ws.close()
            return
        names = {f.name for f in fields(BacktestConfig)}
        cfg_kw = {k: v for k, v in start.items() if k in names}
        cfg_kw.update({k: v for k, v in (start.get("fill") or {}).items() if k in names})
        cfg_kw.update({k: v for k, v in (start.get("risk") or {}).items() if k in names})
        cfg_kw["close_at_end"] = bool(start.get("close_at_end", True))
        try:
            cfg = BacktestConfig(**cfg_kw)
            sim = BacktestSim(cfg, account=BOT_ACCOUNT)
        except (ValueError, KeyError, FileNotFoundError, TypeError) as e:
            await ws.send_json({"type": "error", "error": str(e)})
            await ws.close()
            return
        name = str(start.get("name") or "external bot")
        await ws.send_json({"type": "started", "contract": sim.spec.to_dict(), "timeframe": cfg.timeframe,
                            "first_ts": sim.first_ts, "minutes": sim.n})
        warm = sim.warmup_bars(int(start.get("warmup", 100)))
        await ws.send_json({"type": "warmup", "bars": [_bar(b) for b in warm]})
        eng = sim.engine
        acct = eng.accounts[BOT_ACCOUNT]
        fills_sent = 0
        results: list[dict] = []
        stopped = False
        for bar in sim.steps():
            pos = acct.positions.get(sim.symbol)
            new_fills = acct.fills[fills_sent:]
            fills_sent = len(acct.fills)
            await ws.send_json({
                "type": "bar", "bar": _bar(bar), "time": eng.last_ts,
                "position": {"qty": pos.qty if pos else 0, "avg_price": pos.avg_price if pos and pos.qty else None},
                "account": eng.account_summary(BOT_ACCOUNT),
                "orders": [o.to_dict() for o in eng.open_orders(BOT_ACCOUNT)],
                "fills": [f.to_dict() for f in new_fills], "results": results,
            })
            msg = await ws.receive_json()
            if msg.get("op") == "stop":
                stopped = True
                break
            results = _apply(sim, msg.get("actions") or [])
        sim.finish(cfg.close_at_end)
        report = sim.report(f"Bot: {name}", {"external": True, "stopped_early": stopped})
        rid = uuid.uuid4().hex[:12]
        db().execute("INSERT INTO backtests(id, created, strategy, symbol, timeframe, params, status, summary, result) "
                     "VALUES(?,?,?,?,?,?,?,?,?)",
                     (rid, time.time(), f"bot:{name}", sim.symbol, cfg.timeframe,
                      json.dumps({"kind": "bot", "request": start}), "done", json.dumps(report["summary"]),
                      json.dumps(report, default=str)))
        await ws.send_json({"type": "done", "summary": report["summary"], "trades": report["trades"],
                            "report_id": rid})
        await ws.close()
    except WebSocketDisconnect:
        return
