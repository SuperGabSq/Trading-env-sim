# Trading Env Sim — Roadmap

A local, TradingView-style app built for **futures**: chart the market, trade by hand, replay
history bar by bar, backtest strategies, plug in your own bots, and paper trade in real time.

## Status (updated 2026-10-17)

| Phase | Status |
|---|---|
| 0 Foundation | Done |
| 1 Market data | Done, plus free real ES/NQ minute bars (BARS) with real ES roll times |
| 2 Charting UI | Done |
| 3 Simulation engine | Done (56 scenario tests) |
| Execution model v2 | Done: bid-ask spread (time-of-day or live quotes), volume-capped partial fills, latency |
| 4 Manual trading + bar replay | Done (M1) |
| 5 Strategy backtesting | Done (M2): PATS ported, signal logic verified against the old pats_engine.py |
| 6 Real-time paper trading | Done: simulated live, Binance perpetuals, IBKR (not tested without TWS) |
| 7 External bot API | Done (M3): REST, WebSocket events, lock-step backtests, Python client |
| 8 Advanced | Not started: pick the items you want |

Open items: compare PATS with TradingView's Strategy Tester on the same MES data (needs a TradingView
export of the strategy's trade list), and validate on Databento data before trusting any result.

> Why the rewrite: the previous project ran an MES strategy against **SPY shares on Alpaca** because
> Alpaca has no futures. Every conversion (points → dollars, contracts → shares, no margin, no
> per-contract fees) distorted the results. This simulator models futures contracts natively.

---

## 1. Guiding principles

1. **One engine, many clocks.** Orders, fills, margin and P&L are computed by a single simulation
   engine. Manual trading, bar replay, backtests and live paper trading differ only in *where the
   data comes from* and *who drives the clock*. A strategy that works in a backtest behaves the same
   way in replay and in live paper trading.
2. **Futures-native.** Every instrument has tick size, tick value, multiplier, margin, trading
   sessions, fees per contract, expiry and roll rules. No stock proxies.
3. **Pessimistic by default, configurable.** If one bar touches both stop and target, the stop fills
   first. Limit orders fill only when price trades *through* them (configurable). Slippage in ticks.
4. **No look-ahead.** Strategies and replay only ever see closed bars (or ticks already printed).
5. **Bots talk to the sim like a broker.** The same API serves backtest, replay and live modes.
6. **Engine before UI.** The matching/accounting core is test-driven; the UI sits on top of it.

---

## 2. Architecture

```
 Data sources        Feed adapters          Event bus             Simulation engine
 ────────────        ─────────────          ─────────             ─────────────────
 CSV exports   ─┐                                               ┌─ Orders & matching
 Databento     ─┼─► Historical replay ──┐                       ├─ Positions & P&L
 IBKR (CME L1) ─┤                       ├─► bars / ticks / ─────►├─ Margin & fees
 Crypto perps  ─┘   Live stream     ────┘    clock events        └─ Daily settlement
                                                │                           │
                                                ▼                           ▼
                                      Strategy runner              REST + WebSocket API
                                      (Python bots, in-process)      │            │
                                                                     ▼            ▼
                                                                  Web UI    External bots
                                                              (charts, ticket) (any language)
```

### Proposed stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.12, FastAPI, WebSockets, Pydantic | Your strategies are already Python; strong data ecosystem |
| Market data storage | Parquet files + DuckDB | Fast columnar storage, resample years of 1-min bars on the fly |
| App state | SQLite | Accounts, orders, fills, backtest runs, journal |
| Number crunching | Polars / NumPy | Speed for backtests and resampling |
| Frontend | React + TypeScript + Vite | Standard, fast dev loop |
| Charts | TradingView **Lightweight Charts v5** | Open source (Apache 2.0), multi-pane support, plugin API for drawings |
| Tooling | uv (Python), npm, pytest, ruff | Works well on Windows |
| Later | Tauri desktop wrapper (optional) | Run as a desktop app instead of a browser tab |

### Proposed folder layout

```
Trading-bot/
├─ backend/
│  ├─ sim/engine/      orders, matching, fill models, accounts, margin, fees
│  ├─ sim/data/        importers, Parquet store, resampling, continuous contracts
│  ├─ sim/feeds/       historical replay + live adapters
│  ├─ sim/strategy/    strategy API + backtest runner + reports
│  └─ sim/api/         REST + WebSocket endpoints
├─ frontend/           React app (chart, order ticket, panels, replay controls)
├─ strategies/         your strategies (PATS second-entry is the first port)
├─ config/             contracts.yaml, settings
├─ data/               Parquet + DuckDB files (git-ignored)
└─ docs/               this roadmap, design notes, decisions
```

---

## 3. What "simulating futures" means here

