import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { assetLabel, fmtTime } from '../format'
import { useStore } from '../store'
import type { AssetClass, ContractInfo, SymbolInfo } from '../types'

export default function DataPage() {
  const symbols = useStore((s) => s.symbols)
  const contracts = useStore((s) => s.contracts)
  const loadSymbols = useStore((s) => s.loadSymbols)
  const toast = useStore((s) => s.toast)
  const session = useStore((s) => s.session)

  const remove = async (s: SymbolInfo) => {
    if (!confirm(`Delete all stored bars for ${s.symbol}? This cannot be undone.`)) return
    try { await api.del(`/api/data/${s.symbol}`); await loadSymbols(); toast('info', `Deleted ${s.symbol} data`) }
    catch (e) { toast('error', (e as Error).message) }
  }

  return (
    <div className="page">
      <h1>Market data</h1>
      <p className="lead">Each instrument is stored as one continuous series of bars. Futures use the front-month contract with its traded prices; roll gaps are recorded so charts can show a back-adjusted view and open positions roll correctly. Crypto, forex, stocks and ETFs can be downloaded for free below.</p>
      <div className="panel" style={{ marginBottom: 16, padding: 0 }}>
        <table className="grid">
          <thead><tr><th>Symbol</th><th>Class</th><th>Instrument</th><th>Bars</th><th className="num">Rows</th><th>From (UTC)</th><th>To (UTC)</th><th>Rolls</th><th>Source</th><th /></tr></thead>
          <tbody>
            {symbols.map((s) => (
              <tr key={s.symbol}>
                <td><b>{s.symbol}</b></td>
                <td className="muted">{assetLabel(s.contract?.asset_class)}</td>
                <td className="muted" style={{ whiteSpace: 'normal', maxWidth: 420 }}>{s.contract?.name}{!s.synthetic && s.notes && <div className="faint" style={{ fontSize: 11 }}>{s.notes}</div>}</td>
                <td>{s.base_tf}</td>
                <td className="num">{s.rows.toLocaleString()}</td>
                <td>{fmtTime(s.first_ts, 'UTC')}</td>
                <td>{fmtTime(s.last_ts, 'UTC')}</td>
                <td title={s.rolls.map((r) => `${r.from_contract} -> ${r.to_contract} (${r.gap > 0 ? '+' : ''}${r.gap})`).join('\n')}>{s.contract?.asset_class === 'future' ? s.rolls.length : '-'}</td>
                <td>{s.synthetic ? <span className="badge synthetic">sample data</span> : s.source}</td>
                <td><button className="btn small danger" disabled={!!session?.symbols.includes(s.symbol)} onClick={() => remove(s)}>Delete</button></td>
              </tr>
            ))}
            {!symbols.length && <tr><td colSpan={10} className="empty">No data yet. Generate sample data or import a file below.</td></tr>}
          </tbody>
        </table>
      </div>
      <DownloadPanel contracts={contracts} symbols={symbols} />
      <div className="cols">
        <BarsPanel />
        <ImportPanel contracts={contracts.map((c) => c.symbol)} />
        <DatabentoPanel contracts={contracts.filter((c) => c.asset_class === 'future').map((c) => c.symbol)} />
        <GeneratePanel />
      </div>
    </div>
  )
}

function BarsPanel() {
  const loadSymbols = useStore((s) => s.loadSymbols)
  const toast = useStore((s) => s.toast)
  const [root, setRoot] = useState('ES')
  const [target, setTarget] = useState('MES')
  const [busy, setBusy] = useState(false)
  const go = async () => {
    setBusy(true)
    try {
      const r = await api.post<SymbolInfo>('/api/data/bars', { root, target })
      await loadSymbols()
      toast('info', `Imported ${r.rows.toLocaleString()} real 1-minute bars as ${r.symbol}; ${r.rolls.length} rolls repaired`)
    } catch (e) { toast('error', (e as Error).message) } finally { setBusy(false) }
  }
  return (
    <section className="panel">
      <h2>Free ES/NQ minute bars</h2>
      <p className="help">Real 1-minute E-mini history from the public <a href="https://github.com/kfccolonel1/BARS" target="_blank" rel="noreferrer">BARS</a> repository: ES from June 2025, NQ from June 2024. Every contract switch is detected and recorded as a roll, so positions held across it don't book the price gap as profit or loss. For ES, per-contract files also rebuild the thin expiry days with a volume roll. It replaces sample data for the chosen symbol.</p>
      <div className="row2">
        <label className="field"><span>Market</span>
          <select value={root} onChange={(e) => { setRoot(e.target.value); setTarget(e.target.value === 'ES' ? 'MES' : 'MNQ') }}>
            <option value="ES">S&P 500 (ES)</option><option value="NQ">Nasdaq-100 (NQ)</option>
          </select></label>
        <label className="field"><span>Store as</span>
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value={root === 'ES' ? 'MES' : 'MNQ'}>{root === 'ES' ? 'MES (micro specs)' : 'MNQ (micro specs)'}</option>
            <option value={root}>{root} (E-mini specs)</option>
          </select></label>
      </div>
      <p className="help">Personal research only: the repository has no license, and its data is stitched from several sources. Downloads about 40 MB.</p>
      <button className="btn primary" disabled={busy} onClick={go}>{busy ? 'Downloading and importing...' : 'Download and import'}</button>
    </section>
  )
}

