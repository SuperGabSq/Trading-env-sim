"""Importers: CSV (generic, TradingView, NinjaTrader, Databento), Databento API/DBN files, Binance."""

from __future__ import annotations

import io
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ..contracts import registry
from .continuous import build_continuous, outright_mask
from .store import SeriesMeta, store

FORMATS = ["auto", "generic", "tradingview", "ninjatrader", "databento"]


# ---- helpers --------------------------------------------------------------------------------

def detect_base_tf(ts: np.ndarray) -> str:
    if len(ts) < 3:
        return "1m"
    d = np.diff(np.sort(ts))
    d = d[d > 0]
    step = int(np.median(d)) if len(d) else 60
    minutes = max(1, round(step / 60))
    if minutes >= 1440:
        return "1D"
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def dt_to_epoch(values) -> np.ndarray:
    """tz-aware datetimes -> UTC epoch seconds, independent of the datetime resolution (pandas 2/3)."""
    s = pd.Series(pd.to_datetime(values, utc=True))
    return ((s - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)).to_numpy("int64")


def _to_epoch_seconds(col: pd.Series, tz: str) -> np.ndarray:
    if pd.api.types.is_numeric_dtype(col):
        v = col.astype("int64").to_numpy()
        med = np.median(np.abs(v))
        if med > 1e17:   # nanoseconds
            return v // 1_000_000_000
        if med > 1e14:   # microseconds
            return v // 1_000_000
        if med > 1e11:   # milliseconds
            return v // 1000
        return v
    s = col.astype(str).str.strip()
    sample = s.iloc[: min(len(s), 50)]
    has_offset = sample.str.contains(r"(?:[+-]\d{2}:?\d{2}|Z)$", regex=True).any()
    if has_offset:  # e.g. 2026-03-09 10:00:00-0400: offsets may change with DST inside one file
        parsed = pd.to_datetime(s, utc=True, errors="coerce", format="mixed")
    else:
        parsed = pd.to_datetime(s, errors="coerce", format="mixed")
        parsed = parsed.dt.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward")
    if parsed.isna().all():
        raise ValueError("Could not parse the time column")
    return dt_to_epoch(parsed)


def _find(cols: list[str], *names: str) -> str | None:
    low = {c.lower().strip(): c for c in cols}
    for n in names:
        if n in low:
            return low[n]
    return None


# ---- CSV ------------------------------------------------------------------------------------

