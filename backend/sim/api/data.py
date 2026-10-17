"""Market data, indicators, drawings and settings endpoints."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from functools import lru_cache
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from ..contracts import registry
from ..data import importers
from ..data.bars import TIMEFRAMES, Bars
from ..data.store import store
from ..data.synthetic import generate_into_store
from ..db import db
from ..hub import _clean, tf_bucket
from ..indicators import CATALOG, make_indicator
from ..session import manager

router = APIRouter(prefix="/api")


@router.get("/health")
def health():
    from .. import __version__
    return {"ok": True, "version": __version__}


def _symbol_info(sym: str) -> dict:
    meta = store().meta(sym)
    info = {"symbol": sym, "source": meta.source, "synthetic": meta.synthetic, "base_tf": meta.base_tf,
            "rows": meta.rows, "first_ts": meta.first_ts, "last_ts": meta.last_ts,
            "rolls": [r.__dict__ for r in meta.rolls], "notes": meta.notes}
    if sym in registry():
        info["contract"] = registry().get(sym).to_dict()
    try:
        b, _ = store().load(sym)
        info["last_close"] = float(b.close[-1]) if len(b) else None
    except FileNotFoundError:
        info["last_close"] = None
    return info


@router.get("/symbols")
def symbols():
    return [_symbol_info(s) for s in store().symbols() if s in registry()]


@router.get("/contracts")
def contracts():
    return [c.to_dict() for c in registry().all()]


@router.get("/timeframes")
def timeframes():
    return TIMEFRAMES


@router.get("/indicators/catalog")
def indicator_catalog():
    return [{"type": k, "label": v.label, "params": v.params, "outputs": v.outputs, "overlay": v.overlay}
            for k, v in CATALOG.items()]


def _session_cap(symbol: str, tf: str, to: int | None) -> int | None:
    """Never return bars the replay clock has not reached yet."""
    sess = manager.current
    simulated = bool(sess and sess.live and sess.live.store_cap() is not None)
    if sess and (sess.mode == "replay" or simulated) and symbol in sess.symbols:
        part = sess.partial.get(symbol)
        clock = part["time"] if part else (sess.start_ts if sess.mode == "replay" else sess.live.store_cap())
        cap = tf_bucket(symbol, tf, clock)  # only buckets that started before the current one
        return cap if to is None else min(to, cap)
    return to


@lru_cache(maxsize=64)
def _indicator_full(symbol: str, tf: str, adjusted: bool, spec_json: str, version: float) -> dict:
    spec = json.loads(spec_json)
    full = store().bars(symbol, tf, adjusted=adjusted)
    ind = make_indicator(spec["type"], spec.get("params") or {}, registry().get(symbol).session)
    return ind.batch(full) if len(full) else {}


@router.get("/bars")
def bars(symbol: str, tf: str = "5m", to: int | None = None, limit: int = Query(2000, le=20000),
         adjusted: bool = False, indicators: str | None = None):
    symbol = symbol.upper()
    if not store().has(symbol):
        raise HTTPException(404, f"No data for {symbol}")
    to = _session_cap(symbol, tf, to)
    try:
        b = store().bars(symbol, tf, end=to, limit=limit, adjusted=adjusted)
    except ValueError as e:
        raise HTTPException(400, str(e))
    dec = registry().get(symbol).price_decimals
    out = {"symbol": symbol, "tf": tf, "adjusted": adjusted, "bars": b.to_columns(dec),
           "has_more": bool(len(b) == limit)}
    if indicators:
        specs = json.loads(indicators)
        full = store().bars(symbol, tf, adjusted=adjusted)
        version = store().meta(symbol).updated_at
        res = {}
        if len(b):
            i0 = int(np.searchsorted(full.ts, b.ts[0]))
            i1 = i0 + len(b)
            for s in specs:
                if s.get("type") not in CATALOG:
                    continue
                vals = _indicator_full(symbol, tf, adjusted, json.dumps({"type": s["type"], "params": s.get("params")},
                                                                       sort_keys=True), version)
                res[s["id"]] = {k: [_clean(x) for x in v[i0:i1]] for k, v in vals.items()}
        out["indicators"] = res
    return out


# ---- data management ------------------------------------------------------------------------

class GenerateReq(BaseModel):
    symbols: list[str] = ["MES", "MNQ"]
    days: int = 365
    seed: int = 6


@router.post("/data/generate")
async def data_generate(req: GenerateReq):
    metas = await asyncio.to_thread(generate_into_store, req.symbols, req.days, req.seed)
    _indicator_full.cache_clear()
    return [m.to_dict() for m in metas]


@router.post("/data/import")
async def data_import(request: Request, symbol: str, format: str = "auto", tz: str = "UTC",
                      bar_time: str = "open", filename: str = "upload.csv", merge: bool = True):
    body = await request.body()
    if not body:
        raise HTTPException(400, "Empty upload")
    suffix = "".join(Path(filename).suffixes) or ".csv"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        if suffix.endswith((".dbn", ".dbn.zst", ".zst")):
            meta = await asyncio.to_thread(importers.import_dbn_file, tmp, symbol)
        else:
            meta = await asyncio.to_thread(importers.import_csv, tmp, symbol, format, tz, bar_time, merge)
    except (ValueError, KeyError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    _indicator_full.cache_clear()
    return _symbol_info(meta.symbol)


class DatabentoReq(BaseModel):
    symbol: str
    start: str
    end: str
    schema_: str = "ohlcv-1m"
    api_key: str | None = None
    confirm: bool = False


@router.post("/data/databento")
async def data_databento(req: DatabentoReq):
    key = req.api_key or os.environ.get("DATABENTO_API_KEY", "")
    if not key:
        raise HTTPException(400, "Set DATABENTO_API_KEY in .env (or pass api_key).")
    try:
        if not req.confirm:
            cost = await asyncio.to_thread(importers.databento_cost, req.symbol, req.start, req.end, key, req.schema_)
            return {"cost_usd": cost, "confirm_required": True}
        meta = await asyncio.to_thread(importers.import_databento, req.symbol, req.start, req.end, key, req.schema_)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # databento client errors
        raise HTTPException(502, f"Databento: {e}")
    _indicator_full.cache_clear()
    return _symbol_info(meta.symbol)


class BinanceReq(BaseModel):
    symbol: str = "BTCUSDT"
    days: int = 30


@router.post("/data/binance")
async def data_binance(req: BinanceReq):
    try:
        meta = await asyncio.to_thread(importers.import_binance, req.symbol, req.days, lambda *_: None)
    except Exception as e:
        raise HTTPException(502, f"Binance download failed: {e}")
    _indicator_full.cache_clear()
    return _symbol_info(meta.symbol)


class BarsReq(BaseModel):
    root: str = "ES"
    target: str | None = None


@router.post("/data/bars")
async def data_bars(req: BarsReq):
    """Free ES/NQ 1-minute bars from the BARS GitHub repository."""
    from ..data.bars_github import import_bars
    sess = manager.current
    target = (req.target or {"ES": "MES", "NQ": "MNQ"}.get(req.root.upper(), req.root)).upper()
    if sess and target in sess.symbols:
        raise HTTPException(409, f"Stop the session using {target} first")
    try:
        meta = await asyncio.to_thread(import_bars, req.root, target, lambda *_: None)
    except Exception as e:
        raise HTTPException(502, f"BARS import failed: {e}")
    _indicator_full.cache_clear()
    return _symbol_info(meta.symbol)


@router.delete("/data/{symbol}")
def data_delete(symbol: str):
    sess = manager.current
    if sess and symbol.upper() in sess.symbols:
        raise HTTPException(409, "Stop the session using this symbol first")
    store().delete(symbol)
    _indicator_full.cache_clear()
    return {"ok": True}


# ---- drawings & settings ----------------------------------------------------------------------

@router.get("/drawings/{symbol}")
def drawings_get(symbol: str):
    return db().get(f"drawings:{symbol.upper()}", [])


@router.put("/drawings/{symbol}")
async def drawings_put(symbol: str, request: Request):
    db().set(f"drawings:{symbol.upper()}", await request.json())
    return {"ok": True}


@router.get("/settings")
def settings_get():
    return db().get("ui:settings", {})


@router.put("/settings")
async def settings_put(request: Request):
    db().set("ui:settings", await request.json())
    return {"ok": True}
