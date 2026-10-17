"""In-memory bar container and session-aware resampling."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..contracts import SessionSpec

TIMEFRAMES = ["1m", "2m", "3m", "5m", "10m", "15m", "30m", "1h", "2h", "4h", "1D", "1W"]


def tf_minutes(tf: str) -> int:
    tf = tf.strip()
    unit = tf[-1]
    n = int(tf[:-1] or 1)
    if unit == "m":
        return n
    if unit in ("h", "H"):
        return n * 60
    if unit in ("D", "d"):
        return n * 1440
    if unit in ("W", "w"):
        return n * 10080
    raise ValueError(f"Unknown timeframe '{tf}'")


@dataclass
class Bars:
    """Columnar OHLCV bars. ts = bar OPEN time, UTC epoch seconds."""

    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    contract: np.ndarray | None = None  # int16 codes into `contracts`
    contracts: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return int(self.ts.shape[0])

    @classmethod
    def empty(cls) -> "Bars":
        z = np.zeros(0)
        return cls(np.zeros(0, dtype="int64"), z, z, z, z, z, np.zeros(0, dtype="int16"), [])

    def slice(self, i: int, j: int) -> "Bars":
        c = self.contract[i:j] if self.contract is not None else None
        return Bars(self.ts[i:j], self.open[i:j], self.high[i:j], self.low[i:j], self.close[i:j],
                    self.volume[i:j], c, self.contracts)

    def between(self, start: int | None = None, end: int | None = None) -> "Bars":
        """Bars with start <= ts < end."""
        i = 0 if start is None else int(np.searchsorted(self.ts, start, side="left"))
        j = len(self) if end is None else int(np.searchsorted(self.ts, end, side="left"))
        return self.slice(i, j)

    def tail(self, n: int) -> "Bars":
        return self.slice(max(0, len(self) - n), len(self))

    def contract_at(self, i: int) -> str | None:
        if self.contract is None or not self.contracts:
            return None
        return self.contracts[int(self.contract[i])]

    def shifted(self, offsets: np.ndarray) -> "Bars":
        return Bars(self.ts, self.open + offsets, self.high + offsets, self.low + offsets,
                    self.close + offsets, self.volume, self.contract, self.contracts)

    def to_records(self, decimals: int = 2) -> list[dict]:
        o, h, l, c = (np.round(a, decimals) for a in (self.open, self.high, self.low, self.close))
        return [
            {"time": int(t), "open": float(a), "high": float(b), "low": float(d), "close": float(e),
             "volume": float(v)}
            for t, a, b, d, e, v in zip(self.ts, o, h, l, c, self.volume)
        ]

    def to_columns(self, decimals: int = 2) -> dict:
        return {
            "time": self.ts.astype("int64").tolist(),
            "open": np.round(self.open, decimals).tolist(),
            "high": np.round(self.high, decimals).tolist(),
            "low": np.round(self.low, decimals).tolist(),
            "close": np.round(self.close, decimals).tolist(),
            "volume": self.volume.astype("float64").tolist(),
        }


def bucket_keys(ts: np.ndarray, session: SessionSpec, tf: str) -> np.ndarray:
    """Bucket start time (UTC epoch seconds) for each bar timestamp.

    Intraday buckets are aligned to the session open (CME: 17:00 CT, so 4h bars start at
    17:00, 21:00, 01:00, ...). Daily bars are keyed by trade date (00:00 UTC of that date, which is
    what charts expect for daily data); weekly bars by the Monday of the trade week.
    """
    m = tf_minutes(tf)
    if m < 1440:
        return session.bucket_starts(ts, m)
    td_ns = session.trade_dates_ns(ts)
    days = td_ns // 86_400_000_000_000  # days since epoch
    if m == 1440:
        return days * 86400
    if m == 10080:
        weekday = (days + 3) % 7  # 1970-01-01 was a Thursday -> Monday = 0
        return (days - weekday) * 86400
    raise ValueError(f"Unsupported timeframe {tf}")


def resample(bars: Bars, session: SessionSpec, tf: str) -> Bars:
    if len(bars) == 0:
        return Bars.empty()
    keys = bucket_keys(bars.ts, session, tf)
    starts = np.flatnonzero(np.r_[True, keys[1:] != keys[:-1]])
    ends = np.r_[starts[1:], len(bars)] - 1
    contract = bars.contract[ends] if bars.contract is not None else None
    return Bars(
        ts=keys[starts].astype("int64"),
        open=bars.open[starts],
        high=np.maximum.reduceat(bars.high, starts),
        low=np.minimum.reduceat(bars.low, starts),
        close=bars.close[ends],
        volume=np.add.reduceat(bars.volume, starts),
        contract=contract,
        contracts=bars.contracts,
    )


class BarBuilder:
    """Incrementally aggregates base bars / price points into one timeframe (used for replay & live).

    `update()` returns the current state of the bucket the point belongs to; `closed` holds the
    previous bucket when a new one starts.
    """

    def __init__(self, session: SessionSpec, tf: str) -> None:
        self.session = session
        self.tf = tf
        self.minutes = tf_minutes(tf)
        self.current: dict | None = None
        self._key_cache: tuple[int, int] | None = None  # (minute_ts, key)

    def key(self, ts: int) -> int:
        minute = ts - ts % 60
        if self._key_cache and self._key_cache[0] == minute:
            return self._key_cache[1]
        k = int(bucket_keys(np.array([minute], dtype="int64"), self.session, self.tf)[0])
        self._key_cache = (minute, k)
        return k

    def update(self, ts: int, o: float, h: float, l: float, c: float, v: float) -> tuple[dict | None, dict]:
        """Merge a (partial) bar into the bucket. Returns (closed_bucket_or_None, current_bucket)."""
        k = self.key(ts)
        closed = None
        cur = self.current
        if cur is None or cur["time"] != k:
            closed = cur
            cur = {"time": k, "open": o, "high": h, "low": l, "close": c, "volume": v}
            self.current = cur
        else:
            cur["high"] = max(cur["high"], h)
            cur["low"] = min(cur["low"], l)
            cur["close"] = c
            cur["volume"] += v
        return closed, cur

    def point(self, ts: int, price: float, volume: float = 0.0) -> tuple[dict | None, dict]:
        """Merge one price point (trade/tick or intrabar path point)."""
        k = self.key(ts)
        closed = None
        cur = self.current
        if cur is None or cur["time"] != k:
            closed = cur
            cur = {"time": k, "open": price, "high": price, "low": price, "close": price, "volume": volume}
            self.current = cur
        else:
            if price > cur["high"]:
                cur["high"] = price
            if price < cur["low"]:
                cur["low"] = price
            cur["close"] = price
            cur["volume"] += volume
        return closed, cur

    def seed(self, bar: dict | None) -> None:
        self.current = dict(bar) if bar else None
