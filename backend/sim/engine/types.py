from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    TRAILING_STOP = "trailing_stop"


class TIF(str, Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"


class Status(str, Enum):
    PENDING = "pending"      # bracket child waiting for its parent to fill
    WORKING = "working"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"

    @property
    def is_open(self) -> bool:
        return self in (Status.PENDING, Status.WORKING)


@dataclass
class Order:
    id: int
    account: str
    symbol: str
    side: Side
    qty: int
    type: OrderType
    price: float | None = None        # limit price
    stop: float | None = None         # stop trigger (stop, stop-limit, trailing current stop)
    trail_ticks: int | None = None
    tif: TIF = TIF.GTC
    reduce_only: bool = False
    parent_id: int | None = None
    oco: str | None = None            # orders sharing an OCO group cancel each other
    tag: str = "manual"               # manual | strategy:<name> | bot | tp | sl | flatten | liquidation | roll
    status: Status = Status.WORKING
    filled_qty: int = 0
    avg_fill: float | None = None
    created_ts: int = 0
    updated_ts: int = 0
    reason: str = ""
    triggered: bool = False           # stop-limit has triggered
    trail_anchor: float | None = None
    armed: bool = False               # has seen at least one price since submission/modification
    child_ids: list[int] = field(default_factory=list)
    offset_ticks: float | None = None  # bracket leg: distance from the parent's fill, applied on fill
    active_ts: float = 0.0             # with latency: the order reaches the market at this time

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value
        d["type"] = self.type.value
        d["tif"] = self.tif.value
        d["status"] = self.status.value
        return d


@dataclass
class Fill:
    id: int
    order_id: int
    account: str
    symbol: str
    side: Side
    qty: int
    price: float
    fee: float
    ts: int
    realized: float          # realized P&L from this fill (before fees)
    position_after: int
    tag: str = ""
    contract: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value
        return d


@dataclass
class Position:
    symbol: str
    qty: int = 0
    avg_price: float = 0.0
    realized: float = 0.0     # realized P&L since the position record was created (session lifetime)
    open_ts: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Trade:
    """A flat-to-flat round trip."""

    id: int
    account: str
    symbol: str
    side: str                  # long | short
    qty: int                   # max absolute position during the trade
    entry_price: float
    entry_ts: int
    exit_price: float | None = None
    exit_ts: int | None = None
    pnl: float = 0.0           # gross realized
    fees: float = 0.0
    net: float = 0.0
    mae_ticks: float = 0.0     # maximum adverse excursion (ticks, per contract)
    mfe_ticks: float = 0.0     # maximum favourable excursion
    exit_reason: str = ""
    entry_tag: str = ""
    # internal accumulators
    _entry_qty: int = 0
    _entry_value: float = 0.0
    _exit_qty: int = 0
    _exit_value: float = 0.0

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if not k.startswith("_")}
