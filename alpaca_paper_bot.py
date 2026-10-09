"""Paper-only Alpaca runner for the attached MES-calibrated PATS rules.

SPY mapping defaults approximate one MES contract with 50 SPY shares and a
0.10 SPY price move per 1.00 MES index point. Those are adjustable proxy
assumptions, not exchange equivalences. Order submission is opt-in.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time as time_module
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrderByIdRequest,
    GetOrdersRequest,
    ReplaceOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)

from pats_engine import Bar, PatsSecondEntryEngine


CHICAGO = ZoneInfo("America/Chicago")
STATE_PATH = Path(__file__).with_name(".pats_paper_state.json")
MES_TICK = Decimal("0.25")
STOCK_TICK = Decimal("0.01")
MAX_BP_USAGE = Decimal("0.85")
POLL_SECONDS = 3


def price_to_tick(value: float | Decimal, direction: str = "nearest") -> float:
    amount = Decimal(str(value))
    rounding = {
        "up": ROUND_CEILING,
        "down": ROUND_FLOOR,
        "nearest": ROUND_HALF_UP,
    }[direction]
    return float((amount / STOCK_TICK).quantize(Decimal("1"), rounding=rounding) * STOCK_TICK)


def value_of(value: Any) -> str:
    return str(getattr(value, "value", value)).lower()


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logging.warning("Could not read saved bot state; starting with a fresh daily counter.")
    return {"trading_date": "", "trades_today": 0}


def save_state(state: dict[str, Any]) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, STATE_PATH)


def closed_bars(data_client: StockHistoricalDataClient, symbol: str) -> list[Bar]:
    now = datetime.now(timezone.utc)
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=now - timedelta(days=3),
        end=now,
        limit=1000,
        feed=DataFeed.IEX,
    )
    frame = data_client.get_stock_bars(request).df
    if frame.empty:
        return []
    if getattr(frame.index, "nlevels", 1) > 1 and "symbol" in (frame.index.names or []):
        frame = frame.xs(symbol, level="symbol")

    finalized_before = now - timedelta(seconds=5)
    bars: list[Bar] = []
    for timestamp, row in frame.iterrows():
        bar_time = timestamp.to_pydatetime() if hasattr(timestamp, "to_pydatetime") else timestamp
        if bar_time.tzinfo is None:
            bar_time = bar_time.replace(tzinfo=timezone.utc)
        if bar_time + timedelta(minutes=5) > finalized_before:
            continue
        bars.append(
            Bar(
                timestamp=bar_time,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0.0)),
            )
        )
    return sorted(bars, key=lambda item: item.timestamp)


def positions_for(trading: TradingClient, symbol: str) -> list[Any]:
    return [position for position in trading.get_all_positions() if position.symbol == symbol]


def open_orders_for(trading: TradingClient, symbol: str) -> list[Any]:
    request = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], limit=500)
    return trading.get_orders(filter=request)


def cancel_symbol_orders(trading: TradingClient, symbol: str) -> None:
    for order in open_orders_for(trading, symbol):
        try:
            trading.cancel_order_by_id(order.id)
        except Exception:
            logging.exception("Failed to cancel order %s", order.id)


def mapped_distance(ticks: int, point_ratio: Decimal) -> float:
    """Map Pine MES tick distances to stock dollars and round to cents."""
    return max(float(STOCK_TICK), price_to_tick(MES_TICK * point_ratio * ticks))


def make_order_prices(signal: Any, point_ratio: Decimal, config: dict[str, Any]) -> tuple[float, float, float]:
    entry_offset = Decimal(str(mapped_distance(1, point_ratio)))
    stop_offset = Decimal(str(mapped_distance(config["initial_stop_ticks"], point_ratio)))
    target_offset = Decimal(str(mapped_distance(config["target_ticks"], point_ratio)))
    if signal.side == "buy":
        entry = price_to_tick(Decimal(str(signal.bar.high)) + entry_offset, "up")
        stop = price_to_tick(Decimal(str(entry)) - stop_offset, "up")
        target = price_to_tick(Decimal(str(entry)) + target_offset, "up")
    else:
        entry = price_to_tick(Decimal(str(signal.bar.low)) - entry_offset, "down")
        stop = price_to_tick(Decimal(str(entry)) + stop_offset, "down")
        target = price_to_tick(Decimal(str(entry)) - target_offset, "down")
    return entry, stop, target


def calculate_qty(account: Any, sizing_mode: str, contracts: int, shares_per_contract: int,
                  risk_percent: float, stop_distance: float, price: float) -> int:
    buying_power = Decimal(str(account.buying_power))
    max_by_buying_power = int((buying_power * MAX_BP_USAGE / Decimal(str(price))).to_integral_value(rounding=ROUND_FLOOR))
    if max_by_buying_power < 1:
        return 0
    if sizing_mode == "risk_percent":
        risk_amount = Decimal(str(account.equity)) * Decimal(str(risk_percent)) / Decimal("100")
        desired = int((risk_amount / Decimal(str(stop_distance))).to_integral_value(rounding=ROUND_FLOOR))
    else:
        desired = contracts * shares_per_contract
    return max(0, min(desired, max_by_buying_power))


def get_bracket_stop(trading: TradingClient, parent_id: str) -> Any | None:
    parent = trading.get_order_by_id(parent_id, filter=GetOrderByIdRequest(nested=True))
    for leg in parent.legs or []:
        if value_of(leg.type) in {"stop", "stop_limit"}:
            return leg
    return None


def emergency_flatten(trading: TradingClient, symbol: str) -> None:
    cancel_symbol_orders(trading, symbol)
    time_module.sleep(1)
    if positions_for(trading, symbol):
        trading.close_position(symbol)


def run(symbol: str, submit_orders: bool) -> None:
    symbol = symbol.upper()
    load_dotenv()
    key = os.getenv("APCA_API_KEY_ID", "").strip()
    secret = os.getenv("APCA_API_SECRET_KEY", "").strip()
    if not key or not secret:
        raise SystemExit("Add your Alpaca paper API key and secret to a local .env file first.")

    config = {
        "fixed_contracts": int(os.getenv("PATS_FIXED_CONTRACTS", "2")),
        "shares_per_contract": int(os.getenv("PATS_SHARES_PER_MES_CONTRACT", "50")),
        "sizing_mode": os.getenv("PATS_SIZING_MODE", "fixed").strip().lower(),
        "risk_percent": float(os.getenv("PATS_RISK_PERCENT", "1.0")),
        "target_ticks": int(os.getenv("PATS_TARGET_TICKS", "6")),
        "initial_stop_ticks": int(os.getenv("PATS_INITIAL_STOP_TICKS", "8")),
        "tighten_ticks": int(os.getenv("PATS_TIGHTEN_TICKS", "2")),
        "max_trades_per_day": int(os.getenv("PATS_MAX_TRADES_PER_DAY", "5")),
    }
    point_ratio = Decimal(os.getenv("MES_POINT_TO_STOCK_DOLLARS", "0.10"))
    if config["sizing_mode"] not in {"fixed", "risk_percent"}:
        raise SystemExit("PATS_SIZING_MODE must be 'fixed' or 'risk_percent'.")
    if point_ratio <= 0:
        raise SystemExit("MES_POINT_TO_STOCK_DOLLARS must be positive.")

    # The SDK is deliberately constructed in paper mode; this runner has no live switch.
    trading = TradingClient(key, secret, paper=True)
    data = StockHistoricalDataClient(key, secret)

    existing_positions = positions_for(trading, symbol)
    existing_orders = open_orders_for(trading, symbol)
    if existing_positions or existing_orders:
        raise SystemExit(
            f"{symbol} already has a position or open order in this paper account. "
            "Manage/close it in Alpaca before starting this runner."
        )

    engine = PatsSecondEntryEngine()
    bars = closed_bars(data, symbol)
    if not bars:
        raise SystemExit("No finalized 5-minute bars returned. Check market hours and Alpaca data access.")
    for historical_bar in bars:
        engine.on_bar(historical_bar)
    last_bar_time = bars[-1].timestamp

    state = load_state()
    local_today = datetime.now(CHICAGO).date().isoformat()
    if state.get("trading_date") != local_today:
        state = {"trading_date": local_today, "trades_today": 0}
    state.setdefault("trades_today", 0)
    state["active_order_id"] = None
    state["pending_signal_bar"] = None
    state["entry_bar"] = None
    state["current_stop"] = None
    state["stop_order_id"] = None
    save_state(state)

    mode = "PAPER ORDERS ENABLED" if submit_orders else "DRY RUN — no orders will be submitted"
    logging.info("Starting %s on %s; feed=IEX; session=08:35–14:30 America/Chicago", mode, symbol)
    logging.info(
        "Proxy calibration: %s MES contracts x %s shares; MES point maps to $%s of %s price.",
        config["fixed_contracts"], config["shares_per_contract"], point_ratio, symbol,
    )
    if symbol != "SPY":
        logging.warning("The default conversion is intended for SPY; recalibrate it for %s before paper orders.", symbol)

    while True:
        now_ct = datetime.now(CHICAGO)
        current_date = now_ct.date().isoformat()
        if state.get("trading_date") != current_date:
            state = {"trading_date": current_date, "trades_today": 0}

        # Enforce the Pine cutoff even if no new market bar arrives at that instant.
        if now_ct.time() >= time(14, 30):
            if submit_orders and (positions_for(trading, symbol) or open_orders_for(trading, symbol)):
                emergency_flatten(trading, symbol)
                state["active_order_id"] = None
                state["pending_signal_bar"] = None
                state["entry_bar"] = None
                state["current_stop"] = None
                state["stop_order_id"] = None
                save_state(state)
                logging.info("Cutoff reached; canceled %s orders and flattened %s if needed.", symbol, symbol)
            break

        # Check fills between candles so bracket state and partial fills are not
        # left unattended until the next five-minute close.
        active_id = state.get("active_order_id")
        if submit_orders and active_id:
            parent = trading.get_order_by_id(active_id, filter=GetOrderByIdRequest(nested=True))
            status = value_of(parent.status)
            if status == "partially_filled":
                logging.error("Partial bracket entry detected; canceling and flattening to avoid an unmanaged partial position.")
                emergency_flatten(trading, symbol)
                state["active_order_id"] = None
                state["pending_signal_bar"] = None
                state["entry_bar"] = None
                state["stop_order_id"] = None
                state["current_stop"] = None
                save_state(state)
                raise RuntimeError("Runner stopped after a partial entry; review the paper account before restarting.")
            elif status == "filled":
                state["pending_signal_bar"] = None
                if not state.get("entry_bar") and parent.filled_at is not None:
                    fill_time = parent.filled_at
                    fill_time = fill_time if fill_time.tzinfo else fill_time.replace(tzinfo=timezone.utc)
                    state["entry_bar"] = fill_time.replace(
                        minute=(fill_time.minute // 5) * 5, second=0, microsecond=0
                    ).isoformat()
                    stop_leg = get_bracket_stop(trading, active_id)
                    if stop_leg is None:
                        raise RuntimeError("Filled bracket has no visible stop-loss leg; check Alpaca immediately.")
                    state["stop_order_id"] = str(stop_leg.id)
                if not positions_for(trading, symbol):
                    state["active_order_id"] = None
                    state["entry_bar"] = None
                    state["current_stop"] = None
                    state["stop_order_id"] = None
            elif status in {"canceled", "expired", "rejected", "done_for_day"}:
                state["active_order_id"] = None
                state["pending_signal_bar"] = None
                state["entry_bar"] = None
                state["stop_order_id"] = None
                state["current_stop"] = None
            save_state(state)

        fresh_bars = [bar for bar in closed_bars(data, symbol) if bar.timestamp > last_bar_time]
        for bar in fresh_bars:
            last_bar_time = bar.timestamp
            local_bar_close = (bar.timestamp + timedelta(minutes=5)).astimezone(CHICAGO)
            if local_bar_close.date().isoformat() != state.get("trading_date"):
                state = {"trading_date": local_bar_close.date().isoformat(), "trades_today": 0}

            # Do not place an order for a bar that arrived too late to trade its next bar.
            timely = datetime.now(timezone.utc) - (bar.timestamp + timedelta(minutes=5)) <= timedelta(seconds=20)
            signal = engine.on_bar(bar)
            position = positions_for(trading, symbol) if submit_orders else []

            active_id = state.get("active_order_id")
            if submit_orders and active_id:
                parent = trading.get_order_by_id(active_id, filter=GetOrderByIdRequest(nested=True))
                status = value_of(parent.status)
                if status == "partially_filled":
                    logging.error("Partial bracket entry detected; canceling and flattening to avoid an unmanaged partial position.")
                    emergency_flatten(trading, symbol)
                    state["active_order_id"] = None
                    state["pending_signal_bar"] = None
                    state["entry_bar"] = None
                    state["stop_order_id"] = None
                    state["current_stop"] = None
                    save_state(state)
                    raise RuntimeError("Runner stopped after a partial entry; review the paper account before restarting.")
                elif status == "filled":
                    state["pending_signal_bar"] = None
                    fill_time = parent.filled_at
                    if fill_time is not None and not state.get("entry_bar"):
                        fill_time = fill_time if fill_time.tzinfo else fill_time.replace(tzinfo=timezone.utc)
                        state["entry_bar"] = fill_time.replace(minute=(fill_time.minute // 5) * 5, second=0, microsecond=0).isoformat()
                    stop_leg = get_bracket_stop(trading, active_id)
                    if stop_leg is None:
                        raise RuntimeError("Filled bracket has no visible stop-loss leg; check Alpaca immediately.")
                    state["stop_order_id"] = str(stop_leg.id)
                    position = positions_for(trading, symbol)
                    if not position:
                        state["active_order_id"] = None
                        state["stop_order_id"] = None
                        state["current_stop"] = None
                elif status in {"canceled", "expired", "rejected", "done_for_day"}:
                    state["active_order_id"] = None
                    state["pending_signal_bar"] = None
                    state["entry_bar"] = None
                    state["stop_order_id"] = None
                    state["current_stop"] = None
                    position = positions_for(trading, symbol)

            # Cancel an unfilled entry at the close of its one permitted follow-up bar.
            pending_bar = state.get("pending_signal_bar")
            if submit_orders and active_id and pending_bar and bar.timestamp.isoformat() > pending_bar:
                parent = trading.get_order_by_id(active_id)
                if value_of(parent.status) in {"new", "accepted", "pending_new", "accepted_for_bidding"}:
                    trading.cancel_order_by_id(active_id)
                    state["active_order_id"] = None
                    state["pending_signal_bar"] = None
                    state["current_stop"] = None
                    logging.info("Canceled stale entry from %s", pending_bar)
                    position = positions_for(trading, symbol)

            # Tighten the attached stop once each fully closed bar after the entry bar.
            entry_bar = state.get("entry_bar")
            stop_id = state.get("stop_order_id")
            if submit_orders and position and entry_bar and stop_id and bar.timestamp.isoformat() > entry_bar:
                tighten_distance = Decimal(str(mapped_distance(config["tighten_ticks"], point_ratio)))
                is_long = value_of(position[0].side) == "long"
                candidate = (
                    Decimal(str(bar.low)) - tighten_distance
                    if is_long
                    else Decimal(str(bar.high)) + tighten_distance
                )
                old_stop = float(state["current_stop"])
                candidate = price_to_tick(candidate, "up" if is_long else "down")
                tighter = max(old_stop, candidate) if is_long else min(old_stop, candidate)
                if tighter != old_stop:
                    replaced = trading.replace_order_by_id(str(stop_id), ReplaceOrderRequest(stop_price=tighter))
                    state["stop_order_id"] = str(replaced.id)
                    state["current_stop"] = tighter

            position = positions_for(trading, symbol) if submit_orders else []
            if submit_orders and not position and state.get("entry_bar") and not state.get("pending_signal_bar"):
                state["active_order_id"] = None
                state["entry_bar"] = None
                state["current_stop"] = None
                state["stop_order_id"] = None

            if signal:
                entry, stop, target = make_order_prices(signal, point_ratio, config)
                logging.info("%s signal at %s: entry stop %.2f, protective stop %.2f, target %.2f",
                             signal.side.upper(), bar.timestamp.isoformat(), entry, stop, target)
                can_trade = (
                    timely
                    and engine.in_entry_window(bar)
                    and state["trades_today"] < config["max_trades_per_day"]
                    and not position
                    and not state.get("active_order_id")
                )
                if not can_trade:
                    continue
                if not submit_orders:
                    continue

                account = trading.get_account()
                qty = calculate_qty(
                    account,
                    config["sizing_mode"],
                    config["fixed_contracts"],
                    config["shares_per_contract"],
                    config["risk_percent"],
                    abs(entry - stop),
                    bar.close,
                )
                if qty <= 0:
                    logging.warning("Buying-power guard blocked this signal.")
                    continue
                side = OrderSide.BUY if signal.side == "buy" else OrderSide.SELL
                take_profit = TakeProfitRequest(limit_price=target)
                stop_loss = StopLossRequest(stop_price=stop)
                request = StopOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                    order_class=OrderClass.BRACKET,
                    stop_price=entry,
                    take_profit=take_profit,
                    stop_loss=stop_loss,
                    client_order_id=f"pats-{local_bar_close.strftime('%Y%m%d%H%M')}-{signal.side}",
                )
                order = trading.submit_order(order_data=request)
                state["active_order_id"] = str(order.id)
                state["pending_signal_bar"] = bar.timestamp.isoformat()
                state["trades_today"] += 1
                state["current_stop"] = stop
                logging.info("Submitted paper bracket %s, %s shares, parent order %s", symbol, qty, order.id)

            save_state(state)

        save_state(state)
        time_module.sleep(POLL_SECONDS)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Run the PATS strategy against Alpaca paper trading.")
    parser.add_argument("--symbol", default=os.getenv("PATS_SYMBOL", "SPY").upper(), help="Alpaca stock/ETF symbol (default: SPY)")
    parser.add_argument("--submit-orders", action="store_true", help="Submit orders to the paper account. Without this flag, prints signals only.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(args.symbol.upper(), args.submit_orders)


if __name__ == "__main__":
    main()
