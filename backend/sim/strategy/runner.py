"""Backtest runner and strategy loader."""

from __future__ import annotations

import importlib.util
import inspect
import itertools
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

import numpy as np

from ..config import settings
from ..contracts import registry
from ..data.bars import bucket_keys, tf_minutes
from ..data.store import store
from ..engine import Engine, FillConfig, RiskConfig
from .base import Bar, Strategy, StrategyContext
from .stats import compute_stats, downsample

ACCOUNT = "BT"


# ---------------------------------------------------------------- loading strategies

def _load_module(path: Path):
    name = f"tsim_strategy_{path.stem}_{abs(hash(str(path.resolve()))) % 10**8}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # fresh import every time, so edits apply without restarting
    return mod


def _strategy_classes(mod) -> list[type[Strategy]]:
    return [c for _, c in inspect.getmembers(mod, inspect.isclass)
            if issubclass(c, Strategy) and c is not Strategy and c.__module__ == mod.__name__]


def resolve_path(ref: str) -> tuple[Path, str | None]:
    """'strategies/x.py', 'x.py', 'x' or 'x.py:ClassName' -> (path, class name)."""
    cls_name = None
    if ":" in ref and not (len(ref) > 1 and ref[1] == ":" and ref.count(":") == 1):
        ref, cls_name = ref.rsplit(":", 1)
    p = Path(ref)
    candidates = [p, settings.strategies_dir / p, settings.strategies_dir / f"{ref}.py", Path(f"{ref}.py")]
    for c in candidates:
        if c.is_file():
            return c, cls_name
    raise FileNotFoundError(f"Strategy '{ref}' not found (looked in {settings.strategies_dir})")


def load_strategy(ref: str) -> type[Strategy]:
    path, cls_name = resolve_path(ref)
    classes = _strategy_classes(_load_module(path))
    if not classes:
        raise ValueError(f"No Strategy subclass found in {path}")
    if cls_name:
        for c in classes:
            if c.__name__ == cls_name:
                return c
        raise ValueError(f"{cls_name} not found in {path}")
    return classes[0]