function ImportPanel({ contracts }: { contracts: string[] }) {
  const loadSymbols = useStore((s) => s.loadSymbols)
  const toast = useStore((s) => s.toast)
  const [symbol, setSymbol] = useState('MES')
  const [format, setFormat] = useState('auto')
  const [tz, setTz] = useState('UTC')
  const [barTime, setBarTime] = useState('open')
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const go = async () => {
    if (!file) return
    setBusy(true)
    try {
      const q = new URLSearchParams({ symbol, format, tz, bar_time: barTime, filename: file.name })
      const r = await api.upload<SymbolInfo>(`/api/data/import?${q}`, file)
      await loadSymbols()
      toast('info', `Imported ${r.rows.toLocaleString()} ${r.base_tf} bars for ${r.symbol}`)
    } catch (e) { toast('error', (e as Error).message) } finally { setBusy(false) }
  }
  return (
    <section className="panel">
      <h2>Import a file</h2>
      <p className="help">CSV exports from TradingView, NinjaTrader or Databento, any CSV with time/open/high/low/close/volume columns, or a Databento .dbn.zst file. Files with several contracts are stitched into a continuous series automatically.</p>
      <label className="field"><span>File</span><input type="file" accept=".csv,.txt,.zst,.dbn" onChange={(e) => setFile(e.target.files?.[0] ?? null)} /></label>
      <div className="row2">
        <label className="field"><span>Instrument</span>
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>{contracts.map((c) => <option key={c}>{c}</option>)}</select></label>
        <label className="field"><span>Format</span>
          <select value={format} onChange={(e) => setFormat(e.target.value)}>
            <option value="auto">Detect automatically</option><option value="tradingview">TradingView</option>
            <option value="ninjatrader">NinjaTrader</option><option value="databento">Databento CSV</option><option value="generic">Generic CSV</option>
          </select></label>
      </div>
      <div className="row2">
        <label className="field"><span>Time zone of times without an offset</span>
          <select value={tz} onChange={(e) => setTz(e.target.value)}>
            <option>UTC</option><option>America/Chicago</option><option>America/New_York</option><option>Europe/Vilnius</option><option>Europe/London</option>
          </select></label>
        <label className="field"><span>Timestamps mark the bar</span>
          <select value={barTime} onChange={(e) => setBarTime(e.target.value)}><option value="open">Open</option><option value="close">Close</option></select></label>
      </div>
      <button className="btn primary" disabled={!file || busy} onClick={go}>{busy ? 'Importing...' : 'Import'}</button>
    </section>
  )
}

