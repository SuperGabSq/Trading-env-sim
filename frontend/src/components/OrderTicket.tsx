import { useEffect } from 'react'
import { api } from '../api'
import { fmtMoney, fmtPrice, pnlClass, qtyHint, qtyLabel, roundTick, tickValueUsd, usdPerPoint } from '../format'
import { useStore } from '../store'

export async function placeOrder(side: 'buy' | 'sell', override: Record<string, unknown> = {}) {
  const { symbol, ticket, toast, session } = useStore.getState()
  if (!session) { toast('warning', 'Start a replay or live session first'); return }
  const body: Record<string, unknown> = { symbol, side, qty: ticket.qty, type: ticket.type, tif: ticket.tif, ...override }
  const type = body.type as string
  if (type === 'limit') body.price = override.price ?? ticket.price
  if (type === 'stop') body.stop = override.stop ?? ticket.price
  if (ticket.bracket && !override.noBracket) { body.tp_ticks = ticket.tp; body.sl_ticks = ticket.sl }
  delete body.noBracket
  try {
    await api.post('/api/orders', body)
  } catch (e) {
    toast('error', (e as Error).message)
  }
}

export const flatten = (symbol?: string) =>
  api.post('/api/flatten', { symbol }).catch((e) => useStore.getState().toast('error', e.message))
export const cancelAll = (symbol?: string) =>
  api.post('/api/orders/cancel_all', { symbol }).catch((e) => useStore.getState().toast('error', e.message))
export const reverse = (symbol: string) =>
  api.post('/api/reverse', { symbol }).catch((e) => useStore.getState().toast('error', e.message))