**Contract registry** (`config/contracts.yaml`), seeded with MES, ES, MNQ, NQ, M2K, MYM, MGC, MCL
and a couple of crypto perpetuals. Each entry has:

- tick size and tick value (e.g. MES 0.25 = $1.25, ES 0.25 = $12.50, MNQ 0.25 = $0.50, NQ 0.25 = $5.00)
- trading sessions (CME Globex Sun–Fri 17:00–16:00 CT with the daily 16:00–17:00 halt) and RTH window
- initial / maintenance margin, with separate intraday and overnight values
- fees per contract per side (commission + exchange + clearing), editable to match your broker
- expiry cycle (H, M, U, Z) and roll rule

**Continuous contracts:** unadjusted (real traded prices) and back-adjusted (for indicators and
long backtests). Roll by volume crossover or N days before expiry.

**Account model:** cash balance, realized/unrealized P&L, margin used, buying power, daily
mark-to-market settlement, margin call and auto-liquidation, max position size, daily loss limit.

**Orders:** market, limit, stop, stop-limit, bracket (take-profit + stop-loss as OCO), trailing
stop, time-in-force (DAY / GTC / IOC), reduce-only, flatten all, reverse.

**Fill models (selectable):**
- **Bar model** — fills inside OHLC bars with a configurable intrabar path and slippage in ticks.
- **Tick model** — fills against recorded trades when tick data is available.
- **Book model** (later) — queue position against L2/L3 order-book data.

---

## 4. Market data options

| Need | Free | Paid |
|---|---|---|
| Historical CME (MES, MNQ, ES…) | CSV exports you already have (TradingView, NinjaTrader); Yahoo continuous futures (very limited intraday history) | **Databento** pay-as-you-go — $125 free credit at signup covers years of 1-min bars for a few micros |
| Live CME | none in real time | **IBKR** CME top-of-book ≈ $1.25/month (non-professional, needs a funded IBKR account); **Databento** CME Standard plan $199/month (full depth, more history) |
| Live futures at no cost | **Crypto perpetuals** public market-data streams — real leverage, funding and liquidation mechanics | — |
| Testing the live pipeline | **Simulated live**: replay any historical day at 1× wall-clock speed | — |

**Recommendation:** start with Databento historical (free credit) + CSV import. For live, build
*simulated live* first, then crypto perps (free), then IBKR CME L1 when you want real CME prices.
Use Databento live only if you need order-book depth.

---

## 5. Phases

Sizes: **S** ≈ 1–2 working sessions · **M** ≈ 3–5 · **L** ≈ 6+

### Phase 0 — Foundation (S)
- Folder layout above, `pyproject.toml` (uv), Vite React app, `.gitignore`, `.env.example`.
- One `dev` script (PowerShell) that starts backend + frontend together.
- Logging, pytest, ruff, basic CI (optional).
- **Done when:** running `dev` opens `localhost` and the page receives a heartbeat from the backend
  over WebSocket.

### Phase 1 — Market data layer (M)
- Contract registry with the seed contracts.
- Importers: generic CSV, TradingView CSV export, Databento (OHLCV-1m, trades).
- Parquet store partitioned by symbol / contract / date; DuckDB views on top.
- Continuous-contract builder (roll rules, back-adjust on/off).
- Session-aware resampler: 1m → 5m / 15m / 1h / 4h / daily, with daily bars aligned to the CME
  session, not midnight.
- Endpoints: list symbols, get bars (symbol, timeframe, from, to).
- **Done when:** 2+ years of MES 1-min bars are loaded and a sampled week of 5-min bars matches
  TradingView's MES1! within rounding.

### Phase 2 — Charting UI (M)
- Layout: main chart, watchlist, symbol and timeframe switcher, OHLC legend, volume pane.
- Indicators v1: EMA, SMA, session VWAP, Bollinger Bands, RSI, ATR — computed by the **same Python
  code strategies use**, so the chart shows exactly what a bot sees.
- Drawings v1: horizontal line, trend line, rectangle, with persistence.
- Lazy-load history while scrolling; dark/light theme; chart times shown in exchange time (CT).
- **Done when:** scrolling a year of 1-min MES is smooth and switching timeframe feels instant.

### Phase 3 — Simulation engine core (L) ← the heart of the project
- Clock abstraction (historical clock vs wall clock) and event loop.
- Order management + matching with the bar fill model; tick model when trades exist.
- Positions, P&L in ticks and dollars, fees, margin, daily settlement, liquidation.
- All order types from section 3, including brackets/OCO and trailing stops.
- Append-only event log (every order, modify, cancel, fill) in SQLite for audit and replay.
- Golden-scenario test suite: gap through a stop, stop and target in the same bar, roll while in a
  position, margin call, partial-session days, holiday closes.
