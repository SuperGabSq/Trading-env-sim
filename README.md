# Trading Env Sim

A local, TradingView-style simulator built for **futures**, that also handles **crypto, forex, gold, US
stocks and ETFs**. It runs on your computer and your browser is the screen. You can:

- chart and paper trade by hand,
- replay any past session bar by bar,
- backtest Python strategies,
- run the same strategies inside replays and live sessions,
- connect external bots through an API, and
- paper trade on real-time data.

One engine computes every fill, fee, margin call and roll, so a strategy behaves the same in a
backtest, a replay and live.

See [ROADMAP.md](ROADMAP.md) for the phased plan and what comes next.

---

## Start it (Windows)

1. Install **Python 3.10 or newer** from python.org. Tick "Add python.exe to PATH".
2. Double-click **`start.bat`**, or run `.\start.ps1` in PowerShell.

The first start does three things:

- creates `.venv`,
- installs the packages,
- generates one year of synthetic MES/MNQ sample data. This data is marked "sample" everywhere in the
  app; it is not real prices.

Your browser then opens at http://127.0.0.1:8000. Press `Ctrl+C` in the window to stop the app.

To use real prices, open **Data → Free ES/NQ minute bars → Download and import**. Details are under
[Market data](#market-data).

Node.js is only needed if you change the web UI. The built UI ships in `frontend/dist`.

---

## What you can do

### Trade page: charts, manual trading, bar replay

**Charts**
- Candles and volume, from 1m to 1W, shown in exchange time (CT for CME).
- Indicators: EMA, SMA, session VWAP, Bollinger Bands, RSI, ATR. They are computed by the same Python
  code that strategies use.
- Drawings: level, trend line and box. They are saved per symbol.
- A back-adjusted view that removes roll gaps.

**Start replay**
- Pick instruments and a start time.
- Play at 1x up to Max speed, or step one bar at a time (→ key).
- Charts never show data past the replay clock.
- The ribbon under the chart shows where you are in the trading day: overnight, regular hours and the
  15:45 CT switch to overnight margin. Click ahead on it to fast-forward.

**Orders**
- Use the order ticket (market / limit / stop, with an optional bracket in ticks).
- Right-click the chart to place a limit or stop at that price.
- Drag order lines to move them.

**Hotkeys:** `Shift+B` buy, `Shift+S` sell, `Shift+F` flatten, `Shift+C` cancel all, `Space`
play/pause.

**Tables** show positions, working orders, fills, trades (with MAE/MFE) and daily P&L. Trades and
fills export to CSV.

**Strategies tab:** runs any strategy from `strategies/` inside the current replay or live session.
It trades the session's account; you can stop it alone or stop it and flatten its position.

### Backtest page

- Runs a strategy over history with all its parameters.
- **Report:**
  - equity and drawdown chart
  - P&L, win rate, profit factor, expectancy in ticks
  - Sharpe/Sortino and max drawdown
  - fees as a share of gross profit
  - P&L by hour, weekday and month, plus long vs short
  - every trade, with a chart of that trade
- **Parameter sweeps:** a grid such as `4,6,8` or `4..12 step 2`, ranked by the metric you pick.
- **History:** every run is saved, including runs from external bots.
- Strategy files reload on each run, so edit and run again.

### Live paper trading ("Go live")

| Source | Data | Cost |
|---|---|---|
| Simulated live | stored minute bars streamed as ticks and quotes at real-time speed (any hour, weekends) | free |
| Binance perpetuals | real-time trades and bid/ask: BTC, ETH, SOL, XRP, BNB, DOGE | free, no account |
| Alpaca | real-time US stock and ETF trades and bid/ask (IEX feed: live prices, partial volume) | free account key |
| Interactive Brokers | real-time CME futures, US stocks/ETFs, forex and spot gold through TWS / IB Gateway (`pip install ib_async`) | CME top-of-book about $1.25/month; forex free |

- Orders always fill in this simulator, never at a broker.
- A live session is saved every 10 seconds. If the app restarts, it resumes the account, positions,
  working orders and attached strategies.
- When the feed provides bid/ask quotes, market and stop orders fill at the real bid/ask.

### Bots & API page

- External bots use the REST API and the `/ws` event stream to trade a running session.
- Over `/ws/bot` the simulator runs a lock-step backtest: it replays history and waits for the bot after
  every bar. See [Bots](#bots-and-the-api).

---

## Market data

Each instrument is stored as one continuous front-month series in `data/bars/<SYMBOL>/`. Prices are the
traded (unadjusted) prices of each contract. Roll times and gaps are recorded, which lets the chart offer
a back-adjusted view and lets open positions and orders roll correctly.

**Free ES/NQ minute bars (BARS on GitHub)**

Data → *Free ES/NQ minute bars*, or `python -m sim data bars --root ES`.

- **Coverage:** real 1-minute E-mini history, ES from June 2025 and NQ from June 2024.
- **Stored as micros by default.** ES is stored as MES and NQ as MNQ. Micros trade at the same index
  level, so the bars drive micro tick values, fees and margin.
- **Rolls repaired.** The repository switches contract at 00:00 ET a day or two before expiry, in the
  middle of a session and days after volume has moved. Imported as-is, each switch would look like a
  +50..70 point ES (+200..320 NQ) jump and be booked as P&L. The importer detects every switch, records it
  as a roll with its gap and labels each bar with its contract month. For ES (Dec 2025 onwards) the
  per-contract IBKR files move the roll to the usual volume roll and rebuild the thin expiry days.
- **Quality check:** when both are on the same contract, the ES series matches IBKR's own contract data
  on 99.4% of minutes (all within one tick).
- **Caveats:**
  - The repository has no license, so use it for personal research only and don't push it to a public
    repo. `data/` is git-ignored.
  - It is stitched from several sources; about 5% of the oldest NQ bars (mid-2024) have no volume.

**Databento (CME, paid with free credit)**

Data → *Download CME futures from Databento*, or `python -m sim data databento --symbol MES --start
2025-01-01 --end 2026-10-01`.

- It downloads every contract month and stitches them by volume roll.
- New accounts get $125 of free credit.
- The cost is shown before anything is downloaded.
- Put `DATABENTO_API_KEY` in `.env`.

**Your own files**

Data → *Import a file*, or `python -m sim data import file.csv --symbol MES`. Supported formats:

- TradingView exports
- NinjaTrader `.txt` exports (bar-close times are handled)
- Databento CSV or `.dbn.zst` files
- any CSV with time/open/high/low/close/volume columns

Files with a contract column are stitched into a continuous series automatically.

**Crypto, forex, gold, stocks and ETFs (free)**

Data → *Download free market data*, or `python -m sim data download BTCUSDT EURUSD SPY --start 2024-01-01`
(`crypto`, `forex`, `stocks`, `etfs` or `all` download a whole group).

| Class | Instruments | Source | History |
|---|---|---|---|
| Crypto | BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, BNBUSDT, DOGEUSDT (USD-M perpetuals) | Binance public archive, no key | 1-minute back to 2020 |
| Forex & gold | EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, XAUUSD | Dukascopy bid and ask, no key | 1-minute, many years |
| Stocks | AAPL, MSFT, NVDA, TSLA, AMZN, META, GOOGL, AMD | Alpaca (free key) or Yahoo (no key) | Alpaca: 1-minute since 2016; Yahoo: last 30 days of 1-minute |
| ETFs | SPY, QQQ, IWM, DIA, TQQQ, GLD, TLT | same as stocks | same as stocks |

- Running a download again only adds the new part. Choosing a different source replaces the series.
- **Forex** is stored as the bid/ask mid price, so orders pay half the spread like at a broker. The
  download measures the real median spread and shows it next to the contract's setting.
- **Stocks** include pre-market and after-hours bars and are split-adjusted (not dividend-adjusted). For
  Alpaca, put `ALPACA_API_KEY` and `ALPACA_API_SECRET` in `.env` (paper-trading keys work).
- Yahoo is unofficial and meant for personal use; replay and live sessions need 1-minute bars.
- Any of these symbols can also be filled from your own CSV files with *Import a file*.

**What a quantity of 1 means**

| Class | Quantity 1 | Tick | Margin (default) | Fees (default) |
|---|---|---|---|---|
| Futures | one contract | contract tick | per-contract day/overnight margin | per contract per side |
| Crypto | one contract of 0.01 BTC, 0.1 ETH, 1 SOL, 10 XRP, 0.1 BNB or 100 DOGE | exchange tick | 5% of notional (20x) | 0.04% of notional |
| Forex | one micro lot (1,000 units); 100 = one standard lot | pipette (0.00001, JPY 0.001) | 1:30 majors, 1:20 gold (EU retail) | $3.50 per standard lot per side |
| Stocks / ETFs | one share | $0.01 | 25% intraday (4x), 50% overnight (2x) | $0.005 per share |

USD-base pairs (USDJPY, USDCAD, USDCHF) earn profit in the quote currency; the simulator converts it
to USD at the current rate. Not modelled yet: forex swaps, stock dividends, short-borrow fees, halts
and holidays.

**Contract specs**

`config/contracts.yaml` defines tick size and value, sessions, margins, fees and typical spreads. The
margin and fee values are approximate starting points; edit them to match your broker. To add another
stock or ETF, copy one line such as `AMD:  {<<: *stock, name: Advanced Micro Devices}` and restart.

---

## How fills are simulated

The full rules are at the top of `backend/sim/engine/engine.py`. In short:

- **Prices arrive as points.**
  - A 1-minute bar becomes four points: open, then high or low, then the other, then close. The order
    follows TradingView's heuristic, or a worst-case setting.
  - Live data arrives tick by tick.
  - Backtests, replays and live sessions all use the same matching code.
- **Market and stop orders cross the bid-ask spread.**
  - With live quotes, they fill at the real bid/ask.
  - Otherwise they pay half the contract's typical spread, rounded up to a tick. The spread is wider
    overnight and in the opening minute (for MES that means 1 tick most of the time).
  - Optional extra slippage ticks can be added on top.
  - Stops that price gaps through fill at the gap price.
- **Limit orders fill only when price trades through them.** The default is 1 tick through, the same as
  TradingView's `backtest_fill_limits_assumption = 1`. On a gap they fill at the better price.
- **Orders only see prices that arrive after they are sent,** plus optional latency.
- **Optional partial fills:** fills are capped at a share of traded volume, and bracket legs grow with
  each partial fill.
- **Margin**
  - Intraday margin applies until 15:45 CT, then overnight margin.
  - Positions are liquidated if equity falls below maintenance margin, or if the account cannot carry
    them overnight.
  - Optional daily loss limit and max position.
- **Rolls**
  - Positions and working orders move to the new contract with the price gap. The roll does not change
    your P&L.
  - Roll fees are optional.

Checks behind this:
- 56 engine scenario tests.
- A strategy running in a replay session produces exactly the same trades as the backtester.
- A lock-step external bot produces exactly the same trades as the same logic written as a built-in
  strategy.

---

## Writing a strategy

Create a file in `strategies/`; it shows up in the app automatically.

```python
from sim.indicators import EMA, ATR
from sim.strategy import Bar, Param, Strategy

class EmaCross(Strategy):
    """Shown in the app as the description."""
    timeframe = "15m"
    symbol = "MES"
    warmup = 200                      # bars before the start used to warm up indicators and state
    params = {"fast": Param(9, min=2, max=50), "slow": Param(21, min=5, max=200)}

    def on_start(self):
        self.fast = self.indicator(EMA(self.p.fast))
        self.slow = self.indicator(EMA(self.p.slow))

    def on_bar(self, bar: Bar):       # called when a bar CLOSES; orders act from the next price
        if self.fast[-1] > self.slow[-1] and self.position <= 0:
            self.buy(1, tp_ticks=32, sl_ticks=16)
```

**Data and helpers**
- `self.bars` holds the closed bars.
- `self.position` and `self.entry_price` describe the position; `self.equity` is the account equity.
- `self.open_orders` lists this strategy's working orders; `self.last_price` is the latest price.
- `self.time` and `self.local_time` give the current time.
- `self.minute_of_day(ts)` and `self.in_window(bar, "08:35", "14:30")` help with time windows.
- `self.trade_date(bar)`, `self.tick`, `self.round_price()` and `self.log()` are also available.

**Orders**
- `self.buy(qty, price=…, stop=…, tp=…, sl=…, tp_ticks=…, sl_ticks=…, tif="day", oco="group",
  trail_ticks=…)`, and `self.sell(...)` with the same arguments.
- `self.modify(order, price=…, stop=…)`, `self.cancel(order)`, `self.cancel_all()`,
  `self.close()`.

**Hooks:** `on_start`, `on_bar`, `on_fill`, `on_order`, `on_day_end`, `on_stop`.

**Examples**
- `pats_second_entry.py` is your PATS strategy, ported from the Pine script.
  - It trades real MES contracts with real fees and margin.
  - Its signal logic matches the old `pats_engine.py` exactly; a test checks this against
    `backend/tests/reference/pats_engine_ref.py`.
  - One change from the old Python port: the day reset follows the 17:00 CT session start, like
    TradingView's `time("D")`.
- `ema_crossover.py` and `opening_range_breakout.py` are smaller examples.

---

## Bots and the API

**Trade a running session**

Start a replay or live session, then call the REST API from your bot. The interactive reference is at
http://127.0.0.1:8000/docs. The single-file Python client `clients/python/tsim_client.py` wraps it:

```python
sim = TsimClient("http://127.0.0.1:8000")
sim.buy("MES", 1, tp_ticks=8, sl_ticks=8)
async for msg in sim.events(symbol="MES", tf="5m"): ...
```

**Lock-step backtests**

`clients/python/examples/lockstep_ema_bot.py` runs a bot in another process over `/ws/bot`. The
simulator waits for the bot after each bar, and the report lands in Backtest history.

**From another computer**

Set `TSIM_HOST=0.0.0.0` and `TSIM_API_KEY=...` in `.env`. Remote clients then send `X-API-Key`; local
clients never need it.

---

## Command line

```powershell
.venv\Scripts\python -m sim                      # start the app (same as start.bat)
.venv\Scripts\python -m sim data list
.venv\Scripts\python -m sim data bars --root ES  # free real ES minute bars, stored as MES
.venv\Scripts\python -m sim data import export.csv --symbol MES --tz America/Chicago
.venv\Scripts\python -m sim data databento --symbol MES --start 2025-01-01 --end 2026-10-01
.venv\Scripts\python -m sim data download BTCUSDT EURUSD XAUUSD SPY --start 2024-01-01
.venv\Scripts\python -m sim data download stocks --source yahoo
.venv\Scripts\python -m sim data generate --days 365
.venv\Scripts\python -m sim backtest pats_second_entry --start 2026-01-01 --param target_ticks=8 --out report.json
.venv\Scripts\python -m pytest                   # 114 tests
```

---

## Project layout

```
backend/sim/
  contracts.py     contract registry + session calendars (CME, 24/7 crypto, 24/5 forex, US stocks)
  data/            Parquet store, resampling, continuous contracts, importers, free downloaders, synthetic data
  engine/          orders, matching, positions, fees, margin, rolls
  indicators.py    indicators shared by chart and strategies
  session.py       replay and live sessions (clock, feeds, strategies)
  feeds/           simulated, Binance, Alpaca and IBKR live feeds
  strategy/        Strategy API, backtest runner, statistics, in-session runner
  api/             REST + WebSocket endpoints
backend/tests/     114 tests (engine scenarios, data, downloaders, indicators, strategies, API, live)
frontend/          React + TypeScript + Lightweight Charts (built into frontend/dist)
strategies/        your strategies
clients/python/    Python client and example bots
config/            contracts.yaml
data/              market data, SQLite state, downloads (git-ignored)
```

To work on the UI: run `npm install` once, then `npm run dev` in `frontend/` while the app runs; the dev
server is http://127.0.0.1:5173. When you're done, run `npm run build`.

---

## Known limitations

- Fills come from 1-minute bars (or ticks when live), not from order-book queue position. There is no
  level-2 or depth-of-market (DOM) simulation yet; it is a candidate for Phase 8.
- The ES/NQ bars from BARS and the volumes of micro contracts are approximations. For decisions with
  real money, validate on Databento data.
- The IBKR feed is written against the `ib_async` API but could not be tested here without a TWS
  connection. The Binance feed is tested with recorded messages, not the live network.
- The Binance archive, Dukascopy, Alpaca and Yahoo downloaders and the Alpaca live feed follow each
  service's published formats and are tested with recorded-format samples; they could not be run
  against the real services from the build machine. If one fails, the Data page shows the error.
- The example strategies were written for CME index futures hours (Central time). On other markets,
  change their session times or write strategies that use `self.spec.session`.
- Exchange margins and broker fees change often; keep `config/contracts.yaml` up to date.