export default function OrderTicket() {
  const symbol = useStore((s) => s.symbol)
  const ticket = useStore((s) => s.ticket)
  const setPrefs = useStore((s) => s.setPrefs)
  const contracts = useStore((s) => s.contracts)
  const session = useStore((s) => s.session)
  const last = useStore((s) => s.last[symbol])
  const pos = useStore((s) => s.positions[symbol])
  const orders = useStore((s) => s.orders)
  const quote = useStore((s) => s.quotes[symbol])
  const c = contracts.find((x) => x.symbol === symbol)
  const dec = c?.price_decimals ?? 2
  const tick = c?.tick_size ?? 0.25
  const canTrade = !!session && session.symbols.includes(symbol)
  const t = ticket
  const upd = (p: Partial<typeof ticket>) => setPrefs({ ticket: { ...ticket, ...p } })

  useEffect(() => {
    if (t.type !== 'market' && t.price === null && last !== undefined) upd({ price: last })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [t.type, last === undefined])

  const pnl = pos && pos.qty && last !== undefined && c ? (last - pos.avg_price) * pos.qty * usdPerPoint(c, last) : 0
  const working = Object.values(orders).filter((o) => o.symbol === symbol).length
  const tv = tickValueUsd(c, last)
  const riskPerContract = c ? t.sl * tv : 0
  const rewardPerContract = c ? t.tp * tv : 0
  const feeEst = c ? 2 * t.qty * (c.fees_per_side + (last !== undefined ? Math.abs(last) * usdPerPoint(c, last) * c.fees_per_side_rate : 0)) : 0
  const feeText = c ? [c.fees_per_side ? `$${c.fees_per_side}/${c.asset_class === 'stock' || c.asset_class === 'etf' ? 'share' : c.unit.startsWith('micro') ? 'micro lot' : 'contract'}` : '',
    c.fees_per_side_rate ? `${+(c.fees_per_side_rate * 100).toFixed(4)}% of notional` : ''].filter(Boolean).join(' + ') || 'none' : ''
  const tvText = c ? (tv >= 0.01 ? `$${+tv.toFixed(4)}` : `$${tv.toPrecision(2)}`) : ''

  return (
    <aside className="ticket" aria-label="Order ticket">
      <div className="ticket-inner">
        <h3><span>{symbol}</span><span className="num">{last !== undefined ? fmtPrice(last, dec) : ''}</span></h3>
        <div className="contract">{c ? `${c.name}  |  tick ${c.tick_size} = ${tvText}${c.quote !== 'USD' ? ' (at this rate)' : ''} per ${qtyLabel(c) === 'Shares' ? 'share' : qtyLabel(c) === 'Micro lots' ? 'micro lot' : qtyLabel(c) === 'Ounces' ? 'ounce' : 'contract'}  |  fee ${feeText} per side` : ''}</div>
        {quote && session?.mode === 'live' && (
          <div className="quote"><span>Bid <b className="down">{fmtPrice(quote.bid, dec)}</b></span><span>Ask <b className="up">{fmtPrice(quote.ask, dec)}</b></span>
            <span className="muted">Spread {Math.round((quote.ask - quote.bid) / tick)} tick{Math.round((quote.ask - quote.bid) / tick) === 1 ? '' : 's'}</span></div>
        )}

        <div className="seg" role="group" aria-label="Order type" style={{ width: '100%', marginBottom: 8 }}>
          {(['market', 'limit', 'stop'] as const).map((k) => (
            <button key={k} style={{ flex: 1 }} aria-pressed={t.type === k} onClick={() => upd({ type: k, price: k === 'market' ? null : (t.price ?? last ?? null) })}>
              {k[0].toUpperCase() + k.slice(1)}
            </button>
          ))}
        </div>

        <div className="row2">
          <label className="field"><span title={qtyHint(c, t.qty)}>{qtyLabel(c)}</span>
            <div className="qty">
              <button onClick={() => upd({ qty: Math.max(1, t.qty - 1) })} aria-label="Decrease quantity">-</button>
              <input type="number" min={1} value={t.qty} onChange={(e) => upd({ qty: Math.max(1, Math.floor(+e.target.value || 1)) })} />
              <button onClick={() => upd({ qty: t.qty + 1 })} aria-label="Increase quantity">+</button>
            </div>
          </label>
          <label className="field"><span>{t.type === 'stop' ? 'Stop price' : 'Limit price'}</span>
            <input type="number" step={tick} disabled={t.type === 'market'} value={t.type === 'market' ? '' : (t.price ?? '')}
              onChange={(e) => upd({ price: e.target.value === '' ? null : roundTick(+e.target.value, tick) })} />
          </label>
        </div>

        {qtyHint(c, t.qty) && <div className="help" style={{ marginTop: -4 }}>{qtyHint(c, t.qty)}{c && last !== undefined ? `  |  notional ${fmtMoney(Math.abs(last) * usdPerPoint(c, last) * t.qty)}` : ''}</div>}
        <label className="check">
          <input type="checkbox" checked={t.bracket} onChange={(e) => upd({ bracket: e.target.checked })} />
          Attach take-profit and stop-loss
        </label>
        {t.bracket && (
          <div className="row2">
            <label className="field"><span>Target (ticks)</span>
              <input type="number" min={1} value={t.tp} onChange={(e) => upd({ tp: Math.max(1, +e.target.value || 1) })} />
            </label>
            <label className="field"><span>Stop (ticks)</span>
              <input type="number" min={1} value={t.sl} onChange={(e) => upd({ sl: Math.max(1, +e.target.value || 1) })} />
            </label>
          </div>
        )}
        {t.bracket && c && (
          <div className="help">Risk {fmtMoney(riskPerContract * t.qty)} to make {fmtMoney(rewardPerContract * t.qty)} plus fees {fmtMoney(feeEst)}</div>
        )}
        <label className="field"><span>Time in force</span>
          <select value={t.tif} onChange={(e) => upd({ tif: e.target.value as 'gtc' | 'day' })}>
            <option value="gtc">Good till canceled</option>
            <option value="day">Day (expires at the session close)</option>
          </select>
        </label>

        <div className="buysell">
          <button className="buy" disabled={!canTrade} onClick={() => placeOrder('buy')}>
            <small>Buy {t.type === 'market' ? 'market' : t.type}</small><strong>{t.qty}</strong>
          </button>
          <button className="sell" disabled={!canTrade} onClick={() => placeOrder('sell')}>
            <small>Sell {t.type === 'market' ? 'market' : t.type}</small><strong>{t.qty}</strong>
          </button>
        </div>
        {!session && <div className="help">Start a replay or live session to place orders.</div>}

        <div className="pos-card">
          <div className="muted" style={{ fontSize: 12 }}>Position</div>
          {pos && pos.qty ? (
            <>
              <div className="big"><span className={pos.qty > 0 ? 'up' : 'down'}>{pos.qty > 0 ? 'Long' : 'Short'} {Math.abs(pos.qty)}</span> <span className="muted" style={{ fontSize: 13 }}>@ {fmtPrice(pos.avg_price, dec)}</span></div>
              <div className={pnlClass(pnl)} style={{ fontSize: 15, fontWeight: 600 }}>{fmtMoney(pnl, true)}</div>
            </>
          ) : <div className="muted">Flat</div>}
          <div className="pos-actions" style={{ marginTop: 8 }}>
            <button className="btn small" disabled={!canTrade} onClick={() => flatten(symbol)} title="Cancel orders and close the position">Flatten</button>
            <button className="btn small" disabled={!canTrade || !pos?.qty} onClick={() => reverse(symbol)}>Reverse</button>
            <button className="btn small" disabled={!canTrade || !working} onClick={() => cancelAll(symbol)}>Cancel {working || ''}</button>
          </div>
        </div>

        <div className="kbd">
          <kbd>Shift</kbd>+<kbd>B</kbd> buy  <kbd>Shift</kbd>+<kbd>S</kbd> sell  <kbd>Shift</kbd>+<kbd>F</kbd> flatten  <kbd>Shift</kbd>+<kbd>C</kbd> cancel all<br />
          <kbd>Space</kbd> play or pause  <kbd>→</kbd> step one bar  Right-click the chart for limit and stop orders. Drag order lines to move them.
        </div>
      </div>
    </aside>
  )
}