def read_csv_bars(path: str | Path, fmt: str = "auto", tz: str = "UTC",
                  bar_time: str = "open") -> pd.DataFrame:
    """Return ts, open, high, low, close, volume[, contract] from a CSV file.

    tz        time zone of timestamps that carry no offset (TradingView ISO times carry one).
    bar_time  'open' if timestamps mark the bar open (TradingView, Databento), 'close' if they mark
              the bar close (NinjaTrader). NinjaTrader files are detected automatically.
    """
    path = Path(path)
    if path.suffix == ".gz":
        import gzip
        with gzip.open(path, "rt", encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    else:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    first = text.splitlines()[0] if text else ""
    if fmt == "auto":
        if re.match(r"^\d{8} \d{6};", first):
            fmt = "ninjatrader"
        elif "ts_event" in first:
            fmt = "databento"
        elif first.lower().startswith("time,"):
            fmt = "tradingview"
        else:
            fmt = "generic"

    if fmt == "ninjatrader":
        raw = pd.read_csv(io.StringIO(text), sep=";", header=None,
                          names=["dt", "open", "high", "low", "close", "volume"])
        dt = pd.to_datetime(raw["dt"], format="%Y%m%d %H%M%S")
        ts = _to_epoch_seconds(dt.dt.strftime("%Y-%m-%d %H:%M:%S"), tz)
        out = raw[["open", "high", "low", "close", "volume"]].copy()
        out.insert(0, "ts", ts)
        bar_time = "close"
    else:
        raw = pd.read_csv(io.StringIO(text), sep=None, engine="python")
        cols = list(raw.columns)
        if fmt == "databento":
            tcol = _find(cols, "ts_event")
        else:
            tcol = _find(cols, "time", "timestamp", "datetime", "date", "ts", "open_time", "date/time")
        if tcol is None:
            raise ValueError(f"No time column found in {cols}")
        tseries = raw[tcol]
        dcol, hcol = _find(cols, "date"), _find(cols, "time")
        if dcol and hcol and dcol != hcol and not pd.api.types.is_numeric_dtype(raw[hcol]):
            tseries = raw[dcol].astype(str) + " " + raw[hcol].astype(str)
        out = pd.DataFrame({"ts": _to_epoch_seconds(tseries, tz)})
        for name, aliases in {"open": ("open", "o"), "high": ("high", "h"), "low": ("low", "l"),
                              "close": ("close", "c", "last"), "volume": ("volume", "vol", "v")}.items():
            col = _find(cols, *aliases)
            if col is None:
                if name == "volume":
                    out[name] = 0.0
                    continue
                raise ValueError(f"No '{name}' column found in {cols}")
            out[name] = pd.to_numeric(raw[col], errors="coerce").to_numpy()
        if fmt == "databento" and out["open"].abs().median() > 1e7:  # fixed-point prices (1e-9)
            for c in ["open", "high", "low", "close"]:
                out[c] = out[c] / 1e9
        ccol = _find(cols, "contract", "symbol", "instrument")
        if ccol:  # per-contract files can be stitched into a continuous series with real roll gaps
            out["contract"] = raw[ccol].astype(str).str.upper().to_numpy()

    out = out.dropna(subset=["ts", "open", "high", "low", "close"])
    if bar_time == "close":
        step = _tf_seconds(detect_base_tf(out["ts"].to_numpy()))
        out["ts"] = out["ts"] - step
    return out.sort_values("ts").reset_index(drop=True)


def _tf_seconds(tf: str) -> int:
    from .bars import tf_minutes
    return tf_minutes(tf) * 60


def import_frame(symbol: str, df: pd.DataFrame, source: str, merge: bool = True) -> SeriesMeta:
    """Store a frame. Several contracts in one frame are stitched into a continuous series."""
    spec = registry().get(symbol)
    rolls = []
    if "contract" in df.columns and df["contract"].nunique() > 1:
        mask = outright_mask(df["contract"], spec.symbol)
        if mask.any():
            df = df[mask]
        df, rolls = build_continuous(df, spec)
    elif "contract" not in df.columns:
        df = df.assign(contract=spec.symbol)
    base_tf = detect_base_tf(df["ts"].to_numpy())
    if merge and store().has(spec.symbol):
        old = store().meta(spec.symbol)
        if old.base_tf != base_tf:
            raise ValueError(f"{spec.symbol} already stored as {old.base_tf} bars; this file is {base_tf}. "
                             f"Delete the existing series first or import a {old.base_tf} file.")
        if old.synthetic:
            merge = False  # never mix real data with synthetic data
    meta = SeriesMeta(symbol=spec.symbol, source=source, base_tf=base_tf, synthetic=False, rolls=rolls)
    return store().write(spec.symbol, df, meta, merge=merge)


def import_csv(path: str | Path, symbol: str, fmt: str = "auto", tz: str = "UTC",
               bar_time: str = "open", merge: bool = True) -> SeriesMeta:
    df = read_csv_bars(path, fmt=fmt, tz=tz, bar_time=bar_time)
    return import_frame(symbol, df, source=f"csv:{Path(path).name}", merge=merge)


# ---- Databento ----------------------------------------------------------------------------------

def _databento():
    try:
        import databento as db  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("The 'databento' package is not installed. Run:  pip install databento") from e
    return db


def _databento_frame(df: pd.DataFrame) -> pd.DataFrame:
    df = df.reset_index()
    tcol = "ts_event" if "ts_event" in df.columns else df.columns[0]
    return pd.DataFrame({
        "ts": dt_to_epoch(df[tcol]),
        "open": df["open"].astype(float).to_numpy(),
        "high": df["high"].astype(float).to_numpy(),
        "low": df["low"].astype(float).to_numpy(),
        "close": df["close"].astype(float).to_numpy(),
        "volume": df["volume"].astype(float).to_numpy(),
        "contract": df["symbol"].astype(str).to_numpy(),
    })


def databento_cost(symbol: str, start: str, end: str, api_key: str, schema: str = "ohlcv-1m") -> float:
    db = _databento()
    client = db.Historical(api_key)
    return float(client.metadata.get_cost(dataset="GLBX.MDP3", symbols=[f"{symbol}.FUT"],
                                          stype_in="parent", schema=schema, start=start, end=end))


def import_databento(symbol: str, start: str, end: str, api_key: str, schema: str = "ohlcv-1m") -> SeriesMeta:
    """Download all outright contracts of a CME root (e.g. MES.FUT) and stitch a continuous series."""
    db = _databento()
    client = db.Historical(api_key)
    data = client.timeseries.get_range(dataset="GLBX.MDP3", symbols=[f"{symbol}.FUT"], stype_in="parent",
                                       schema=schema, start=start, end=end)
    df = _databento_frame(data.to_df())
    return import_frame(symbol, df, source=f"databento:{schema}")


def import_dbn_file(path: str | Path, symbol: str) -> SeriesMeta:
    db = _databento()
    data = db.DBNStore.from_file(str(path))
    return import_frame(symbol, _databento_frame(data.to_df()), source=f"databento-file:{Path(path).name}")


# ---- Binance USD-M perpetuals (public, no key) -------------------------------------------------

BINANCE_FAPI = "https://fapi.binance.com"


def parse_binance_klines(rows: list[list], point_value: float) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "contract"])
    a = np.array(rows, dtype=object)
    return pd.DataFrame({
        "ts": a[:, 0].astype("int64") // 1000,
        "open": a[:, 1].astype(float),
        "high": a[:, 2].astype(float),
        "low": a[:, 3].astype(float),
        "close": a[:, 4].astype(float),
        # base-asset volume -> contracts
        "volume": np.round(a[:, 5].astype(float) / point_value),
    })


def import_binance(symbol: str = "BTCUSDT", days: int = 30, progress=print) -> SeriesMeta:
    import httpx

    spec = registry().get(symbol)
    end_ms = int(time.time() // 60 * 60 * 1000) - 60_000
    start_ms = end_ms - days * 86_400_000
    frames = []
    cursor = start_ms
    with httpx.Client(timeout=30) as client:
        while cursor < end_ms:
            r = client.get(f"{BINANCE_FAPI}/fapi/v1/klines",
                           params={"symbol": symbol, "interval": "1m", "startTime": cursor,
                                   "endTime": end_ms, "limit": 1500})
            r.raise_for_status()
            rows = r.json()
            if not rows:
                break
            frames.append(parse_binance_klines(rows, spec.point_value))
            cursor = int(rows[-1][0]) + 60_000
            progress(f"  {symbol}: {datetime.fromtimestamp(cursor / 1000, tz=timezone.utc):%Y-%m-%d %H:%M} UTC")
            time.sleep(0.25)
    df = pd.concat(frames) if frames else parse_binance_klines([], spec.point_value)
    df["contract"] = symbol
    return import_frame(symbol, df, source="binance:klines-1m")
