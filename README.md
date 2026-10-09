# PATS paper-trading starter

This project contains a Python signal engine and an Alpaca **paper-only** runner for the PATS second-entry strategy in the attached Pine text. The runner defaults to `SPY`; it uses five-minute IEX stock bars and has no live-trading mode.

## Instrument and conversion assumptions

The Pine strategy is calibrated for MES futures. Alpaca's Trading API supports stocks, ETFs, options, and crypto; its current docs list futures as a roadmap item. This runner therefore applies the MES price-action rules to SPY shares. That is a proxy experiment, not a MES backtest or a contract-for-contract equivalent.

The default conversion assumes 1.00 MES index point is about $0.10 of SPY price movement and one MES contract is about 50 SPY shares. With the Pine defaults, two contracts map to 100 shares, a 6-tick target maps to $0.15, an 8-tick stop to $0.20, and a 2-tick tightening step to $0.05. These are explicit starting assumptions; SPY tracking, share price, market structure, and Alpaca buying power differ from MES. QQQ tracks a different index. Do not use the defaults for QQQ without recalibrating `MES_POINT_TO_STOCK_DOLLARS` and `PATS_SHARES_PER_MES_CONTRACT` in `.env`.

The strategy's MES commission and 2% futures margin settings are not applied to stock orders. The runner sizes in shares, limits notional to 85% of Alpaca buying power, and retains the five-trade daily cap. Alpaca stock paper fills and order handling also differ from TradingView's futures emulator.

The attached Pine text is the translation source. The workspace file `PATS_Second_Entry_MES.pine` contains an additional relative-volume breakout module that is not in the attached text and is not ported here.

## Setup on Windows PowerShell

1. Create API keys for the **paper account** in Alpaca. Keep the key ID and secret private; do not paste them into chat or commit them.
2. In this folder, create and activate a virtual environment, then install the dependencies:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and put the paper key ID and secret in the first two fields. Review the proxy assumptions below before submitting orders.
4. Start in signal-only mode:

   ```powershell
   python .\alpaca_paper_bot.py --symbol SPY
   ```

   It prints PATS signals and sends no orders. Stop it with `Ctrl+C`.
5. To enable orders in the Alpaca paper account, run:

   ```powershell
   python .\alpaca_paper_bot.py --symbol SPY --submit-orders
   ```

`paper=True` is hard-coded in the Alpaca client. The screenshot endpoint includes `/v2`; `alpaca-py` selects the paper API host itself and appends the API route, so the script does not need a separate endpoint setting.

## What the runner does

- Replays recent closed bars to initialize EMA and PATS counting, then only acts on newly closed bars.
- Uses the attached defaults: EMA 21, trend lookback 12, EMA slope 3, 55% signal-bar close quality, six-tick target, eight-tick initial stop, two-tick bar-by-bar tightening, five trades per day, and 08:35–14:30 Central time.
- Places a stop-entry bracket order with a take-profit and stop-loss. An unfilled entry is canceled after its next five-minute bar. The bot cancels open orders and closes a position at the 14:30 Central cutoff.
- Uses an 85% buying-power ceiling. `PATS_SIZING_MODE=fixed` maps the Pine fixed-contract setting to shares; `PATS_SIZING_MODE=risk_percent` sizes from account equity and the stop distance.
- Refuses to start if the selected symbol already has a position or open order in the paper account. The local daily counter is saved in `.pats_paper_state.json`.

Keep the process running through the 14:30 Central cutoff. If you stop it or it exits unexpectedly before then, the attached bracket exits remain with Alpaca, but the bot can no longer tighten the stop or enforce the cutoff. Check and manage any open SPY orders or position in the paper dashboard before restarting.

Alpaca paper trading is a simulation. Its fills do not reproduce queue position, market impact, or all live slippage. The runner has not been connected to your account or used to submit a paper order because your private paper keys were not provided. Confirm the bracket-order behavior in Alpaca's paper dashboard before relying on the run.

## Files

- `pats_engine.py` — broker-independent PATS signal-state translation.
- `alpaca_paper_bot.py` — Alpaca market-data, paper-order, cutoff, and position-management loop.
- `.env.example` — paper-key and conversion settings template.
