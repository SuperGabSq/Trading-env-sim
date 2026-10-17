"""External bot trading a running replay or live session through the REST API + WebSocket events.

1. Start the app and a session (Trade page: Start replay, press Play; or Go live).
2. Run:  .venv\\Scripts\\python clients\\python\\examples\\live_rest_bot.py MES

The bot buys a 1-contract bracket whenever a 5-minute bar closes above the session VWAP while flat,
and sells when it closes below. Orders show up on the chart tagged "bot".
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tsim_client import TsimClient, TsimError  # noqa: E402

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "MES"


async def main():
    sim = TsimClient()
    if not sim.session():
        sys.exit("Start a replay or live session in the app first.")
    last_closed = None
    async for msg in sim.events(symbol=SYMBOL, tf="5m", indicators=[{"id": "vwap", "type": "vwap"}]):
        if msg["type"] != "bars":
            if msg["type"] == "fill":
                f = msg["data"]
                print(f"fill: {f['side']} {f['qty']} {f['symbol']} @ {f['price']} ({f['tag']})")
            continue
        bars = msg["bars"]
        if len(bars) < 2:      # a new bar starts -> the previous one in this batch is closed
            continue
        closed = bars[-2]
        if closed["time"] == last_closed:
            continue
        last_closed = closed["time"]
        vwap = dict(msg["indicators"].get("vwap", {}).get("value", [])).get(closed["time"])
        if vwap is None:
            continue
        pos = sim.position(SYMBOL)
        try:
            if pos == 0 and closed["close"] > vwap:
                sim.buy(SYMBOL, 1, tp_ticks=12, sl_ticks=8)
                print(f"long: close {closed['close']} > vwap {vwap:.2f}")
            elif pos == 0 and closed["close"] < vwap:
                sim.sell(SYMBOL, 1, tp_ticks=12, sl_ticks=8)
                print(f"short: close {closed['close']} < vwap {vwap:.2f}")
        except TsimError as e:
            print("order error:", e)


if __name__ == "__main__":
    asyncio.run(main())