def list_strategies() -> list[dict]:
    out = []
    d = settings.strategies_dir
    if not d.exists():
        return out
    for f in sorted(d.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            classes = _strategy_classes(_load_module(f))
        except Exception as e:  # show broken files instead of hiding them
            out.append({"id": f.name, "file": str(f), "name": f.stem, "error": f"{type(e).__name__}: {e}"})
            continue
        for c in classes:
            out.append({
                "id": f"{f.name}:{c.__name__}", "file": str(f), "class": c.__name__, "name": c.display_name(),
                "doc": inspect.getdoc(c) or "", "timeframe": c.timeframe, "symbol": c.symbol, "warmup": c.warmup,
                "params": [p.to_dict(k) for k, p in c.param_specs().items()],
            })
    return out


# ---------------------------------------------------------------- running

@dataclass
class BacktestConfig:
    symbol: str = "MES"
    timeframe: str = "5m"
    start: str | int | None = None    # 'YYYY-MM-DD' (trade date, inclusive) or UTC epoch seconds
    end: str | int | None = None      # 'YYYY-MM-DD' (trade date, inclusive) or UTC epoch seconds
    balance: float = 50_000.0
    params: dict = field(default_factory=dict)
    slippage_ticks: float = 0.0       # extra, beyond the bid-ask spread
    limit_through_ticks: float = 1.0
    intrabar: str = "tv"
    fill_resolution: str = "1m"       # '1m' = fills simulated on 1-minute bars; 'chart' = on strategy bars
    roll_fees: bool = True
    spread_model: bool = True
    max_volume_pct: float | None = None
    latency_ms: int = 0
    daily_loss_limit: float | None = None
    max_position: int | None = None
    check_margin: bool = True
    warmup: int | None = None
    close_at_end: bool = True


class Cancelled(Exception):
    pass


def _to_ts(v, spec, is_end: bool) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        return int(v)
    d = date.fromisoformat(str(v)[:10])
    return spec.session.session_end(d) if is_end else spec.session.session_start(d)


class BacktestSim:
    """Historical simulation driver shared by strategy backtests and lock-step external bots.

    `steps()` feeds execution bars to the engine and yields a `Bar` each time a strategy-timeframe bar
    closes; whatever the caller does at that moment (place/cancel orders) is evaluated from the next price.
    """

    def __init__(self, cfg: BacktestConfig, account: str = ACCOUNT) -> None:
        self.t0 = time.time()
        self.cfg = cfg
        self.account = account
        self.spec = registry().get(cfg.symbol)
        self.symbol = self.spec.symbol
        self.meta = store().meta(self.symbol)
        self.tf = cfg.timeframe
        if tf_minutes(self.tf) < tf_minutes(self.meta.base_tf):
            raise ValueError(f"{self.symbol} data is {self.meta.base_tf}; timeframe {self.tf} is finer")
        self.start_ts = _to_ts(cfg.start, self.spec, False)
        self.end_ts = _to_ts(cfg.end, self.spec, True)
        exec_tf = self.tf if cfg.fill_resolution == "chart" else self.meta.base_tf
        self.ex = store().bars(self.symbol, exec_tf).between(self.start_ts, self.end_ts)
        if len(self.ex) == 0:
            raise ValueError("No data in the selected range")
        self.tf_all = store().bars(self.symbol, self.tf)
        self.engine = Engine(FillConfig(slippage_ticks=cfg.slippage_ticks, limit_through_ticks=cfg.limit_through_ticks,
                                        intrabar=cfg.intrabar, roll_fees=cfg.roll_fees, spread_model=cfg.spread_model,
                                        max_volume_pct=cfg.max_volume_pct, latency_ms=cfg.latency_ms),
                             RiskConfig(cfg.daily_loss_limit, cfg.max_position, cfg.check_margin, cfg.check_margin),
                             keep_fills=True)
        self.engine.add_account(account, cfg.balance)
        self.engine.spec(self.symbol)
        self.tf_seconds = tf_minutes(self.tf) * 60
        self.first_ts = int(self.ex.ts[0])
        self.eq_ts: list[int] = []
        self.eq_v: list[float] = []
        self.on_day_end: Callable[[str], None] | None = None
        self.last_date: str | None = None
        self.n = len(self.ex)

    def warmup_bars(self, n: int) -> list[Bar]:
        k = int(bucket_keys(np.array([self.first_ts]), self.spec.session, self.tf)[0])
        w = self.tf_all.between(None, k).tail(n)
        bars = [Bar(int(w.ts[i]), float(w.open[i]), float(w.high[i]), float(w.low[i]), float(w.close[i]),
                    float(w.volume[i]), self.tf) for i in range(len(w))]
        if bars:
            self.engine.on_price(self.symbol, bars[-1].ts + self.tf_seconds - 1, bars[-1].close, False)
        return bars

    def steps(self, progress: Callable[[float], None] | None = None, cancel: Callable[[], bool] | None = None):
        ex, spec, engine, symbol, tf = self.ex, self.spec, self.engine, self.symbol, self.tf
        keys = bucket_keys(ex.ts, spec.session, tf).tolist()
        tdates = spec.session.trade_dates_ns(ex.ts).tolist()
        ts_l, o_l, h_l, l_l, c_l, v_l = (ex.ts.tolist(), ex.open.tolist(), ex.high.tolist(), ex.low.tolist(),
                                         ex.close.tolist(), ex.volume.tolist())
        rolls = sorted([r for r in self.meta.rolls if r.ts > self.first_ts and (self.end_ts is None or r.ts < self.end_ts)],
                       key=lambda r: r.ts)
        rp = 0
        n = self.n
        bo = bh = bl = bc = bv = None
        prev_td = tdates[0]
        on_bar = engine.on_bar
        report_every = max(1, n // 200)
        for i in range(n):
            t = ts_l[i]
            while rp < len(rolls) and rolls[rp].ts <= t:
                r = rolls[rp]
                engine.on_roll(symbol, t, r.from_contract, r.to_contract, r.gap)
                rp += 1
            if tdates[i] != prev_td:
                if self.on_day_end:
                    self.on_day_end(_ns_date(prev_td))
                prev_td = tdates[i]
            o, h, l, c, v = o_l[i], h_l[i], l_l[i], c_l[i], v_l[i]
            on_bar(symbol, t, o, h, l, c, v)
            if bo is None:
                bo, bh, bl, bc, bv = o, h, l, c, v
            else:
                if h > bh:
                    bh = h
                if l < bl:
                    bl = l
                bc = c
                bv += v
            if i == n - 1 or keys[i + 1] != keys[i]:
                yield Bar(keys[i], bo, bh, bl, bc, bv, tf)
                self.eq_ts.append(t + 60)
                self.eq_v.append(engine.equity(self.account))
                bo = None
            if i % report_every == 0:
                if progress:
                    progress(i / n)
                if cancel and cancel():
                    raise Cancelled()
        self.last_date = _ns_date(prev_td)

    def finish(self, close_at_end: bool = True) -> None:
        if self.on_day_end and self.last_date:
            self.on_day_end(self.last_date)
        if close_at_end:
            self.engine.flatten(self.account, immediate=True, tag="end of test")
            if self.eq_v:
                self.eq_v[-1] = self.engine.equity(self.account)
        if self.last_date:
            self.engine._session_end(self.spec.session.name, self.last_date)  # record the last (partial) day

    def report(self, name: str, params: dict, logs: list | None = None) -> dict:
        acct = self.engine.accounts[self.account]
        trades = [t.to_dict() for t in acct.trades]
        eq_ts_a, eq_v_a = np.array(self.eq_ts, dtype="int64"), np.array(self.eq_v, dtype=float)
        stats = compute_stats(trades, eq_ts_a, eq_v_a, acct.days, self.cfg.balance, self.spec.tick_value,
                              self.spec.session.tz, tick_size=self.spec.tick_size)
        return {
            "strategy": name,
            "config": asdict(self.cfg) | {"params": params, "start_ts": self.first_ts, "end_ts": int(self.ex.ts[-1]) + 60},
            "summary": stats["summary"],
            "breakdown": stats["breakdown"],
            "equity": downsample(eq_ts_a, eq_v_a),
            "trades": trades,
            "fills": [f.to_dict() for f in acct.fills[-20000:]],
            "days": acct.days,
            "logs": logs or [],
            "bars_processed": self.n,
            "duration_s": round(time.time() - self.t0, 2),
            "synthetic_data": self.meta.synthetic,
            "data_source": self.meta.source,
        }


def run_backtest(cls: type[Strategy], cfg: BacktestConfig, progress: Callable[[float], None] | None = None,
                 cancel: Callable[[], bool] | None = None) -> dict:
    sim = BacktestSim(cfg)
    engine = sim.engine
    ctx = StrategyContext(engine, ACCOUNT, sim.symbol, sim.spec, sim.tf_seconds, tag=f"strategy:{cls.display_name()}")
    strat = cls(ctx, cfg.params)
    ctx.strategy = strat
    engine.hooks[ACCOUNT] = ctx.on_engine
    strat.on_start()
    ctx.warming = True
    for b in sim.warmup_bars(cfg.warmup if cfg.warmup is not None else cls.warmup):
        ctx.deliver(b)
    ctx.warming = False
    sim.on_day_end = strat.on_day_end
    for bar in sim.steps(progress, cancel):
        ctx.deliver(bar)
    sim.finish(cfg.close_at_end)
    strat.on_stop()
    if progress:
        progress(1.0)
    out = sim.report(cls.display_name(), vars(strat.p), [[t, m] for t, m in ctx.logs[-1000:]])
    out["strategy_class"] = cls.__name__
    return out


def _ns_date(ns: int) -> str:
    return np.datetime64(int(ns), "ns").astype("datetime64[D]").astype(str)


def grid(params: dict[str, list]) -> list[dict]:
    keys = list(params)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(params[k] for k in keys))]
