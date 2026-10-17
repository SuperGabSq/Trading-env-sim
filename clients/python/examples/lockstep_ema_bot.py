"""External bot backtested in lock-step: an EMA crossover computed in this process.

Run the simulator first (start.bat), then:
    .venv\\Scripts\\python clients\\python\\examples\\lockstep_ema_bot.py

The report also appears in the app's Backtest history as "Bot: ema bot".
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tsim_client import Lockstep  # noqa: E402

FAST, SLOW = 9, 21


def ema(prev, x, n):
    a = 2 / (n + 1)
    return x if prev is None else a * x + (1 - a) * prev


async def main():
    async with Lockstep(symbol="MES", timeframe="15m", start="2026-06-01", end="2026-09-30", name="ema bot") as bt:
        fast = slow = None
        for b in bt.warmup_bars:
            fast, slow = ema(fast, b["close"], FAST), ema(slow, b["close"], SLOW)
        async for step in bt:
            prev_diff = (fast - slow) if fast is not None else 0
            fast, slow = ema(fast, step.bar["close"], FAST), ema(slow, step.bar["close"], SLOW)
            diff = fast - slow
            if prev_diff <= 0 < diff and step.position <= 0:
                if step.position < 0:
                    bt.flatten()
                bt.buy(1, sl_ticks=16, tp_ticks=32)
            elif prev_diff >= 0 > diff and step.position >= 0:
                if step.position > 0:
                    bt.flatten()
                bt.sell(1, sl_ticks=16, tp_ticks=32)
            for f in step.fills:
                print(f"{f['side']:4} {f['qty']} @ {f['price']}  ({f['tag']})")
    s = bt.summary
    print(f"\ntrades {s['trades']}  net {s['net_pnl']}  win rate {s['win_rate']}%  PF {s['profit_factor']}  "
          f"max DD {s['max_drawdown']}")


if __name__ == "__main__":
    asyncio.run(main())
