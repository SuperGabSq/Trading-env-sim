import { fmtMoney, pnlClass } from '../format'
import { useStore, type Page } from '../store'

const PAGES: [Page, string][] = [['trade', 'Trade'], ['backtest', 'Backtest'], ['data', 'Data'], ['bots', 'Bots & API']]

export default function TopBar() {
  const page = useStore((s) => s.page)
  const set = useStore((s) => s.set)
  const account = useStore((s) => s.account)
  const session = useStore((s) => s.session)
  const connected = useStore((s) => s.connected)
  const theme = useStore((s) => s.theme)
  const setPrefs = useStore((s) => s.setPrefs)
  const a = session ? account : null
  return (
    <header className="topbar">
      <div className="brand">
        <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden><rect width="32" height="32" rx="6" fill="var(--raised)" /><path d="M7 21h4v-6H7zM14 25h4V9h-4zM21 18h4V7h-4z" fill="var(--amber)" /></svg>
        <span className="wordmark">Trading Env Sim</span>
      </div>
      <nav className="nav" aria-label="Main">
        {PAGES.map(([k, l]) => <button key={k} aria-current={page === k ? 'page' : undefined} onClick={() => set({ page: k })}>{l}</button>)}
      </nav>
      <span className="spacer" />
      {a && (
        <div className="acct" aria-label="Paper account">
          {a.locked && <span className="badge" style={{ color: 'var(--down)' }}>Locked: {a.locked}</span>}
          <div className="kv"><span>Equity</span><span>{fmtMoney(a.equity)}</span></div>
          <div className="kv"><span>Day P&L</span><span className={pnlClass(a.day_pnl)}>{fmtMoney(a.day_pnl, true)}</span></div>
          <div className="kv"><span>Open P&L</span><span className={pnlClass(a.unrealized)}>{fmtMoney(a.unrealized, true)}</span></div>
          <div className="kv"><span>Margin used</span><span>{fmtMoney(a.margin_used)}</span></div>
          <div className="kv"><span>Buying power</span><span>{fmtMoney(a.buying_power)}</span></div>
        </div>
      )}
      <button className="icon-btn" onClick={() => setPrefs({ theme: theme === 'dark' ? 'light' : 'dark' })}
        aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`} title="Theme">{theme === 'dark' ? '☀' : '☾'}</button>
      <span className={`conn ${connected ? 'ok' : ''}`} title={connected ? 'Connected to the simulator' : 'Disconnected - retrying'} role="status"
        aria-label={connected ? 'Connected' : 'Disconnected'} />
    </header>
  )
}
