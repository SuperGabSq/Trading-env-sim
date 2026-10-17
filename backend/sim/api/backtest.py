"""Strategies and backtests: list strategies, queue backtests and parameter sweeps, fetch reports."""

from __future__ import annotations

import asyncio
import json
import time
import traceback
import uuid
from dataclasses import fields
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import settings
from ..db import db
from ..strategy.runner import BacktestConfig, Cancelled, grid, list_strategies, load_strategy, resolve_path, run_backtest

router = APIRouter(prefix="/api")

_jobs: dict[str, dict] = {}          # id -> {status, progress, cancel}
_queue: asyncio.Queue | None = None
_worker: asyncio.Task | None = None


class BacktestReq(BaseModel):
    strategy: str
    symbol: str = "MES"
    timeframe: str | None = None
    start: str | None = None
    end: str | None = None
    balance: float = 50_000
    params: dict = Field(default_factory=dict)
    slippage_ticks: float = 0.0
    limit_through_ticks: float = 1.0
    intrabar: str = "tv"
    fill_resolution: str = "1m"
    roll_fees: bool = True
    spread_model: bool = True
    max_volume_pct: float | None = None
    latency_ms: int = 0
    daily_loss_limit: float | None = None
    max_position: int | None = None
    check_margin: bool = True
    warmup: int | None = None


class SweepReq(BaseModel):
    base: BacktestReq
    grid: dict[str, list]
    metric: str = "net_pnl"


def _cfg(req: BacktestReq, cls) -> BacktestConfig:
    data = req.model_dump()
    data["timeframe"] = req.timeframe or cls.timeframe
    names = {f.name for f in fields(BacktestConfig)}
    return BacktestConfig(**{k: v for k, v in data.items() if k in names})


@router.get("/strategies")
def strategies():
    return list_strategies()


@router.get("/strategies/source")
def strategy_source(id: str):
    path, _ = resolve_path(id)
    p = Path(path).resolve()
    if settings.strategies_dir.resolve() not in p.parents:
        raise HTTPException(403, "Only files in the strategies folder can be shown")
    return {"file": str(p), "source": p.read_text(encoding="utf-8")}


def _save(rid: str, **cols) -> None:
    sets = ", ".join(f"{k}=?" for k in cols)
    db().execute(f"UPDATE backtests SET {sets} WHERE id=?", (*[
        json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for v in cols.values()], rid))


def _create(kind: str, req: BacktestReq, extra: dict | None = None) -> str:
    rid = uuid.uuid4().hex[:12]
    params = {"kind": kind, "request": req.model_dump(), **(extra or {})}
    db().execute("INSERT INTO backtests(id, created, strategy, symbol, timeframe, params, status) VALUES(?,?,?,?,?,?,?)",
                 (rid, time.time(), req.strategy, req.symbol.upper(), req.timeframe or "", json.dumps(params), "queued"))
    _jobs[rid] = {"status": "queued", "progress": 0.0, "cancel": False}
    return rid


async def _ensure_worker() -> None:
    global _queue, _worker
    if _queue is None:
        _queue = asyncio.Queue()
    if _worker is None or _worker.done():
        _worker = asyncio.create_task(_work())


