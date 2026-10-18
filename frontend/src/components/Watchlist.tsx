import { useStore } from '../store'
import { fmtPrice, groupByClass } from '../format'

export default function Watchlist() {
  const symbols = useStore((s) => s.symbols)
  const symbol = useStore((s) => s.symbol)
  const last = useStore((s) => s.last)
  const session = useStore((s) => s.session)
  const positions = useStore((s) => s.positions)
  const setPrefs = useStore((s) => s.setPrefs)
  const set = useStore((s) => s.set)
  const groups = new Set(symbols.map((s) => s.contract?.asset_class ?? 'other')).size

  return (
    <aside className="watch" aria-label="Watchlist">
      <div className="section-title"><span>Watchlist</span></div>
      <div role="listbox" aria-label="Symbols">
        {groupByClass(symbols, (s) => s.contract?.asset_class).map((g) => [
          groups > 1 ? <div key={'g-' + g.id} className="watch-group" role="presentation">{g.label}</div> : null,
          ...g.items.map((s) => {
          const c = s.contract
          const px = last[s.symbol] ?? s.last_close ?? undefined
          const pos = positions[s.symbol]
          const inSess = session?.symbols.includes(s.symbol)
          return (
            <div key={s.symbol} className="watch-row" role="option" aria-selected={s.symbol === symbol} tabIndex={0}
              onClick={() => setPrefs({ symbol: s.symbol })}
              onKeyDown={(e) => { if (e.key === 'Enter') setPrefs({ symbol: s.symbol }) }}>
              <span className="sym">{s.symbol}</span>
              <span className="px">{px !== undefined ? fmtPrice(px, c?.price_decimals ?? 2) : ''}</span>
              <span className="sub">
                <span>{c?.name ?? ''}</span>
                <span>
                  {pos && pos.qty !== 0 && <span className={pos.qty > 0 ? 'up' : 'down'}>{pos.qty > 0 ? '+' : ''}{pos.qty} </span>}
                  {s.synthetic && <span className="badge synthetic" title="Generated sample data, not real prices">sample</span>}
                  {inSess && <span className="badge" style={{ marginLeft: 4 }}>{session?.mode}</span>}
                </span>
              </span>
            </div>
          )
        })])}
      </div>
      {!symbols.length && (
        <div className="empty">No market data yet. <button className="btn small" onClick={() => set({ page: 'data' })}>Open Data</button></div>
      )}
    </aside>
  )
}
