"""Contract registry and trading-session calendar.

All timestamps in the system are UTC epoch seconds (int). Sessions are defined in the exchange's
time zone; a CME equity session opens at 17:00 CT the previous evening and closes at 16:00 CT, and
its *trade date* is the day it closes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yaml

from .config import settings

_DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MONTH_CODES = "FGHJKMNQUVXZ"  # Jan..Dec


def _parse_hhmm(value: str | None) -> timedelta | None:
    if value is None:
        return None
    hh, mm = str(value).split(":")
    return timedelta(hours=int(hh), minutes=int(mm))


@dataclass(frozen=True)
class SessionSpec:
    name: str
    tz: str
    open: timedelta
    close: timedelta
    open_days: tuple[int, ...]  # weekday() numbers on which a session OPENS
    rth: tuple[timedelta, timedelta]
    day_margin_end: timedelta | None

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def overnight(self) -> bool:
        """True when the session opens on the previous calendar day (open time after close time)."""
        return self.open >= self.close and not self.is_24h

    @property
    def is_24h(self) -> bool:
        return self.open == timedelta(0) and self.close == timedelta(hours=24)

    # ---- scalar helpers -------------------------------------------------------------------
    def local(self, ts: int) -> datetime:
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(self.zone)

    def trade_date(self, ts: int) -> date:
        loc = self.local(ts)
        tod = timedelta(hours=loc.hour, minutes=loc.minute, seconds=loc.second)
        if self.overnight and tod >= self.open:
            return loc.date() + timedelta(days=1)
        return loc.date()

    def session_start(self, trade_date: date) -> int:
        base = datetime.combine(trade_date, time(0), tzinfo=self.zone)
        start = base + self.open - (timedelta(days=1) if self.overnight else timedelta(0))
        return int(_fix_local(start, self.zone).timestamp())

    def session_end(self, trade_date: date) -> int:
        base = datetime.combine(trade_date, time(0), tzinfo=self.zone)
        return int(_fix_local(base + self.close, self.zone).timestamp())

    def time_of_day(self, ts: int) -> timedelta:
        loc = self.local(ts)
        return timedelta(hours=loc.hour, minutes=loc.minute, seconds=loc.second)

    def in_session(self, ts: int) -> bool:
        if self.is_24h:
            return True
        loc = self.local(ts)
        tod = timedelta(hours=loc.hour, minutes=loc.minute, seconds=loc.second)
        if self.overnight:
            if tod >= self.open:
                return loc.weekday() in self.open_days
            if tod < self.close:
                return (loc - timedelta(days=1)).weekday() in self.open_days
            return False
        return self.open <= tod < self.close and loc.weekday() in self.open_days

    def is_day_margin(self, ts: int) -> bool:
        """True while intraday (day-trade) margin applies."""
        if self.day_margin_end is None:
            return True
        if not self.in_session(ts):
            return False
        tod = self.time_of_day(ts)
        if self.overnight:
            # From the open (previous evening) until day_margin_end on the trade date.
            return tod >= self.open or tod < self.day_margin_end
        return self.open <= tod < self.day_margin_end

    def state(self, ts: int) -> tuple[bool, str, bool, int]:
        """(in_session, trade_date ISO, day_margin, valid_until_ts): the state holds until valid_until."""
        in_s = self.in_session(ts)
        td = self.trade_date(ts).isoformat()
        day = self.is_day_margin(ts) if in_s else False
        loc = self.local(ts).date()
        nxt = None
        for k in (-1, 0, 1, 2):
            d = loc + timedelta(days=k)
            for off in (self.open, self.close, self.day_margin_end):
                if off is None:
                    continue
                b = int(_fix_local(datetime.combine(d, time(0), tzinfo=self.zone) + off, self.zone).timestamp())
                if b > ts and (nxt is None or b < nxt):
                    nxt = b
        return in_s, td, day, nxt if nxt is not None else ts + 60

    def is_rth(self, ts: int) -> bool:
        tod = self.time_of_day(ts)
        return self.rth[0] <= tod < self.rth[1]

    # ---- vectorised helpers ----------------------------------------------------------------
    def trade_dates_ns(self, ts: np.ndarray) -> np.ndarray:
        """Trade date (as int64 ns at local midnight, naive) for an array of UTC epoch seconds."""
        naive = _naive_local(ts, self.tz)
        midnight = naive.astype("datetime64[D]").astype("datetime64[ns]")
        if self.overnight:
            tod = naive - midnight
            after = tod >= np.timedelta64(int(self.open.total_seconds()), "s")
            midnight = midnight + after.astype("int64") * np.timedelta64(1, "D")
        return midnight.astype("int64")

    def in_session_mask(self, ts: np.ndarray) -> np.ndarray:
        """Vectorised in_session for an array of UTC epoch seconds."""
        ts = np.asarray(ts, dtype="int64")
        if self.is_24h:
            return np.ones(len(ts), dtype=bool)
        naive = _naive_local(ts, self.tz)
        days = naive.astype("datetime64[D]")
        tod = (naive - days.astype("datetime64[ns]")).astype("int64") // 1_000_000_000
        wd = (days.astype("int64") + 3) % 7  # 1970-01-01 was a Thursday; Monday = 0
        o, c = int(self.open.total_seconds()), int(self.close.total_seconds())
        days_ok = np.isin(wd, self.open_days)
        prev_ok = np.isin((wd - 1) % 7, self.open_days)
        if self.overnight:
            return ((tod >= o) & days_ok) | ((tod < c) & prev_ok)
        return (tod >= o) & (tod < c) & days_ok

    def bucket_starts(self, ts: np.ndarray, minutes: int) -> np.ndarray:
        """Session-aligned bucket start (UTC epoch seconds) for intraday timeframes."""
        ts = np.asarray(ts, dtype="int64")
        naive = _naive_local(ts, self.tz).astype("int64") // 1_000_000_000  # local wall-clock secs
        td = self.trade_dates_ns(ts) // 1_000_000_000
        open_s = int(self.open.total_seconds())
        start = td + open_s - (86400 if self.overnight else 0)
        step = minutes * 60
        offset = naive - start
        bucket_local = start + (offset // step) * step
        # local -> utc shift is constant inside one bucket (DST changes happen while CME is closed)
        return ts - (naive - bucket_local)


def _fix_local(dt: datetime, zone: ZoneInfo) -> datetime:
    # Re-normalise after timedelta arithmetic across DST changes.
    naive = dt.replace(tzinfo=None)
    return naive.replace(tzinfo=zone)


def _naive_local(ts: np.ndarray, tz: str) -> np.ndarray:
    idx = pd.DatetimeIndex(pd.to_datetime(np.asarray(ts, dtype="int64"), unit="s", utc=True))
    return idx.tz_convert(tz).tz_localize(None).values.astype("datetime64[ns]")


@dataclass(frozen=True)
class Margin:
    intraday: float = 0.0
    initial: float = 0.0
    maintenance: float = 0.0
    initial_rate: float = 0.0  # fraction of notional (crypto, forex, stocks)
    maintenance_rate: float = 0.0
    intraday_rate: float = 0.0  # fraction of notional while day margin applies (stocks: 4x day trading)


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    name: str
    exchange: str
    tick_size: float
    tick_value: float
    session: SessionSpec
    months: str = "HMUZ"
    roll_days: int = 8
    perpetual: bool = False
    margin: Margin = field(default_factory=Margin)
    fees_per_side: float = 0.0
    fees_per_side_rate: float = 0.0
    spread_rth: float = 1.0            # typical bid-ask spread in ticks, regular hours
    spread_eth: float = 1.0            # overnight / extended hours
    spread_open: float = 2.0           # first minutes after the regular-hours open
    spread_open_minutes: int = 1
    synthetic: dict = field(default_factory=dict)
    asset_class: str = "future"        # future | crypto | forex | stock | etf
    unit: str = "contract"             # what one unit of quantity is, for display
    quote: str = "USD"                 # currency that prices (and tick_value) are quoted in
    data: dict = field(default_factory=dict)  # free data source settings (dukascopy code, etc.)

    @property
    def point_value(self) -> float:
        """Value of a 1.0 price move for one unit of quantity, in the QUOTE currency."""
        return self.tick_value / self.tick_size

    @property
    def inverse_quote(self) -> bool:
        """USD-base forex pairs (USDJPY, USDCAD, USDCHF): profit is in the quote currency and is
        converted to USD at the current price."""
        return self.quote != "USD"

    def usd_per_point(self, price: float) -> float:
        """USD value of a 1.0 price move for one unit, at `price`."""
        if self.inverse_quote:
            return self.point_value / price if price else 0.0
        return self.point_value

    def tick_value_usd(self, price: float) -> float:
        return self.usd_per_point(price) * self.tick_size

    def notional_usd(self, qty: float, price: float) -> float:
        return abs(qty) * price * self.usd_per_point(price)

    @property
    def price_decimals(self) -> int:
        s = f"{self.tick_size:.10f}".rstrip("0")
        return len(s.split(".")[1]) if "." in s else 0

    def round_price(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, self.price_decimals)

    def fee(self, qty: int, price: float) -> float:
        return abs(qty) * self.fees_per_side + self.notional_usd(qty, price) * self.fees_per_side_rate

    def margin_per_contract(self, price: float, day: bool) -> float:
        if self.margin.initial_rate:
            rate = self.margin.intraday_rate if (day and self.margin.intraday_rate) else self.margin.initial_rate
            return self.notional_usd(1, price) * rate
        return self.margin.intraday if day else self.margin.initial

    def maintenance_per_contract(self, price: float, day: bool) -> float:
        if self.margin.maintenance_rate:
            return self.notional_usd(1, price) * self.margin.maintenance_rate
        return self.margin.intraday if day else self.margin.maintenance

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "exchange": self.exchange,
            "tick_size": self.tick_size,
            "tick_value": self.tick_value,
            "point_value": self.point_value,
            "price_decimals": self.price_decimals,
            "session": {
                "name": self.session.name,
                "tz": self.session.tz,
                "open": _fmt_td(self.session.open),
                "close": _fmt_td(self.session.close),
                "rth": [_fmt_td(self.session.rth[0]), _fmt_td(self.session.rth[1])],
                "day_margin_end": _fmt_td(self.session.day_margin_end) if self.session.day_margin_end else None,
            },
            "months": self.months,
            "perpetual": self.perpetual,
            "margin": self.margin.__dict__,
            "fees_per_side": self.fees_per_side,
            "fees_per_side_rate": self.fees_per_side_rate,
            "asset_class": self.asset_class,
            "unit": self.unit,
            "quote": self.quote,
            "spread_ticks": {"rth": self.spread_rth, "eth": self.spread_eth, "open": self.spread_open,
                             "open_minutes": self.spread_open_minutes},
        }


def _fmt_td(td: timedelta) -> str:
    total = int(td.total_seconds()) // 60
    return f"{total // 60:02d}:{total % 60:02d}"


class ContractRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.config_dir / "contracts.yaml"
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        self.sessions: dict[str, SessionSpec] = {}
        for name, s in raw.get("sessions", {}).items():
            self.sessions[name] = SessionSpec(
                name=name,
                tz=s["tz"],
                open=_parse_hhmm(s["open"]),
                close=_parse_hhmm(s["close"]),
                open_days=tuple(_DAY_NAMES.index(d) for d in s.get("open_days", _DAY_NAMES)),
                rth=(_parse_hhmm(s["rth"][0]), _parse_hhmm(s["rth"][1])),
                day_margin_end=_parse_hhmm(s.get("day_margin_end")),
            )
        self.contracts: dict[str, ContractSpec] = {}
        for sym, c in raw.get("contracts", {}).items():
            if sym.startswith("_"):  # YAML anchors / templates
                continue
            sym = str(sym).upper()
            self.contracts[sym] = ContractSpec(
                symbol=sym,
                name=c.get("name", sym),
                exchange=c.get("exchange", ""),
                tick_size=float(c["tick_size"]),
                tick_value=float(c["tick_value"]),
                session=self.sessions[c["session"]],
                months=c.get("months", "HMUZ") or "",
                roll_days=int(c.get("roll_days", 8)),
                perpetual=bool(c.get("perpetual", False)),
                margin=Margin(**{k: float(v) for k, v in (c.get("margin") or {}).items()}),
                fees_per_side=float(c.get("fees_per_side", 0.0)),
                fees_per_side_rate=float(c.get("fees_per_side_rate", 0.0)),
                spread_rth=float((c.get("spread_ticks") or {}).get("rth", 1)),
                spread_eth=float((c.get("spread_ticks") or {}).get("eth", 1)),
                spread_open=float((c.get("spread_ticks") or {}).get("open", 2)),
                spread_open_minutes=int((c.get("spread_ticks") or {}).get("open_minutes", 1)),
                synthetic=c.get("synthetic") or {},
                asset_class=c.get("asset_class") or ("crypto" if c.get("perpetual") else "future"),
                unit=c.get("unit") or "contract",
                quote=str(c.get("quote") or "USD").upper(),
                data=c.get("data") or {},
            )

    def get(self, symbol: str) -> ContractSpec:
        root = root_symbol(symbol)
        if root not in self.contracts:
            raise KeyError(f"Unknown contract '{symbol}'. Add it to {self.path}.")
        return self.contracts[root]

    def __contains__(self, symbol: str) -> bool:
        return root_symbol(symbol) in self.contracts

    def all(self) -> list[ContractSpec]:
        return list(self.contracts.values())


def root_symbol(symbol: str) -> str:
    """'MESZ6' -> 'MES', 'MES' -> 'MES', 'BTCUSDT' -> 'BTCUSDT'."""
    s = symbol.upper().replace("1!", "")
    m = re.match(r"^(.+?)[FGHJKMNQUVXZ]\d{1,2}$", s)
    return m.group(1) if m else s


@lru_cache(maxsize=1)
def registry() -> ContractRegistry:
    return ContractRegistry()


# ---- expiries and rolls -------------------------------------------------------------------

def third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def contract_expiry(spec: ContractSpec, year: int, month: int) -> date:
    # Equity index futures expire on the third Friday; others are approximated the same way.
    return third_friday(year, month)


def contract_code(root: str, year: int, month: int) -> str:
    return f"{root}{MONTH_CODES[month - 1]}{year % 10}"


def contract_cycle(spec: ContractSpec, start: date, end: date) -> list[tuple[str, date, date]]:
    """List of (code, roll_date, expiry) covering [start, end]. roll_date = first trade date on the
    NEXT contract, i.e. the continuous series uses a contract for trade dates < its roll date."""
    months = [MONTH_CODES.index(m) + 1 for m in spec.months]
    out = []
    for year in range(start.year - 1, end.year + 2):
        for m in months:
            exp = contract_expiry(spec, year, m)
            roll = exp - timedelta(days=spec.roll_days)  # e.g. ES/MES: Thursday, 8 days before expiry
            out.append((contract_code(spec.symbol, year, m), roll, exp))
    out.sort(key=lambda x: x[2])
    return [c for c in out if c[2] >= start - timedelta(days=120) and c[1] <= end + timedelta(days=120)]

