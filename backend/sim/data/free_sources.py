"""Free historical data for crypto, forex, gold, stocks and ETFs.

    crypto        Binance public archive (data.binance.vision): years of 1-minute USD-M perpetual bars,
                  topped up to the current minute with the public REST API. No key.
    forex, gold   Dukascopy (datafeed.dukascopy.com): 1-minute bid and ask candles, stored as the mid
                  price; the measured spread is reported so the contract's spread can be tuned. No key.
    stocks, ETFs  Alpaca market data (free account key, years of 1-minute bars incl. extended hours,
                  split-adjusted), or Yahoo Finance without a key (1-minute bars for the last ~30 days,
                  5-minute for 60 days, hourly for 2 years).

All downloads land in the bar store as one continuous series per symbol (no rolls) and merge with
what is already stored, so running a download again only adds the missing part.
"""

from __future__ import annotations

import io
import lzma
import os
import struct
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import numpy as np
import pandas as pd

from ..contracts import ContractSpec, registry
from .store import SeriesMeta, store

Progress = Callable[[str, float | None], None]  # message, fraction done (0..1) or None

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) trading-env-sim/1.0"}
COLS = ["ts", "open", "high", "low", "close", "volume"]


def _noop(msg: str, frac: float | None = None) -> None:
    pass


def _client(timeout: float = 60):
    import httpx
    return httpx.Client(timeout=timeout, headers=UA, follow_redirects=True,
                        limits=httpx.Limits(max_connections=16, max_keepalive_connections=16))


def _get(client, url: str, params: dict | None = None, headers: dict | None = None, tries: int = 4):
    """GET with retries on network errors, 429 and 5xx. Returns the response (404s are returned)."""
    last = None
    for i in range(tries):
        try:
            r = client.get(url, params=params, headers=headers)
            if r.status_code == 429 or r.status_code >= 500:
                last = RuntimeError(f"HTTP {r.status_code} from {url}")
                time.sleep(min(30, 2 ** i * (3 if r.status_code == 429 else 1)))
                continue
            return r
        except Exception as e:  # network error: retry
            last = e
            time.sleep(2 ** i)
    raise RuntimeError(f"Download failed: {url}: {last}")


def _day(s: str | date | None, default: date) -> date:
    if s is None or s == "":
        return default
    if isinstance(s, date):
        return s
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def _epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _empty() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in COLS})


def _save(spec: ContractSpec, df: pd.DataFrame, source: str, notes: str = "") -> SeriesMeta:
    if df is None or not len(df):
        raise RuntimeError(f"No bars were downloaded for {spec.symbol}: check the dates and your internet connection")
    df = df[COLS].copy()
    df["contract"] = spec.symbol
    # keep bars inside the instrument's trading hours (e.g. Dukascopy fills weekends with flat candles)
    sess = spec.session
    if not sess.is_24h:
        df = df[sess.in_session_mask(df["ts"].to_numpy())]
    merge = store().has(spec.symbol)
    if merge:
        old = store().meta(spec.symbol)
        if old.synthetic or old.source != source:
            merge = False  # never mix sample data, other bar sizes or another vendor's prices
    if merge and _tf_of(df) != store().meta(spec.symbol).base_tf:
        merge = False
    meta = SeriesMeta(symbol=spec.symbol, source=source, base_tf=_tf_of(df), synthetic=False, notes=notes)
    return store().write(spec.symbol, df, meta, merge=merge)


def _tf_of(df: pd.DataFrame) -> str:
    from .importers import detect_base_tf
    return detect_base_tf(df["ts"].to_numpy())


# ==== Binance (crypto) =========================================================================

BINANCE_VISION = "https://data.binance.vision/data/futures/um"


