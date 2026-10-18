import { useState } from 'react'
import { api } from '../api'
import { fmtMoney, fmtNum, fmtPrice, fmtTime, pnlClass, tradeTicks, usdPerPoint } from '../format'
import { useStore } from '../store'
import { flatten } from './OrderTicket'
import StrategyPanel from './StrategyPanel'

type Tab = 'positions' | 'orders' | 'fills' | 'trades' | 'days' | 'bots'

export default function BottomPanel() {
  const [tab, setTab] = useState<Tab>('positions')
  const positions = useStore((s) => s.positions)
  const orders = useStore((s) => s.orders)
  const fills = useStore((s) => s.fills)
  const trades = useStore((s) => s.trades)
  const days = useStore((s) => s.days)
  const session = useStore((s) => s.session)
  const open = Object.values(positions).filter((p) => p.qty)
  const working = Object.values(orders).sort((a, b) => b.id - a.id)
  const tabs: [Tab, string, number][] = [
    ['positions', 'Positions', open.length], ['orders', 'Working orders', working.length], ['fills', 'Fills', fills.length],
    ['trades', 'Trades', trades.length], ['days', 'Daily P&L', days.length],
    ['bots', 'Strategies', session?.strategies?.filter((x) => x.status === 'running').length ?? 0],
  ]
  return (
    <section className="bottom" aria-label="Account activity">
      <div className="tabs" role="tablist">
        {tabs.map(([k, l, n]) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)}>{l}<span className="count">{n || ''}</span></button>
        ))}
        <span className="spacer" />
        {session && <a className="btn small" href="/api/journal.csv" download>Export trades CSV</a>}
        {session && <a className="btn small" href="/api/fills.csv" download style={{ marginLeft: 6 }}>Export fills CSV</a>}
      </div>
      <div className="table-wrap">
        {!session && tab !== 'bots' && <div className="empty">Start a replay or live session to see positions, orders and fills here.</div>}
        {tab === 'bots' && <StrategyPanel />}
        {session && tab === 'positions' && <Positions />}
        {session && tab === 'orders' && <Orders />}
        {session && tab === 'fills' && <Fills />}
        {session && tab === 'trades' && <Trades />}
        {session && tab === 'days' && <Days />}
      </div>
    </section>
  )
}

function useDec() {
  const contracts = useStore((s) => s.contracts)
  return (sym: string) => contracts.find((c) => c.symbol === sym)?.price_decimals ?? 2
}

function useTz() {
  const contracts = useStore((s) => s.contracts)
  return (sym: string) => contracts.find((c) => c.symbol === sym)?.session.tz ?? 'UTC'
}