- **Done when:** 50+ scenario tests pass and a scripted trade sequence matches a hand calculation to
  the cent.

### Phase 4 — Manual paper trading + Bar Replay (M) ← first usable version (MVP)
- Order ticket: quantity, market / limit / stop, bracket presets in ticks (e.g. +6 / −8).
- Trade from the chart: click to place, drag orders and TP/SL lines to modify, position line with
  live P&L.
- Panels: positions, working orders, fills, account summary (balance, margin, day P&L).
- **Bar Replay:** pick a date, play / pause / step, speed 1×–100×, trade while it plays; fills use
  only data that has already "happened".
- Hotkeys: buy market, sell market, flatten, cancel all.
- Trade journal export (CSV).
- **Done when:** you can practice a full RTH session of MES from any past date and the journal and
  P&L are correct.

### Phase 5 — Strategy backtesting (L)
- Python strategy API: `on_start`, `on_bar`, `on_fill`, `on_order_update`; `ctx.buy / sell /
  bracket / cancel / flatten`; indicator helpers; typed parameters.
- Runner: single run, parameter sweep, date ranges, on the Phase 3 engine.
- Report page: equity curve, drawdown, trades plotted on the chart, net P&L, win rate, profit
  factor, expectancy in ticks, average win/loss, max drawdown, Sharpe/Sortino, commissions as % of
  gross, breakdown by hour and weekday; CSV export.
- Port **PATS second-entry** (recoverable from git history) as the reference strategy, running on
  real MES data with real MES fees and margin.
- **Done when:** the PATS backtest matches TradingView's strategy tester on the same MES data within
  an agreed tolerance, with every difference explained by the fill model.

### Phase 6 — Real-time paper trading (M)
- Feed adapters behind one interface: SimulatedLive, CryptoPerp, IBKR (CME L1), Databento (optional).
- Tick → bar builder; handles reconnects, gaps, session open/close and the daily halt.
- Paper accounts and working orders survive restarts.
- Strategies run live with exactly the same code as in backtests.
- **Done when:** a strategy runs a full week unattended on live data, and backtesting that same week
  reproduces the same trades (apart from documented fill-model differences).

### Phase 7 — External bot API (M)
- REST: accounts, orders (place / modify / cancel), positions, fills, bars.
- WebSocket: market-data stream and order/fill events.
- Local API keys; auto-generated OpenAPI docs.
- Python client SDK + examples (any language can use the HTTP/WS API).
- **Lock-step mode:** the sim drives the clock and waits for the bot each step, so external bots can
  be backtested too, not only run live.
- Optional broker-compatible shim (e.g. Alpaca-style endpoints) so an existing bot can switch by
  changing its base URL.
- **Done when:** a bot in a separate process trades the sim in both live-paper and lock-step
  backtest mode.

### Phase 8 — Advanced (pick as needed)
- Optimization: grid / random search, walk-forward, in-sample vs out-of-sample split.
- Robustness: Monte Carlo on trade order, slippage and fee stress tests.
- Prop-firm rules mode: trailing drawdown, daily loss limit, consistency rules.
- Tick / L2 simulation with queue position and a DOM ladder.
- Multi-chart layouts, price alerts, journal with tags, notes and screenshots.
- Multiple accounts and strategy portfolios.
- Desktop packaging (Tauri).

---

## 6. Milestones

| Milestone | Phases | You get |
|---|---|---|
| **M1 — Trainer** | 0–4 | Charts + manual futures paper trading + bar replay on historical data |
| **M2 — Lab** | 5 | Backtest Python strategies with reports; PATS validated on real MES |
| **M3 — Live** | 6–7 | Real-time paper trading and any external bot connected to the sim |
| **M4 — Pro** | 8 | Optimization, robustness, prop-firm rules, order-book simulation |

---

## 7. Risks and how we handle them

- **Bar fills are approximations** → pessimistic defaults, tick model where data exists, clearly
  labelled assumptions in every report.
- **Look-ahead bias** → strict event ordering; strategies see closed bars only unless intrabar mode is
  explicitly enabled.
- **Contract rolls** distort indicators on unadjusted series → back-adjusted series for signals,
  real contract prices for fills.
- **Time zones** → store UTC, display exchange time (CT).
- **Data licensing** → CME data is for your personal use; don't redistribute raw data.

---

## 8. Open decisions

1. Stack: Python/FastAPI + React/TypeScript + Lightweight Charts — OK?
2. Historical data: open a Databento account (free credit), or start from CSV exports you have?
3. First instruments: MES and MNQ?
4. Live path: simulated live → crypto perps → IBKR CME L1 — OK, or do you already have a futures
   broker/data feed (NinjaTrader, Tradovate, Rithmic) you'd rather connect?