def parse_binance_csv(raw: bytes, point_value: float) -> pd.DataFrame:
    """Binance archive kline CSV (with or without a header row; ms or us timestamps)."""
    text = raw.decode("utf-8", "replace")
    if not text.strip():
        return _empty()
    first = text.split("\n", 1)[0]
    header = 0 if first[:1].isalpha() else None
    df = pd.read_csv(io.StringIO(text), header=header)
    df = df.iloc[:, :6]
    df.columns = COLS
    ts = pd.to_numeric(df["ts"], errors="coerce").astype("int64").to_numpy()
    ts = np.where(ts > 1e14, ts // 1_000_000, ts // 1000)  # microseconds (2025+ spot) or milliseconds
    out = pd.DataFrame({"ts": ts})
    for c in COLS[1:]:
        out[c] = pd.to_numeric(df[c], errors="coerce").to_numpy()
    out["volume"] = np.round(out["volume"] / point_value)  # base-asset volume -> contracts
    return out.dropna()


def _zip_csv(content: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        name = [n for n in z.namelist() if n.endswith(".csv")][0]
        return z.read(name)


def _months(start: date, end: date) -> list[tuple[int, int]]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def download_binance(symbol: str, start=None, end=None, progress: Progress = _noop) -> SeriesMeta:
    spec = registry().get(symbol)
    code = (spec.data or {}).get("binance", spec.symbol)
    today = datetime.now(timezone.utc).date()
    start = _day(start, today - timedelta(days=365 * 2))
    end = min(_day(end, today), today)
    frames = []
    months = _months(start, end)
    with _client() as client:
        def fetch(url: str) -> pd.DataFrame | None:
            r = _get(client, url)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return parse_binance_csv(_zip_csv(r.content), spec.point_value)

        done = 0
        for (y, m) in months:
            month_end = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
            df = None
            if month_end < today.replace(day=1):  # completed month: one monthly file
                df = fetch(f"{BINANCE_VISION}/monthly/klines/{code}/1m/{code}-1m-{y}-{m:02d}.zip")
            if df is None:  # current month, or monthly file not published yet: daily files
                days = [date(y, m, d) for d in range(1, month_end.day + 1)]
                days = [d for d in days if start <= d < today]
                with ThreadPoolExecutor(8) as ex:
                    parts = list(ex.map(lambda d: fetch(
                        f"{BINANCE_VISION}/daily/klines/{code}/1m/{code}-1m-{d:%Y-%m-%d}.zip"), days))
                parts = [p for p in parts if p is not None and len(p)]
                df = pd.concat(parts) if parts else None
            if df is not None and len(df):
                frames.append(df)
            done += 1
            progress(f"{spec.symbol}: {y}-{m:02d} ({sum(len(f) for f in frames):,} bars)", done / len(months))

        # top up today's bars from the live REST API (optional: may be blocked in some regions)
        try:
            from .importers import BINANCE_FAPI, parse_binance_klines
            cursor = _epoch(today) * 1000
            if frames:
                cursor = max(cursor, int(max(f["ts"].max() for f in frames)) * 1000 + 60_000)
            now_ms = int(time.time() // 60 * 60 * 1000) - 60_000
            while end >= today and cursor < now_ms:
                r = _get(client, f"{BINANCE_FAPI}/fapi/v1/klines",
                         params={"symbol": code, "interval": "1m", "startTime": cursor, "limit": 1500})
                r.raise_for_status()
                rows = r.json()
                if not rows:
                    break
                frames.append(parse_binance_klines(rows, spec.point_value).drop(columns=["contract"], errors="ignore"))
                cursor = int(rows[-1][0]) + 60_000
        except Exception as e:  # archive data is still saved
            progress(f"{spec.symbol}: live top-up skipped ({e})", None)

    df = pd.concat(frames) if frames else _empty()
    df = df[(df["ts"] >= _epoch(start)) & (df["ts"] < _epoch(end + timedelta(days=1)))]
    return _save(spec, df, "binance:um-1m")


# ==== Dukascopy (forex, gold) ==================================================================

DUKASCOPY = "https://datafeed.dukascopy.com/datafeed"


def decode_bi5_candles(raw: bytes, day_ts: int, point: float) -> pd.DataFrame:
    """Dukascopy *_candles_min_1.bi5: LZMA-compressed big-endian records of
    (seconds from midnight UTC, open, close, low, high as int32 / point, volume float32)."""
    if not raw:
        return _empty()
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_AUTO)
    try:
        data = dec.decompress(raw)
    except lzma.LZMAError:
        data = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(raw)
    n = len(data) // 24
    if n == 0:
        return _empty()
    a = np.frombuffer(data[: n * 24], dtype=np.dtype([("t", ">i4"), ("o", ">i4"), ("c", ">i4"), ("l", ">i4"),
                                                      ("h", ">i4"), ("v", ">f4")]))
    out = pd.DataFrame({
        "ts": day_ts + a["t"].astype("int64"),
        "open": a["o"] / point, "high": a["h"] / point, "low": a["l"] / point, "close": a["c"] / point,
        "volume": a["v"].astype("float64"),
    })
    live = out["volume"] > 0
    if live.sum() > 10:
        o, c, lo, hi = (out.loc[live, k] for k in ("open", "close", "low", "high"))
        bad = ((lo > np.minimum(o, c) + 1e-9) | (hi < np.maximum(o, c) - 1e-9)).mean()
        if bad > 0.2:
            raise RuntimeError("Unexpected Dukascopy candle layout (low/high do not bracket open/close)")
    return out


def _dukascopy_day(client, code: str, d: date, side: str, point: float) -> pd.DataFrame | None:
    url = f"{DUKASCOPY}/{code}/{d.year:04d}/{d.month - 1:02d}/{d.day:02d}/{side}_candles_min_1.bi5"
    r = _get(client, url)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return decode_bi5_candles(r.content, _epoch(d), point)


def download_dukascopy(symbol: str, start=None, end=None, progress: Progress = _noop,
                       spread: bool = True) -> SeriesMeta:
    """1-minute bars from Dukascopy. With spread=True both bid and ask are downloaded and stored as the
    mid price (orders then pay half the contract's spread, like a real broker); otherwise bid prices."""
    spec = registry().get(symbol)
    d = spec.data or {}
    if "dukascopy" not in d:
        raise RuntimeError(f"{spec.symbol} has no Dukascopy code in contracts.yaml (data: {{dukascopy: ..., point: ...}})")
    code, point = d["dukascopy"], float(d.get("point", 100000))
    today = datetime.now(timezone.utc).date()
    start = _day(start, today - timedelta(days=365 * 2))
    end = min(_day(end, today - timedelta(days=1)), today - timedelta(days=1))  # day files are published after the day
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    days = [x for x in days if x.weekday() != 5]  # Saturday: market closed
    sides = ["BID", "ASK"] if spread else ["BID"]
    bids, asks = [], []
    with _client() as client, ThreadPoolExecutor(8) as ex:
        chunk = 30
        for i in range(0, len(days), chunk):
            part = days[i:i + chunk]
            jobs = [(x, s) for x in part for s in sides]
            res = list(ex.map(lambda js: _dukascopy_day(client, code, js[0], js[1], point), jobs))
            for (x, s), df in zip(jobs, res):
                if df is not None and len(df):
                    (bids if s == "BID" else asks).append(df)
            progress(f"{spec.symbol}: {part[-1]:%Y-%m-%d} ({sum(len(b) for b in bids):,} bars)",
                     min(1.0, (i + len(part)) / max(1, len(days))))
    if not bids:
        return _save(spec, _empty(), "dukascopy:1m")
    bid = pd.concat(bids)
    bid = bid[bid["volume"] > 0]  # Dukascopy fills closed periods with flat zero-volume candles
    bid = bid.drop_duplicates("ts").set_index("ts").sort_index()
    notes, source = "", "dukascopy:bid-1m"
    if asks:
        ask = pd.concat(asks).drop_duplicates("ts").set_index("ts").sort_index().reindex(bid.index)
        ok = ask["close"].notna()
        sp = (ask["close"] - bid["close"])[ok] / spec.tick_size
        mid = bid.copy()
        for c in ["open", "high", "low", "close"]:
            mid.loc[ok, c] = (bid.loc[ok, c] + ask.loc[ok, c]) / 2
        bid = mid
        source = "dukascopy:mid-1m"
        notes = _spread_note(spec, sp)
    # volume is reported in millions of units: convert to quantity units (micro lots of 1,000 / ounces)
    unit = 1.0 if spec.unit == "ounce" else 1000.0
    bid["volume"] = np.round(bid["volume"] * 1e6 / unit, 2)
    df = bid.reset_index()
    for c in ["open", "high", "low", "close"]:
        df[c] = np.round(df[c] / spec.tick_size) * spec.tick_size
    return _save(spec, df, source, notes)


def _spread_note(spec: ContractSpec, sp: pd.Series) -> str:
    if not len(sp):
        return ""
    ts = sp.index.to_numpy()
    rth = np.fromiter((spec.session.is_rth(int(t)) for t in ts[:: max(1, len(ts) // 20000)]), bool)
    sample = sp.to_numpy()[:: max(1, len(ts) // 20000)]
    med_rth = float(np.median(sample[rth])) if rth.any() else float("nan")
    med_eth = float(np.median(sample[~rth])) if (~rth).any() else float("nan")
    return (f"Measured median bid-ask spread: {med_rth:.1f} ticks in regular hours, {med_eth:.1f} ticks outside "
            f"(contract setting: {spec.spread_rth:g} / {spec.spread_eth:g}). Prices are bid/ask mid.")


# ==== Stocks and ETFs ==========================================================================

ALPACA = "https://data.alpaca.markets/v2/stocks/bars"


def alpaca_keys() -> tuple[str, str]:
    return (os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID") or "",
            os.environ.get("ALPACA_API_SECRET") or os.environ.get("APCA_API_SECRET_KEY") or "")


def parse_alpaca_bars(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return _empty()
    from .importers import dt_to_epoch
    t = dt_to_epoch(pd.to_datetime([r["t"] for r in rows], utc=True))
    return pd.DataFrame({"ts": t, "open": [r["o"] for r in rows], "high": [r["h"] for r in rows],
                         "low": [r["l"] for r in rows], "close": [r["c"] for r in rows],
                         "volume": [float(r["v"]) for r in rows]})


def download_alpaca(symbol: str, start=None, end=None, progress: Progress = _noop,
                    key: str | None = None, secret: str | None = None, feed: str = "sip") -> SeriesMeta:
    spec = registry().get(symbol)
    k, s = alpaca_keys()
    key, secret = key or k, secret or s
    if not key or not secret:
        raise RuntimeError("Alpaca needs a free API key: sign up at alpaca.markets, then put ALPACA_API_KEY and "
                           "ALPACA_API_SECRET in .env (or use the Yahoo source, which needs no key).")
    ticker = (spec.data or {}).get("ticker", spec.symbol)
    today = datetime.now(timezone.utc).date()
    start = _day(start, today - timedelta(days=365 * 2))
    end = _day(end, today)
    end_dt = min(datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1),
                 datetime.now(timezone.utc) - timedelta(minutes=16))  # free plan: SIP data is 15 minutes delayed
    params = {"symbols": ticker, "timeframe": "1Min", "start": f"{start:%Y-%m-%d}T00:00:00Z",
              "end": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000, "adjustment": "split", "feed": feed,
              "sort": "asc"}
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    rows: list[dict] = []
    span = max(1.0, (end_dt - datetime(start.year, start.month, start.day, tzinfo=timezone.utc)).total_seconds())
    with _client() as client:
        token = None
        while True:
            p = dict(params, **({"page_token": token} if token else {}))
            r = _get(client, ALPACA, params=p, headers=headers)
            if r.status_code in (401, 403) and feed == "sip" and not rows:
                if r.status_code == 401:
                    raise RuntimeError("Alpaca rejected the API key (401). Check ALPACA_API_KEY / ALPACA_API_SECRET.")
                progress(f"{spec.symbol}: SIP feed not allowed on this plan, using IEX", None)
                return download_alpaca(symbol, start, end, progress, key, secret, feed="iex")
            if r.status_code >= 400:
                raise RuntimeError(f"Alpaca HTTP {r.status_code}: {r.text[:300]}")
            body = r.json()
            page = (body.get("bars") or {}).get(ticker) or []
            rows.extend(page)
            token = body.get("next_page_token")
            if page:
                t = pd.Timestamp(page[-1]["t"]).timestamp()
                progress(f"{spec.symbol}: {pd.Timestamp(page[-1]['t']):%Y-%m-%d} ({len(rows):,} bars)",
                         min(1.0, (t - pd.Timestamp(params['start']).timestamp()) / span))
            if not token:
                break
    return _save(spec, parse_alpaca_bars(rows), f"alpaca:{feed}-1m",
                 "Split-adjusted prices (dividends not adjusted). Includes pre-market and after-hours bars.")


YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
YAHOO_LIMITS = {"1m": (7, 29), "2m": (59, 59), "5m": (59, 59), "15m": (59, 59), "30m": (59, 59),
                "1h": (729, 729), "1d": (36500, 36500)}  # (days per request, max days back)


def parse_yahoo_chart(body: dict) -> pd.DataFrame:
    res = ((body.get("chart") or {}).get("result") or [None])[0]
    if not res or not res.get("timestamp"):
        err = (body.get("chart") or {}).get("error")
        if err:
            raise RuntimeError(f"Yahoo: {err.get('description') or err}")
        return _empty()
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({"ts": np.array(res["timestamp"], dtype="int64"), "open": q.get("open"), "high": q.get("high"),
                       "low": q.get("low"), "close": q.get("close"), "volume": q.get("volume")})
    df["volume"] = df["volume"].fillna(0)
    return df.dropna(subset=["open", "high", "low", "close"])


def download_yahoo(symbol: str, start=None, end=None, progress: Progress = _noop, interval: str = "1m") -> SeriesMeta:
    spec = registry().get(symbol)
    if interval not in YAHOO_LIMITS:
        raise RuntimeError(f"Yahoo interval must be one of {list(YAHOO_LIMITS)}")
    ticker = (spec.data or {}).get("yahoo") or (spec.data or {}).get("ticker", spec.symbol)
    per, back = YAHOO_LIMITS[interval]
    now = int(time.time())
    first = now - back * 86400
    s = max(first, _epoch(_day(start, date(1990, 1, 1))))
    e = min(now, _epoch(_day(end, date.today())) + 86400)
    frames = []
    with _client() as client:
        cur = s
        while cur < e:
            nxt = min(e, cur + per * 86400)
            params = {"period1": cur, "period2": nxt, "interval": interval, "includePrePost": "true",
                      "events": "div,split"}
            r = _get(client, YAHOO + ticker, params=params)
            if r.status_code >= 400:
                r = _get(client, YAHOO.replace("query1", "query2") + ticker, params=params)
            if r.status_code >= 400:
                raise RuntimeError(f"Yahoo HTTP {r.status_code}: {r.text[:200]}")
            frames.append(parse_yahoo_chart(r.json()))
            progress(f"{spec.symbol}: {datetime.fromtimestamp(nxt, timezone.utc):%Y-%m-%d}", (nxt - s) / max(1, e - s))
            cur = nxt
    df = pd.concat(frames) if frames else _empty()
    if interval != "1d":
        df["ts"] = df["ts"] // 60 * 60
    note = ("Yahoo Finance (unofficial, personal use). " +
            ("1-minute bars only reach back about 30 days; add an Alpaca key for years of minute data."
             if interval == "1m" else f"{interval} bars."))
    meta = _save(spec, df, f"yahoo:{interval}", note)
    return meta


# ==== dispatcher ===============================================================================

SOURCES = {
    "binance": {"label": "Binance archive (free, no key)", "classes": ["crypto"]},
    "dukascopy": {"label": "Dukascopy bid/ask (free, no key)", "classes": ["forex"]},
    "alpaca": {"label": "Alpaca (free account key, years of 1-minute bars)", "classes": ["stock", "etf"]},
    "yahoo": {"label": "Yahoo Finance (no key, 1-minute for the last 30 days)", "classes": ["stock", "etf"]},
}


def default_source(spec: ContractSpec) -> str:
    if spec.asset_class == "crypto":
        return "binance"
    if spec.asset_class == "forex":
        return "dukascopy"
    if spec.asset_class in ("stock", "etf"):
        return "alpaca" if all(alpaca_keys()) else "yahoo"
    raise RuntimeError(f"{spec.symbol} is a {spec.asset_class}: use the BARS, Databento or file import instead")


def download(symbol: str, source: str = "auto", start=None, end=None, progress: Progress = _noop,
             **kw) -> SeriesMeta:
    spec = registry().get(symbol)
    src = default_source(spec) if source in (None, "", "auto") else source
    if src == "binance":
        return download_binance(symbol, start, end, progress)
    if src == "dukascopy":
        return download_dukascopy(symbol, start, end, progress, spread=kw.get("spread", True))
    if src == "alpaca":
        return download_alpaca(symbol, start, end, progress, kw.get("key"), kw.get("secret"))
    if src == "yahoo":
        return download_yahoo(symbol, start, end, progress, kw.get("interval") or "1m")
    raise RuntimeError(f"Unknown source '{source}'")
