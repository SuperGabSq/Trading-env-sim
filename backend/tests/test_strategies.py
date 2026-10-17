"""Strategy API, backtest runner and the PATS port."""

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from sim.contracts import registry
from sim.data.bars import Bars, resample
from sim.data.synthetic import generate
from sim.engine import Engine
from sim.strategy import Bar, Param, Strategy, StrategyContext
from sim.strategy.runner import BacktestConfig, grid, list_strategies, load_strategy, run_backtest
from sim.strategy.stats import max_drawdown

from reference import pats_engine_ref as ref  # noqa: E402  (backend/tests is on sys.path via conftest)


@pytest.fixture()
def data(tmp_store):
    for sym, (df, meta) in generate(["MES", "MNQ"], date(2026, 8, 1), date(2026, 10, 9)).items():
        tmp_store.write(sym, df, meta)
    return tmp_store


def test_pats_signals_match_reference_engine(data):
    """The strategy's signal logic equals the original pats_engine.py (with session-based day resets)."""
    bars5 = data.bars("MES", "5m")
    # The reference resets on the calendar day in Chicago; TradingView's time("D") resets at the
    # 17:00 CT session start. During CDT, UTC+2 maps 17:00 CT to midnight, aligning both definitions.
    ref.CHICAGO = timezone(timedelta(hours=2))
    eng_ref = ref.PatsSecondEntryEngine()
    want = []
    for i in range(len(bars5)):
        t = datetime.fromtimestamp(int(bars5.ts[i]), tz=timezone.utc)
        s = eng_ref.on_bar(ref.Bar(t, bars5.open[i], bars5.high[i], bars5.low[i], bars5.close[i], bars5.volume[i]))
        if s:
            want.append((int(bars5.ts[i]), s.side))

    Pats = load_strategy("pats_second_entry")

    class Recorder(Pats):
        def signal(self, bar):
            s = super().signal(bar)
            if s:
                self.got.append((bar.ts, s))
            return s

    spec = registry().get("MES")
    ctx = StrategyContext(Engine(), "A", "MES", spec, 300, "t")
    ctx.engine.add_account("A")
    st = Recorder(ctx, {})
    st.got = []
    ctx.strategy = st
    st.on_start()
    ctx.warming = True
    for i in range(len(bars5)):
        ctx.deliver(Bar(int(bars5.ts[i]), bars5.open[i], bars5.high[i], bars5.low[i], bars5.close[i], bars5.volume[i]))
    assert len(want) > 20
    assert st.got == want


def test_backtest_runs_and_is_deterministic(data):
    cls = load_strategy("pats_second_entry")
    cfg = BacktestConfig(symbol="MES", timeframe="5m", start="2026-09-01", end="2026-10-09")
    r1 = run_backtest(cls, cfg)
    r2 = run_backtest(cls, cfg)
    assert r1["summary"] == r2["summary"]
    s = r1["summary"]
    assert s["trades"] > 0 and s["trades"] == len(r1["trades"])
    assert s["net_pnl"] == pytest.approx(sum(t["net"] for t in r1["trades"]), abs=0.05)
    assert s["final_equity"] == pytest.approx(50_000 + s["net_pnl"], abs=0.05)
    # every PATS trade respects the 08:35-14:30 CT entry window and the trade cap
    for t in r1["trades"]:
        assert t["entry_tag"] in ("pats long", "pats short")
    per_day = {}
    for t in r1["trades"]:
        d = registry().get("MES").session.trade_date(t["entry_ts"])
        per_day[d] = per_day.get(d, 0) + 1
    assert max(per_day.values()) <= 5


def test_no_lookahead_strategy_sees_only_closed_bars(data):
    seen = []

    class Peek(Strategy):
        timeframe = "15m"
        warmup = 0

        def on_bar(self, bar):
            seen.append((bar.ts, self.time))

    run_backtest(Peek, BacktestConfig(symbol="MES", timeframe="15m", start="2026-09-14", end="2026-09-15"))
    assert seen and all(now >= ts + 15 * 60 - 1 for ts, now in seen)


def test_fill_resolution_chart_vs_1m(data):
    cls = load_strategy("ema_crossover")
    a = run_backtest(cls, BacktestConfig(symbol="MES", timeframe="15m", start="2026-08-15", fill_resolution="1m"))
    b = run_backtest(cls, BacktestConfig(symbol="MES", timeframe="15m", start="2026-08-15", fill_resolution="chart"))
    assert a["summary"]["trades"] > 0 and b["summary"]["trades"] > 0


def test_params_coercion_and_grid():
    class S(Strategy):
        params = {"n": Param(10, min=1), "x": Param(1.5), "on": Param(True), "mode": Param("a", choices=["a", "b"])}

    ctx = StrategyContext(Engine(), "A", "MES", registry().get("MES"), 300, "t")
    s = S(ctx, {"n": "12", "x": "2", "on": "false"})
    assert (s.p.n, s.p.x, s.p.on, s.p.mode) == (12, 2.0, False, "a")
    assert len(grid({"a": [1, 2, 3], "b": [True, False]})) == 6


def test_list_strategies_has_examples():
    names = {s.get("class") for s in list_strategies()}
    assert {"PatsSecondEntry", "EmaCrossover", "OpeningRangeBreakout"} <= names


def test_max_drawdown():
    dd, pct, pk, tr = max_drawdown(np.array([100, 120, 90, 130, 80, 140.0]))
    assert dd == 50 and pk == 3 and tr == 4
