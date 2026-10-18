"""Command line:  python -m sim [serve|data|backtest] ..."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from datetime import datetime, timezone


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else "-"


def cmd_serve(a) -> None:
    import uvicorn

    from .config import settings
    host, port = a.host or settings.host, a.port or settings.port
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    print(f"\n  Trading Env Sim  ->  {url}\n  (Ctrl+C to stop)\n")
    if not a.no_browser:
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    uvicorn.run("sim.app:app", host=host, port=port, log_level="warning")


def cmd_data(a) -> None:
    from .data import importers
    from .data.store import store
    from .data.synthetic import generate_into_store

    if a.data_cmd == "list":
        syms = store().symbols()
        if not syms:
            print("No data. Run:  python -m sim data generate")
        for s in syms:
            m = store().meta(s)
            print(f"{s:8} {m.base_tf:4} {m.rows:>9,} rows  {_fmt_ts(m.first_ts)} -> {_fmt_ts(m.last_ts)} UTC  "
                  f"source={m.source}{'  [SYNTHETIC]' if m.synthetic else ''}  rolls={len(m.rolls)}")
    elif a.data_cmd == "generate":
        for m in generate_into_store(a.symbols, a.days, a.seed):
            print(f"generated {m.symbol}: {m.rows:,} 1-minute bars (synthetic)")
    elif a.data_cmd == "import":
        m = importers.import_csv(a.file, a.symbol, a.format, a.tz, a.bar_time, merge=not a.replace)
        print(f"imported {m.symbol}: {m.rows:,} {m.base_tf} bars, {_fmt_ts(m.first_ts)} -> {_fmt_ts(m.last_ts)} UTC, "
              f"rolls={len(m.rolls)}")
    elif a.data_cmd == "databento":
        import os
        key = a.api_key or os.environ.get("DATABENTO_API_KEY", "")
        if not key:
            sys.exit("Set DATABENTO_API_KEY in .env or pass --api-key")
        cost = importers.databento_cost(a.symbol, a.start, a.end, key, a.schema)
        print(f"Databento cost estimate for {a.symbol}.FUT {a.schema} {a.start} -> {a.end}: ${cost:,.2f}")
        if not a.yes and input("Download? [y/N] ").strip().lower() != "y":
            return
        m = importers.import_databento(a.symbol, a.start, a.end, key, a.schema)
        print(f"imported {m.symbol}: {m.rows:,} bars, rolls: {[(r.from_contract, r.to_contract, r.gap) for r in m.rolls]}")
    elif a.data_cmd == "bars":
        from .data.bars_github import import_bars
        m = import_bars(a.root, a.target, force=a.force)
        print(m.notes)
    elif a.data_cmd == "binance":
        m = importers.import_binance(a.symbol, a.days)
        print(f"imported {m.symbol}: {m.rows:,} bars")
    elif a.data_cmd == "download":
        from .contracts import registry
        from .data import free_sources as fs
        groups = {"crypto": ["crypto"], "forex": ["forex"], "stocks": ["stock"], "etfs": ["etf"],
                  "all": ["crypto", "forex", "stock", "etf"]}
        syms: list[str] = []
        for x in a.symbols:
            if x.lower() in groups:
                syms += [c.symbol for c in registry().all() if c.asset_class in groups[x.lower()]]
            else:
                syms.append(x.upper())
        failed = 0
        for sym in syms:
            last = [0.0]

            def progress(msg, frac=None):
                if frac is None or frac - last[0] >= 0.05 or frac >= 1:
                    last[0] = frac if frac is not None else last[0]
                    print(f"  {msg}" + (f"  {frac:.0%}" if frac is not None else ""), flush=True)

            try:
                m = fs.download(sym, a.source, a.start, a.end, progress, interval=a.interval,
                                spread=not a.bid_only)
                print(f"{m.symbol}: {m.rows:,} {m.base_tf} bars, {_fmt_ts(m.first_ts)} -> {_fmt_ts(m.last_ts)} UTC "
                      f"[{m.source}]" + (f"\n  {m.notes}" if m.notes else ""))
            except Exception as e:
                failed += 1
                print(f"{sym}: FAILED: {e}")
        if failed:
            sys.exit(1)
    elif a.data_cmd == "delete":
        store().delete(a.symbol)
        print(f"deleted {a.symbol}")


def cmd_backtest(a) -> None:
    from .strategy.runner import BacktestConfig, load_strategy, run_backtest

    params = {}
    for kv in a.param or []:
        k, v = kv.split("=", 1)
        try:
            params[k] = json.loads(v)
        except json.JSONDecodeError:
            params[k] = v
    cls = load_strategy(a.strategy)
    cfg = BacktestConfig(symbol=a.symbol or getattr(cls, "symbol", "MES"), timeframe=a.tf or cls.timeframe,
                         start=a.start, end=a.end, balance=a.balance, params=params,
                         slippage_ticks=a.slippage, limit_through_ticks=a.limit_through, intrabar=a.intrabar,
                         spread_model=not a.no_spread, max_volume_pct=a.max_volume_pct, latency_ms=a.latency_ms)
    res = run_backtest(cls, cfg, progress=lambda p: print(f"\r  {p * 100:5.1f}%", end="", flush=True))
    print()
    s = res["summary"]
    for k in ("trades", "win_rate", "net_pnl", "gross_pnl", "fees", "profit_factor", "expectancy",
              "avg_win", "avg_loss", "max_drawdown", "sharpe", "return_pct"):
        print(f"  {k:15} {s.get(k)}")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1, default=str)
        print(f"  report written to {a.out}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m sim", description="Trading Env Sim")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="start the web app (default)")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--no-browser", action="store_true")

    d = sub.add_parser("data", help="manage market data")
    dsub = d.add_subparsers(dest="data_cmd", required=True)
    dsub.add_parser("list")
    g = dsub.add_parser("generate", help="synthetic sample data")
    g.add_argument("--symbols", nargs="+", default=["MES", "MNQ"])
    g.add_argument("--days", type=int, default=365)
    g.add_argument("--seed", type=int, default=6)
    i = dsub.add_parser("import", help="import a CSV (TradingView, NinjaTrader, Databento or generic)")
    i.add_argument("file")
    i.add_argument("--symbol", required=True)
    i.add_argument("--format", default="auto", choices=["auto", "generic", "tradingview", "ninjatrader", "databento"])
    i.add_argument("--tz", default="UTC", help="time zone of timestamps without an offset, e.g. America/Chicago")
    i.add_argument("--bar-time", default="open", choices=["open", "close"])
    i.add_argument("--replace", action="store_true", help="replace instead of merging with existing data")
    db_ = dsub.add_parser("databento", help="download CME futures from Databento")
    db_.add_argument("--symbol", required=True)
    db_.add_argument("--start", required=True)
    db_.add_argument("--end", required=True)
    db_.add_argument("--schema", default="ohlcv-1m")
    db_.add_argument("--api-key")
    db_.add_argument("--yes", action="store_true")
    bg = dsub.add_parser("bars", help="free ES/NQ 1-minute bars from github.com/kfccolonel1/BARS")
    bg.add_argument("--root", default="ES", choices=["ES", "NQ"])
    bg.add_argument("--target", help="store as this symbol (default: MES for ES, MNQ for NQ; use ES/NQ to keep)")
    bg.add_argument("--force", action="store_true", help="download again even if cached")
    b = dsub.add_parser("binance", help="download Binance USD-M perpetual 1m klines (free)")
    b.add_argument("--symbol", default="BTCUSDT")
    b.add_argument("--days", type=int, default=30)
    dl = dsub.add_parser("download", help="free history for crypto (Binance), forex/gold (Dukascopy), "
                                           "stocks/ETFs (Alpaca key or Yahoo)")
    dl.add_argument("symbols", nargs="+", help="e.g. BTCUSDT EURUSD XAUUSD SPY AAPL, or: crypto forex stocks etfs all")
    dl.add_argument("--source", default="auto", choices=["auto", "binance", "dukascopy", "alpaca", "yahoo"])
    dl.add_argument("--start", help="YYYY-MM-DD (default: two years ago)")
    dl.add_argument("--end", help="YYYY-MM-DD (default: today)")
    dl.add_argument("--interval", default="1m", help="Yahoo bar size: 1m, 5m, 15m, 30m, 1h, 1d")
    dl.add_argument("--bid-only", action="store_true", help="Dukascopy: bid prices only (half the downloads)")
    x = dsub.add_parser("delete")
    x.add_argument("symbol")

    bt = sub.add_parser("backtest", help="run a strategy backtest")
    bt.add_argument("strategy", help="path to a strategy .py file or strategies/<name>")
    bt.add_argument("--symbol")
    bt.add_argument("--tf")
    bt.add_argument("--start")
    bt.add_argument("--end")
    bt.add_argument("--balance", type=float, default=50_000)
    bt.add_argument("--slippage", type=float, default=0.0, help="extra ticks per side beyond the bid-ask spread")
    bt.add_argument("--no-spread", action="store_true", help="do not model the bid-ask spread")
    bt.add_argument("--max-volume-pct", type=float, default=None, help="cap fills at this %% of bar volume")
    bt.add_argument("--latency-ms", type=int, default=0)
    bt.add_argument("--limit-through", type=float, default=1.0)
    bt.add_argument("--intrabar", default="tv", choices=["tv", "worst"])
    bt.add_argument("--param", action="append", help="strategy parameter, e.g. --param fast=9")
    bt.add_argument("--out")

    a = p.parse_args(argv)
    if a.cmd in (None, "serve"):
        if a.cmd is None:
            a = p.parse_args(["serve"] + (argv or sys.argv[1:]))
        cmd_serve(a)
    elif a.cmd == "data":
        cmd_data(a)
    elif a.cmd == "backtest":
        cmd_backtest(a)


if __name__ == "__main__":
    main()
