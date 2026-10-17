"""Parquet bar store.

Layout (one continuous series per root symbol):

    data/bars/<SYMBOL>/base.parquet   ts, open, high, low, close, volume, contract
    data/bars/<SYMBOL>/meta.json      source, base timeframe, rolls, coverage

Prices are UNADJUSTED (the traded price of whichever contract was front month). Rolls are listed
in meta.json with the price gap between the new and the old contract, which is what the
back-adjusted view and the engine's position roll use.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import settings
from ..contracts import registry
from .bars import Bars, resample, tf_minutes


@dataclass
class Roll:
    ts: int          # first bar on the new contract
    from_contract: str
    to_contract: str
    gap: float       # new contract price - old contract price at the roll


@dataclass
class SeriesMeta:
    symbol: str
    source: str = "unknown"
    base_tf: str = "1m"
    synthetic: bool = False
    rows: int = 0
    first_ts: int | None = None
    last_ts: int | None = None
    rolls: list[Roll] = field(default_factory=list)
    updated_at: float = 0.0
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SeriesMeta":
        rolls = [Roll(**r) for r in d.pop("rolls", [])]
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(rolls=rolls, **known)


class BarStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or settings.bars_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[float, Bars, SeriesMeta]] = {}
        self._tf_cache: dict[tuple[str, str, bool], Bars] = {}

    # ---- paths ------------------------------------------------------------------------------
    def _dir(self, symbol: str) -> Path:
        return self.root / symbol.upper()

    def symbols(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if (p / "base.parquet").exists())

    def has(self, symbol: str) -> bool:
        return (self._dir(symbol) / "base.parquet").exists()

    def meta(self, symbol: str) -> SeriesMeta:
        p = self._dir(symbol) / "meta.json"
        if not p.exists():
            return SeriesMeta(symbol=symbol.upper())
        return SeriesMeta.from_dict(json.loads(p.read_text(encoding="utf-8")))

    # ---- write ------------------------------------------------------------------------------
    def write(self, symbol: str, df: pd.DataFrame, meta: SeriesMeta, merge: bool = False) -> SeriesMeta:
        """Write a frame with columns ts (epoch s), open, high, low, close, volume[, contract]."""
        symbol = symbol.upper()
        df = _normalise(df)
        d = self._dir(symbol)
        d.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if merge and self.has(symbol):
                old = pd.read_parquet(d / "base.parquet")
                df = pd.concat([old[~old["ts"].isin(df["ts"])], df]).sort_values("ts", kind="stable")
                old_meta = self.meta(symbol)
                rolls = {r.ts: r for r in old_meta.rolls}
                rolls.update({r.ts: r for r in meta.rolls})
                meta.rolls = sorted(rolls.values(), key=lambda r: r.ts)
            df = df.reset_index(drop=True)
            meta.symbol = symbol
            meta.rows = len(df)
            meta.first_ts = int(df["ts"].iloc[0]) if len(df) else None
            meta.last_ts = int(df["ts"].iloc[-1]) if len(df) else None
            meta.updated_at = time.time()
            tmp = d / "base.parquet.tmp"
            df.to_parquet(tmp, index=False)
            tmp.replace(d / "base.parquet")
            (d / "meta.json").write_text(json.dumps(meta.to_dict(), indent=2), encoding="utf-8")
            self._invalidate(symbol)
        return meta

    def delete(self, symbol: str) -> None:
        d = self._dir(symbol)
        with self._lock:
            for p in d.glob("*"):
                p.unlink()
            if d.exists():
                d.rmdir()
            self._invalidate(symbol)

    def _invalidate(self, symbol: str) -> None:
        self._cache.pop(symbol, None)
        for k in [k for k in self._tf_cache if k[0] == symbol]:
            self._tf_cache.pop(k, None)

    # ---- read -------------------------------------------------------------------------------
    def load(self, symbol: str) -> tuple[Bars, SeriesMeta]:
        symbol = symbol.upper()
        p = self._dir(symbol) / "base.parquet"
        if not p.exists():
            raise FileNotFoundError(f"No data for {symbol}. Import or generate data first.")
        mtime = p.stat().st_mtime
        with self._lock:
            hit = self._cache.get(symbol)
            if hit and hit[0] == mtime:
                return hit[1], hit[2]
            df = pd.read_parquet(p)
            if "contract" in df.columns:
                cat = pd.Categorical(df["contract"].astype(str))
                codes = cat.codes.astype("int16")
                contracts = [str(c) for c in cat.categories]
            else:
                codes, contracts = np.zeros(len(df), dtype="int16"), [symbol]
            bars = Bars(
                ts=df["ts"].to_numpy("int64"),
                open=df["open"].to_numpy("float64"),
                high=df["high"].to_numpy("float64"),
                low=df["low"].to_numpy("float64"),
                close=df["close"].to_numpy("float64"),
                volume=df["volume"].to_numpy("float64"),
                contract=codes,
                contracts=contracts,
            )
            meta = self.meta(symbol)
            self._cache[symbol] = (mtime, bars, meta)
            for k in [k for k in self._tf_cache if k[0] == symbol]:
                self._tf_cache.pop(k, None)
            return bars, meta

    def adjustment_offsets(self, symbol: str, ts: np.ndarray) -> np.ndarray:
        """Additive back-adjustment: every bar before a roll is shifted by that roll's gap."""
        _, meta = self.load(symbol)
        off = np.zeros(len(ts))
        for r in meta.rolls:
            off[ts < r.ts] += r.gap
        return off

    def bars(self, symbol: str, tf: str = "1m", start: int | None = None, end: int | None = None,
             limit: int | None = None, adjusted: bool = False) -> Bars:
        """Bars of `tf` with start <= ts < end (bucket open time), optionally the last `limit`."""
        symbol = symbol.upper()
        base, meta = self.load(symbol)
        key = (symbol, tf, adjusted)
        with self._lock:
            full = self._tf_cache.get(key)
            if full is None:
                src = base
                if adjusted and meta.rolls:
                    src = base.shifted(self.adjustment_offsets(symbol, base.ts))
                if tf_minutes(tf) < tf_minutes(meta.base_tf):
                    raise ValueError(f"{symbol} data is {meta.base_tf}; cannot show {tf}")
                if tf == meta.base_tf:
                    full = src
                else:
                    full = resample(src, registry().get(symbol).session, tf)
                self._tf_cache[key] = full
        out = full.between(start, end)
        if limit:
            out = out.tail(limit)
        return out


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    cols = ["ts", "open", "high", "low", "close", "volume"]
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns: {missing}")
    out = df[cols + (["contract"] if "contract" in df.columns else [])].copy()
    out["ts"] = out["ts"].astype("int64")
    for c in ["open", "high", "low", "close", "volume"]:
        out[c] = out[c].astype("float64")
    if "contract" in out.columns:
        out["contract"] = out["contract"].astype(str)
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out.drop_duplicates("ts", keep="last").sort_values("ts", kind="stable")
    # Repair OHLC consistency (some exports contain highs below closes, etc.)
    out["high"] = out[["open", "high", "low", "close"]].max(axis=1)
    out["low"] = out[["open", "high", "low", "close"]].min(axis=1)
    return out


_store: BarStore | None = None


def store() -> BarStore:
    global _store
    if _store is None:
        _store = BarStore()
    return _store