function DatabentoPanel({ contracts }: { contracts: string[] }) {
  const loadSymbols = useStore((s) => s.loadSymbols)
  const toast = useStore((s) => s.toast)
  const [symbol, setSymbol] = useState('MES')
  const [start, setStart] = useState('2025-01-01')
  const [end, setEnd] = useState(new Date(Date.now() - 86400000).toISOString().slice(0, 10))
  const [key, setKey] = useState('')
  const [cost, setCost] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const body = (confirm: boolean) => ({ symbol, start, end, api_key: key || null, confirm })
  const estimate = async () => {
    setBusy(true)
    try { const r = await api.post<{ cost_usd: number }>('/api/data/databento', body(false)); setCost(r.cost_usd) }
    catch (e) { toast('error', (e as Error).message) } finally { setBusy(false) }
  }
  const download = async () => {
    setBusy(true)
    try { const r = await api.post<SymbolInfo>('/api/data/databento', body(true)); await loadSymbols(); toast('info', `Downloaded ${r.rows.toLocaleString()} bars for ${r.symbol}`); setCost(null) }
    catch (e) { toast('error', (e as Error).message) } finally { setBusy(false) }
  }
  return (
    <section className="panel">
      <h2>Download CME futures from Databento</h2>
      <p className="help">Real 1-minute bars for every contract month, stitched by volume roll. New Databento accounts include free credit. Put your key in <code>.env</code> as DATABENTO_API_KEY or paste it here (it is not stored).</p>
      <div className="row3" style={{ gridTemplateColumns: '0.8fr 1fr 1fr' }}>
        <label className="field"><span>Instrument</span>
          <select value={symbol} onChange={(e) => { setSymbol(e.target.value); setCost(null) }}>{contracts.map((c) => <option key={c}>{c}</option>)}</select></label>
        <label className="field"><span>From</span><input type="date" value={start} onChange={(e) => { setStart(e.target.value); setCost(null) }} /></label>
        <label className="field"><span>To</span><input type="date" value={end} onChange={(e) => { setEnd(e.target.value); setCost(null) }} /></label>
      </div>
      <label className="field"><span>API key (optional if set in .env)</span><input type="password" value={key} onChange={(e) => setKey(e.target.value)} autoComplete="off" /></label>
      {cost === null
        ? <button className="btn" disabled={busy} onClick={estimate}>{busy ? 'Checking...' : 'Check cost'}</button>
        : <><p className="help">Estimated cost: <b>${cost.toFixed(2)}</b> (charged to your Databento account or free credit).</p>
          <button className="btn primary" disabled={busy} onClick={download}>{busy ? 'Downloading...' : `Download for $${cost.toFixed(2)}`}</button></>}
    </section>
  )
}

function GeneratePanel() {
  const loadSymbols = useStore((s) => s.loadSymbols)
  const toast = useStore((s) => s.toast)
  const [days, setDays] = useState(365)
  const [seed, setSeed] = useState(6)
  const [busy, setBusy] = useState(false)
  const go = async () => {
    setBusy(true)
    try { await api.post('/api/data/generate', { symbols: ['MES', 'MNQ'], days, seed }); await loadSymbols(); toast('info', 'Sample data generated') }
    catch (e) { toast('error', (e as Error).message) } finally { setBusy(false) }
  }
  return (
    <section className="panel">
      <h2>Generate sample data</h2>
      <p className="help">Synthetic MES and MNQ minute bars on the real CME schedule, with an opening-bell volatility spike, news jumps, holidays and quarterly rolls. Useful for practice and testing; it replaces any existing MES/MNQ series.</p>
      <div className="row2">
        <label className="field"><span>Days</span><input type="number" min={5} max={1500} value={days} onChange={(e) => setDays(+e.target.value)} /></label>
        <label className="field"><span>Random seed</span><input type="number" value={seed} onChange={(e) => setSeed(+e.target.value)} /></label>
      </div>
      <button className="btn" disabled={busy} onClick={go}>{busy ? 'Generating...' : 'Generate'}</button>
    </section>
  )
}

// ---------------------------------------------------------------- free downloads (crypto, forex, stocks, ETFs)

interface JobResult { symbol: string; ok: boolean; rows?: number; source?: string; first_ts?: number; last_ts?: number; notes?: string; error?: string }
interface Job { id: string; status: 'running' | 'done'; symbols: string[]; progress: number; message: string; current: string | null; results: JobResult[] }
interface SourcesInfo { alpaca_key_set: boolean; yahoo_intervals: string[] }

const CLASSES: { id: AssetClass; label: string; help: string }[] = [
  { id: 'crypto', label: 'Crypto', help: 'Binance USD-M perpetual futures from the public archive (data.binance.vision): 1-minute bars back to 2020, no account needed. Trades 24/7; also available for live paper trading.' },
  { id: 'forex', label: 'Forex & gold', help: 'Dukascopy bank feed: 1-minute bid and ask candles for the major pairs and spot gold, no account needed. Stored as the mid price; orders pay half the spread like at a broker, and the real median spread is measured so you can compare it with the contract settings. About 1 MB per pair per month.' },
  { id: 'stock', label: 'Stocks', help: 'US stocks with pre-market and after-hours, split-adjusted. Alpaca gives years of 1-minute bars with a free account key; without a key Yahoo Finance gives the last 30 days of 1-minute bars (or 60 days of 5-minute, 2 years of hourly).' },
  { id: 'etf', label: 'ETFs', help: 'Index, sector, gold and bond ETFs with the same sources as stocks. SPY and QQQ are the most liquid way to trade the S&P 500 and Nasdaq-100 outside futures.' },
]

const isoDay = (d: Date) => d.toISOString().slice(0, 10)

