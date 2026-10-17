import { useEffect, useState } from 'react'
import { api } from '../api'
import { fmtMoney, fmtNum, pnlClass } from '../format'
import { useStore } from '../store'

interface Run { id: string; created: number; strategy: string; symbol: string; timeframe: string; kind: string; summary: Record<string, number | null> | null }

const PY_LIVE = `from tsim_client import TsimClient      # clients/python/tsim_client.py

sim = TsimClient("http://127.0.0.1:8000")
print(sim.account())
sim.buy("MES", 1, tp_ticks=8, sl_ticks=8)            # market + bracket
sim.sell("MES", 1, type="limit", price=7250.25)
print(sim.positions(), sim.orders())
sim.flatten("MES")

# stream bars (with indicators), fills, orders and account updates
async for msg in sim.events(symbol="MES", tf="5m"):
    ...`

const PY_LOCK = `import asyncio
from tsim_client import Lockstep

async def main():
    async with Lockstep(symbol="MES", timeframe="5m",
                        start="2026-03-02", end="2026-03-31", name="my bot") as bt:
        async for step in bt:              # one step per closed 5m bar
            if step.position == 0 and step.bar["close"] > step.bar["open"]:
                bt.buy(1, tp_ticks=8, sl_ticks=8)   # acts from the next price
    print(bt.summary)                      # also saved to Backtest history

asyncio.run(main())`

const CURL = `curl http://127.0.0.1:8000/api/account
curl -X POST http://127.0.0.1:8000/api/orders -H "Content-Type: application/json" \\
     -d '{"symbol":"MES","side":"buy","qty":1,"type":"market","tp_ticks":8,"sl_ticks":8}'`

const ENDPOINTS: [string, string, string][] = [
  ['GET', '/api/symbols', 'Instruments with stored data and their contract specs'],
  ['GET', '/api/bars?symbol=MES&tf=5m&limit=500', 'Bars (never past the replay clock); add &indicators=[...]'],
  ['GET', '/api/session', 'Current replay/live session, clock and account'],
  ['POST', '/api/session/replay', 'Start a replay: {symbols, start, balance, speed, fill, risk}'],
  ['POST', '/api/session/live', 'Go live: {source: simulated|binance|ibkr, symbols, balance}'],
  ['POST', '/api/session/control', 'Replay clock: {action: play|pause|step|speed|jump}'],
  ['GET', '/api/account', 'Balance, equity, open P&L, margin, buying power'],
  ['GET', '/api/positions', 'Open positions with unrealized P&L'],
  ['GET', '/api/orders', 'Working orders (?all=true for history)'],
  ['POST', '/api/orders', 'Place: {symbol, side, qty, type, price, stop, tp_ticks, sl_ticks, tif, oco, tag}'],
  ['PATCH', '/api/orders/{id}', 'Modify price / stop / qty'],
  ['DELETE', '/api/orders/{id}', 'Cancel'],
  ['POST', '/api/flatten', 'Cancel orders and close positions ({symbol} optional)'],
  ['POST', '/api/session/strategies', 'Run a strategy from strategies/ inside the session'],
  ['POST', '/api/backtests', 'Queue a backtest of a strategy file'],
]

export default function BotsPage() {
  const [runs, setRuns] = useState<Run[]>([])
  const set = useStore((s) => s.set)
  useEffect(() => { api.get<Run[]>('/api/backtests').then((r) => setRuns(r.filter((x) => x.kind === 'bot'))).catch(() => {}) }, [])
  return (
    <div className="page">
      <h1>Bots & API</h1>
      <p className="lead">Connect your own bots, in any language, to the simulator as if it were a broker. They trade the same paper account you see on the Trade page, and their fills come from the same engine as replays, backtests and built-in strategies.</p>
      <div className="cols">
        <section className="panel">
          <h2>Trade a running session</h2>
          <p className="help">Start a replay or live session on the Trade page, then call the REST API. Orders tagged by your bot appear on the chart and in the journal. Subscribe to <code>/ws</code> for bars, fills and account updates.</p>
          <pre className="code">{PY_LIVE}</pre>
          <p className="help" style={{ marginTop: 10 }}>Plain HTTP works too:</p>
          <pre className="code">{CURL}</pre>
        </section>
        <section className="panel">
          <h2>Backtest a bot in lock-step</h2>
          <p className="help">Over <code>/ws/bot</code> the simulator replays history and waits for your bot after every bar, so an external bot gets exactly the fills a built-in strategy would. Results land in the Backtest history.</p>
          <pre className="code">{PY_LOCK}</pre>
          <p className="help" style={{ marginTop: 10 }}>Examples: <code>clients/python/examples/lockstep_ema_bot.py</code> and <code>live_rest_bot.py</code>. Protocol details: <a href="/api/bots/protocol" target="_blank" rel="noreferrer">/api/bots/protocol</a>.</p>
        </section>
      </div>
      <section className="panel" style={{ marginTop: 16, padding: 0 }}>
        <div style={{ padding: '12px 16px 4px', display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12, flexWrap: 'wrap' }}>
          <h2>Main endpoints</h2>
          <a href="/docs" target="_blank" rel="noreferrer">Full interactive API reference (OpenAPI)</a>
        </div>
        <table className="grid">
          <tbody>{ENDPOINTS.map(([m, p, d]) => <tr key={m + p}><td style={{ width: 70 }}><b>{m}</b></td><td><code>{p}</code></td><td className="muted" style={{ whiteSpace: 'normal' }}>{d}</td></tr>)}</tbody>
        </table>
      </section>
      <div className="cols" style={{ marginTop: 16 }}>
        <section className="panel">
          <h2>Access from another computer</h2>
          <p className="help">The simulator listens on 127.0.0.1 only. To reach it from another machine, set <code>TSIM_HOST=0.0.0.0</code> and a secret <code>TSIM_API_KEY</code> in <code>.env</code>. Remote bots then send the key as the <code>X-API-Key</code> header, or as <code>?key=</code> on <code>/ws/bot</code>. Local clients never need it.</p>
        </section>
        <section className="panel" style={{ padding: 0 }}>
          <div style={{ padding: '12px 16px 4px' }}><h2>Recent bot runs</h2></div>
          {runs.length ? (
            <table className="grid"><thead><tr><th>When</th><th>Bot</th><th>Market</th><th className="num">Trades</th><th className="num">Net</th><th className="num">PF</th></tr></thead>
              <tbody>{runs.slice(0, 10).map((r) => (
                <tr key={r.id} onClick={() => set({ page: 'backtest' })} style={{ cursor: 'pointer' }}>
                  <td>{new Date(r.created * 1000).toLocaleString()}</td><td>{r.strategy.replace('bot:', '')}</td><td>{r.symbol} {r.timeframe}</td>
                  <td className="num">{r.summary?.trades}</td><td className={`num ${pnlClass(r.summary?.net_pnl)}`}>{fmtMoney(r.summary?.net_pnl, true)}</td>
                  <td className="num">{fmtNum(r.summary?.profit_factor)}</td>
                </tr>))}</tbody></table>
          ) : <div className="empty">No lock-step bot runs yet.</div>}
        </section>
      </div>
    </div>
  )
}