async def _work() -> None:
    while True:
        rid, fn = await _queue.get()
        job = _jobs.get(rid)
        if job is None or job["cancel"]:
            _save(rid, status="cancelled")
            continue
        job["status"] = "running"
        _save(rid, status="running")
        try:
            result = await asyncio.to_thread(fn, job)
            summary = result.get("summary") if isinstance(result, dict) else None
            _save(rid, status="done", summary=summary or {}, result=result)
            job["status"] = "done"
        except Cancelled:
            job["status"] = "cancelled"
            _save(rid, status="cancelled")
        except Exception as e:
            job["status"] = "error"
            _save(rid, status="error", result={"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()})
        job["progress"] = 1.0


@router.post("/backtests")
async def backtest_start(req: BacktestReq):
    try:
        cls = load_strategy(req.strategy)
    except Exception as e:
        raise HTTPException(400, f"Cannot load strategy: {e}")
    cfg = _cfg(req, cls)
    rid = _create("single", req.model_copy(update={"timeframe": cfg.timeframe}))

    def fn(job):
        return run_backtest(load_strategy(req.strategy), cfg, progress=lambda p: job.__setitem__("progress", p),
                            cancel=lambda: job["cancel"])

    await _ensure_worker()
    await _queue.put((rid, fn))
    return {"id": rid}


@router.post("/backtests/sweep")
async def sweep_start(req: SweepReq):
    try:
        cls = load_strategy(req.base.strategy)
    except Exception as e:
        raise HTTPException(400, f"Cannot load strategy: {e}")
    combos = grid(req.grid)
    if not combos or len(combos) > 2000:
        raise HTTPException(400, "The grid must produce between 1 and 2000 combinations")
    base_cfg = _cfg(req.base, cls)
    rid = _create("sweep", req.base.model_copy(update={"timeframe": base_cfg.timeframe}),
                  {"grid": req.grid, "metric": req.metric, "combinations": len(combos)})

    def fn(job):
        rows = []
        strat = load_strategy(req.base.strategy)
        for k, combo in enumerate(combos):
            if job["cancel"]:
                raise Cancelled()
            cfg = BacktestConfig(**{**base_cfg.__dict__, "params": {**base_cfg.params, **combo}})
            try:
                r = run_backtest(strat, cfg)
                rows.append({"params": combo, "summary": r["summary"]})
            except Exception as e:
                rows.append({"params": combo, "error": str(e)})
            job["progress"] = (k + 1) / len(combos)
        key = req.metric
        rows.sort(key=lambda r: (r.get("summary") or {}).get(key) if (r.get("summary") or {}).get(key) is not None
                  else float("-inf"), reverse=True)
        best = rows[0]["summary"] if rows and "summary" in rows[0] else {}
        return {"kind": "sweep", "metric": key, "grid": req.grid, "rows": rows, "summary": best,
                "strategy": strat.display_name(), "config": base_cfg.__dict__}

    await _ensure_worker()
    await _queue.put((rid, fn))
    return {"id": rid, "combinations": len(combos)}


@router.get("/backtests")
def backtest_list(limit: int = 100):
    rows = db().execute("SELECT id, created, strategy, symbol, timeframe, params, status, summary FROM backtests "
                        "ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for rid, created, strat, sym, tf, params, status, summary in rows:
        p = json.loads(params)
        job = _jobs.get(rid)
        out.append({"id": rid, "created": created, "strategy": strat, "symbol": sym, "timeframe": tf,
                    "kind": p.get("kind"), "params": p.get("request", {}).get("params", {}),
                    "start": p.get("request", {}).get("start"), "end": p.get("request", {}).get("end"),
                    "status": status,
                    "progress": job["progress"] if job else (1.0 if status == "done" else 0.0),
                    "combinations": p.get("combinations"),
                    "summary": json.loads(summary) if summary else None})
    return out


@router.get("/backtests/{rid}")
def backtest_get(rid: str):
    row = db().execute("SELECT id, created, strategy, symbol, timeframe, params, status, summary, result "
                       "FROM backtests WHERE id=?", (rid,)).fetchone()
    if not row:
        raise HTTPException(404, "Unknown backtest")
    job = _jobs.get(rid)
    _, created, strat, sym, tf, params, status, summary, result = row
    return {"id": rid, "created": created, "strategy": strat, "symbol": sym, "timeframe": tf,
            "request": json.loads(params), "status": status,
            "progress": job["progress"] if job else 1.0,
            "summary": json.loads(summary) if summary else None, "result": json.loads(result) if result else None}


@router.post("/backtests/{rid}/cancel")
def backtest_cancel(rid: str):
    job = _jobs.get(rid)
    if job:
        job["cancel"] = True
    return {"ok": True}


@router.delete("/backtests/{rid}")
def backtest_delete(rid: str):
    backtest_cancel(rid)
    db().execute("DELETE FROM backtests WHERE id=?", (rid,))
    _jobs.pop(rid, None)
    return {"ok": True}