function DownloadPanel({ contracts, symbols }: { contracts: ContractInfo[]; symbols: SymbolInfo[] }) {
  const loadSymbols = useStore((s) => s.loadSymbols)
  const toast = useStore((s) => s.toast)
  const [cls, setCls] = useState<AssetClass>('crypto')
  const [picked, setPicked] = useState<Record<string, string[]>>({ crypto: ['BTCUSDT'], forex: ['EURUSD'], stock: ['AAPL'], etf: ['SPY'] })
  const [start, setStart] = useState(isoDay(new Date(Date.now() - 365 * 86400000)))
  const [end, setEnd] = useState(isoDay(new Date()))
  const [stockSource, setStockSource] = useState<'alpaca' | 'yahoo'>('yahoo')
  const [interval, setIntervalTf] = useState('1m')
  const [spread, setSpread] = useState(true)
  const [key, setKey] = useState('')
  const [secret, setSecret] = useState('')
  const [info, setInfo] = useState<SourcesInfo | null>(null)
  const [job, setJob] = useState<Job | null>(null)
  const timer = useRef<number | null>(null)

  const poll = (id: string) => {
    if (timer.current) window.clearTimeout(timer.current)
    const tick = async () => {
      try {
        const j = await api.get<Job>(`/api/data/jobs/${id}`)
        setJob(j)
        if (j.status === 'running') { timer.current = window.setTimeout(tick, 1000); return }
        await loadSymbols()
        const ok = j.results.filter((r) => r.ok), bad = j.results.filter((r) => !r.ok)
        if (ok.length) toast('info', `Downloaded ${ok.map((r) => `${r.symbol} (${(r.rows ?? 0).toLocaleString()} bars)`).join(', ')}`)
        if (bad.length) toast('error', `Failed: ${bad.map((r) => `${r.symbol}: ${r.error}`).join('; ')}`)
      } catch { timer.current = window.setTimeout(tick, 2000) }
    }
    tick()
  }

  useEffect(() => {
    api.get<SourcesInfo>('/api/data/sources').then((i) => { setInfo(i); if (i.alpaca_key_set) setStockSource('alpaca') }).catch(() => {})
    api.get<Job[]>('/api/data/jobs').then((js) => { const r = js.find((j) => j.status === 'running'); if (r) { setJob(r); poll(r.id) } }).catch(() => {})
    return () => { if (timer.current) window.clearTimeout(timer.current) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const list = contracts.filter((c) => c.asset_class === cls)
  const sel = (picked[cls] ?? []).filter((s) => list.some((c) => c.symbol === s))
  const have = new Map(symbols.map((s) => [s.symbol, s]))
  const toggle = (sym: string) => setPicked({ ...picked, [cls]: sel.includes(sym) ? sel.filter((x) => x !== sym) : [...sel, sym] })
  const meta = CLASSES.find((c) => c.id === cls)!
  const isStock = cls === 'stock' || cls === 'etf'
  const source = cls === 'crypto' ? 'binance' : cls === 'forex' ? 'dukascopy' : stockSource
  const running = job?.status === 'running'
  const yahooBack: Record<string, number> = { '1m': 29, '2m': 59, '5m': 59, '15m': 59, '30m': 59, '1h': 729, '1d': 36500 }
  const est = (() => {
    const days = Math.max(1, (new Date(end).getTime() - new Date(start).getTime()) / 86400000)
    if (source === 'dukascopy') return `about ${Math.ceil(days * 6 / 7 * (spread ? 2 : 1))} small files per pair`
    if (source === 'binance') return `${Math.ceil(days / 30)} monthly files per coin`
    return ''
  })()

  const go = async () => {
    if (!sel.length) return
    try {
      const j = await api.post<Job>('/api/data/download', {
        symbols: sel, source, start, end, interval: source === 'yahoo' ? interval : '1m', spread,
        key: key || null, secret: secret || null,
      })
      setJob(j)
      poll(j.id)
    } catch (e) { toast('error', (e as Error).message) }
  }

  return (
    <section className="panel" style={{ marginBottom: 16 }}>
      <h2>Download free market data</h2>
      <div className="seg" role="tablist" aria-label="Asset class" style={{ margin: '8px 0 10px' }}>
        {CLASSES.map((c) => <button key={c.id} role="tab" aria-pressed={cls === c.id} onClick={() => setCls(c.id)}>{c.label}</button>)}
      </div>
      <p className="help">{meta.help}</p>
      <div className="chips" role="group" aria-label="Instruments">
        {list.map((c) => {
          const h = have.get(c.symbol)
          return (
            <button key={c.symbol} className="chip" aria-pressed={sel.includes(c.symbol)} onClick={() => toggle(c.symbol)}
              title={`${c.name}${h ? `\nStored: ${h.rows.toLocaleString()} ${h.base_tf} bars to ${fmtTime(h.last_ts, 'UTC')} UTC (${h.source})` : ''}`}>
              {h && !h.synthetic && <span className="have" aria-label="already stored" />}{c.symbol}
            </button>
          )
        })}
        <button className="chip" onClick={() => setPicked({ ...picked, [cls]: sel.length === list.length ? [] : list.map((c) => c.symbol) })}>
          {sel.length === list.length ? 'Clear' : 'All'}
        </button>
      </div>
      <div style={{ maxWidth: 820 }}>
      <div className="row3" style={{ gridTemplateColumns: isStock ? '1.4fr 1fr 1fr' : '1fr 1fr' }}>
        {isStock && (
          <label className="field"><span>Source</span>
            <select value={stockSource} onChange={(e) => setStockSource(e.target.value as 'alpaca' | 'yahoo')}>
              <option value="alpaca">Alpaca: years of 1-minute bars (free key)</option>
              <option value="yahoo">Yahoo Finance: no key, recent data only</option>
            </select></label>
        )}
        <label className="field"><span>From</span><input type="date" value={start} disabled={source === 'yahoo'} onChange={(e) => setStart(e.target.value)} /></label>
        <label className="field"><span>To</span><input type="date" value={end} disabled={source === 'yahoo'} onChange={(e) => setEnd(e.target.value)} /></label>
      </div>
      {source === 'yahoo' && (
        <div className="row2">
          <label className="field"><span>Bar size</span>
            <select value={interval} onChange={(e) => setIntervalTf(e.target.value)}>
              {(info?.yahoo_intervals ?? ['1m', '5m', '15m', '1h', '1d']).map((i) => <option key={i} value={i}>{i} (last {yahooBack[i] >= 36500 ? 'all years' : `${yahooBack[i] + 1} days`})</option>)}
            </select></label>
          <p className="help" style={{ alignSelf: 'end' }}>Replay and live sessions need 1-minute bars. Yahoo's data is unofficial and for personal use.</p>
        </div>
      )}
      {source === 'alpaca' && !info?.alpaca_key_set && (
        <>
          <p className="help">Sign up free at <a href="https://alpaca.markets" target="_blank" rel="noreferrer">alpaca.markets</a>, create API keys (paper keys work) and put them in <code>.env</code> as ALPACA_API_KEY and ALPACA_API_SECRET, or paste them here (they are not stored).</p>
          <div className="row2">
            <label className="field"><span>API key ID</span><input value={key} onChange={(e) => setKey(e.target.value)} autoComplete="off" /></label>
            <label className="field"><span>Secret key</span><input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} autoComplete="off" /></label>
          </div>
        </>
      )}
      {cls === 'forex' && (
        <label className="check"><input type="checkbox" checked={spread} onChange={(e) => setSpread(e.target.checked)} />
          Download ask prices too: store mid prices and measure the real spread (twice the downloads)</label>
      )}
      </div>
      <p className="help">Running it again later only adds what is new. A different source replaces the stored series. {est && `Size: ${est}.`}</p>
      <button className="btn primary" disabled={running || !sel.length || (source === 'alpaca' && !info?.alpaca_key_set && !(key && secret))} onClick={go}>
        {running ? 'Downloading...' : `Download ${sel.length ? sel.join(', ') : ''}`}
      </button>
      {job && (
        <div aria-live="polite">
          {running && <>
            <div className="progress" role="progressbar" aria-valuenow={Math.round(job.progress * 100)} aria-valuemin={0} aria-valuemax={100}><div style={{ width: `${Math.max(2, job.progress * 100)}%` }} /></div>
            <div className="help">{Math.round(job.progress * 100)}%  {job.message}</div>
          </>}
          {!!job.results.length && (
            <ul className="result-list">
              {job.results.map((r) => (
                <li key={r.symbol}>
                  {r.ok
                    ? <><b className="up">{r.symbol}</b> {(r.rows ?? 0).toLocaleString()} bars, {fmtTime(r.first_ts, 'UTC').slice(0, 10)} to {fmtTime(r.last_ts, 'UTC').slice(0, 10)} <span className="faint">{r.source}</span>{r.notes && <div className="faint" style={{ fontSize: 11 }}>{r.notes}</div>}</>
                    : <><b className="down">{r.symbol}</b> {r.error}</>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}