function Positions() {
  const positions = useStore((s) => s.positions)
  const last = useStore((s) => s.last)
  const contracts = useStore((s) => s.contracts)
  const dec = useDec()
  const rows = Object.values(positions).filter((p) => p.qty)
  if (!rows.length) return <div className="empty">Flat. Use the order ticket, the chart's right-click menu or the hotkeys to open a position.</div>
  return (
    <table className="grid">
      <thead><tr><th>Symbol</th><th className="num">Qty</th><th className="num">Avg price</th><th className="num">Last</th>
        <th className="num">Open P&L</th><th className="num">Ticks</th><th className="num">Realized</th><th /></tr></thead>
      <tbody>
        {rows.map((p) => {
          const c = contracts.find((x) => x.symbol === p.symbol)
          const lp = last[p.symbol]
          const pnl = c && lp !== undefined ? (lp - p.avg_price) * p.qty * usdPerPoint(c, lp) : 0
          const ticks = c && lp !== undefined ? ((lp - p.avg_price) / c.tick_size) * Math.sign(p.qty) : 0
          return (
            <tr key={p.symbol}>
              <td>{p.symbol}</td>
              <td className={`num ${p.qty > 0 ? 'up' : 'down'}`}>{p.qty > 0 ? '+' : ''}{p.qty}</td>
              <td className="num">{fmtPrice(p.avg_price, dec(p.symbol))}</td>
              <td className="num">{fmtPrice(lp, dec(p.symbol))}</td>
              <td className={`num ${pnlClass(pnl)}`}>{fmtMoney(pnl, true)}</td>
              <td className={`num ${pnlClass(ticks)}`}>{fmtNum(ticks, 1)}</td>
              <td className={`num ${pnlClass(p.realized)}`}>{fmtMoney(p.realized, true)}</td>
              <td><button className="btn small" onClick={() => flatten(p.symbol)}>Close</button></td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function Orders() {
  const orders = useStore((s) => s.orders)
  const dec = useDec()
  const tz = useTz()
  const rows = Object.values(orders).sort((a, b) => b.id - a.id)
  if (!rows.length) return <div className="empty">No working orders.</div>
  const kind: Record<string, string> = { market: 'Market', limit: 'Limit', stop: 'Stop', stop_limit: 'Stop limit', trailing_stop: 'Trailing stop' }
  return (
    <table className="grid">
      <thead><tr><th>#</th><th>Time</th><th>Symbol</th><th>Side</th><th>Type</th><th className="num">Qty</th><th className="num">Price</th>
        <th className="num">Stop</th><th>TIF</th><th>Role</th><th>Status</th><th /></tr></thead>
      <tbody>
        {rows.map((o) => (
          <tr key={o.id}>
            <td className="faint">{o.id}</td>
            <td>{fmtTime(o.created_ts, tz(o.symbol), false)}</td>
            <td>{o.symbol}</td>
            <td className={o.side === 'buy' ? 'up' : 'down'}>{o.side === 'buy' ? 'Buy' : 'Sell'}</td>
            <td>{kind[o.type]}{o.trail_ticks ? ` ${o.trail_ticks}t` : ''}</td>
            <td className="num">{o.qty}</td>
            <td className="num">{o.price !== null ? fmtPrice(o.price, dec(o.symbol)) : ''}</td>
            <td className="num">{o.stop !== null ? fmtPrice(o.stop, dec(o.symbol)) : ''}</td>
            <td>{o.tif.toUpperCase()}</td>
            <td className="muted">{o.tag === 'tp' ? 'Take profit' : o.tag === 'sl' ? 'Stop loss' : o.tag}</td>
            <td className={o.status === 'pending' ? 'faint' : ''}>{o.status === 'pending' ? 'Waits for entry' : 'Working'}{o.triggered ? ', triggered' : ''}</td>
            <td><button className="btn small" onClick={() => api.del(`/api/orders/${o.id}`).catch((e) => useStore.getState().toast('error', e.message))}>Cancel</button></td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Fills() {
  const fills = useStore((s) => s.fills)
  const dec = useDec()
  const tz = useTz()
  if (!fills.length) return <div className="empty">No fills yet.</div>
  return (
    <table className="grid">
      <thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th className="num">Qty</th><th className="num">Price</th>
        <th className="num">Fee</th><th className="num">Realized</th><th className="num">Position after</th><th>Source</th></tr></thead>
      <tbody>
        {fills.map((f) => (
          <tr key={f.id}>
            <td>{fmtTime(f.ts, tz(f.symbol), true, true)}</td>
            <td>{f.symbol}</td>
            <td className={f.side === 'buy' ? 'up' : 'down'}>{f.side === 'buy' ? 'Buy' : 'Sell'}</td>
            <td className="num">{f.qty}</td>
            <td className="num">{fmtPrice(f.price, dec(f.symbol))}</td>
            <td className="num">{fmtMoney(f.fee)}</td>
            <td className={`num ${pnlClass(f.realized)}`}>{f.realized ? fmtMoney(f.realized, true) : ''}</td>
            <td className="num">{f.position_after}</td>
            <td className="muted">{f.tag}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Trades() {
  const trades = useStore((s) => s.trades)
  const contracts = useStore((s) => s.contracts)
  const dec = useDec()
  const tz = useTz()
  if (!trades.length) return <div className="empty">Closed round trips appear here with their MAE and MFE.</div>
  const net = trades.reduce((a, t) => a + t.net, 0)
  const wins = trades.filter((t) => t.net > 0).length
  return (
    <>
      <div className="stats" style={{ padding: '0 10px', margin: '8px 0' }}>
        <div className="stat"><span>Net P&L</span><strong className={pnlClass(net)}>{fmtMoney(net, true)}</strong></div>
        <div className="stat"><span>Trades</span><strong>{trades.length}</strong></div>
        <div className="stat"><span>Win rate</span><strong>{((wins / trades.length) * 100).toFixed(1)}%</strong></div>
        <div className="stat"><span>Fees</span><strong>{fmtMoney(trades.reduce((a, t) => a + t.fees, 0))}</strong></div>
      </div>
      <table className="grid">
        <thead><tr><th>Entry</th><th>Exit</th><th>Symbol</th><th>Side</th><th className="num">Qty</th><th className="num">Entry px</th>
          <th className="num">Exit px</th><th className="num">Ticks</th><th className="num">Net</th><th className="num">MAE</th><th className="num">MFE</th><th>Exit</th></tr></thead>
        <tbody>
          {trades.slice().reverse().map((t) => {
            const c = contracts.find((x) => x.symbol === t.symbol)
            const ticks = tradeTicks(t, c)
            return (
              <tr key={t.id}>
                <td>{fmtTime(t.entry_ts, tz(t.symbol))}</td>
                <td>{fmtTime(t.exit_ts, tz(t.symbol), false)}</td>
                <td>{t.symbol}</td>
                <td className={t.side === 'long' ? 'up' : 'down'}>{t.side === 'long' ? 'Long' : 'Short'}</td>
                <td className="num">{t.qty}</td>
                <td className="num">{fmtPrice(t.entry_price, dec(t.symbol))}</td>
                <td className="num">{fmtPrice(t.exit_price, dec(t.symbol))}</td>
                <td className={`num ${pnlClass(ticks)}`}>{fmtNum(ticks, 1)}</td>
                <td className={`num ${pnlClass(t.net)}`}>{fmtMoney(t.net, true)}</td>
                <td className="num down">{fmtNum(t.mae_ticks, 1)}</td>
                <td className="num up">{fmtNum(t.mfe_ticks, 1)}</td>
                <td className="muted">{t.exit_reason}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </>
  )
}

function Days() {
  const days = useStore((s) => s.days)
  if (!days.length) return <div className="empty">A row is added when each trading session closes.</div>
  return (
    <table className="grid">
      <thead><tr><th>Trade date</th><th className="num">P&L</th><th className="num">Realized</th><th className="num">Fees</th><th className="num">Equity</th></tr></thead>
      <tbody>
        {days.slice().reverse().map((d) => (
          <tr key={d.date}>
            <td>{d.date}</td>
            <td className={`num ${pnlClass(d.pnl)}`}>{fmtMoney(d.pnl, true)}</td>
            <td className={`num ${pnlClass(d.realized)}`}>{fmtMoney(d.realized, true)}</td>
            <td className="num">{fmtMoney(d.fees)}</td>
            <td className="num">{fmtMoney(d.equity)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
