import { useState } from 'react'
import { api } from '../api'
import { fmtTime } from '../format'
import { useStore } from '../store'
import type { SymbolInfo } from '../types'

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
      <p className="lead">Each instrument is stored as one continuous front-month series. Prices are the traded prices of each contract; roll gaps are recorded so charts can show a back-adjusted view and open positions roll correctly.</p>
      <div className="panel" style={{ marginBottom: 16, padding: 0 }}>
        <table className="grid">
          <thead><tr><th>Symbol</th><th>Contract</th><th>Bars</th><th className="num">Rows</th><th>From (UTC)</th><th>To (UTC)</th><th>Rolls</th><th>Source</th><th /></tr></thead>
          <tbody>
            {symbols.map((s) => (
              <tr key={s.symbol}>
                <td><b>{s.symbol}</b></td>
                <td className="muted" style={{ whiteSpace: 'normal', maxWidth: 420 }}>{s.contract?.name}{!s.synthetic && s.notes && <div className="faint" style={{ fontSize: 11 }}>{s.notes}</div>}</td>
                <td>{s.base_tf}</td>
                <td className="num">{s.rows.toLocaleString()}</td>
                <td>{fmtTime(s.first_ts, 'UTC')}</td>
                <td>{fmtTime(s.last_ts, 'UTC')}</td>
                <td title={s.rolls.map((r) => `${r.from_contract} -> ${r.to_contract} (${r.gap > 0 ? '+' : ''}${r.gap})`).join('\n')}>{s.rolls.length}</td>
                <td>{s.synthetic ? <span className="badge synthetic">sample data</span> : s.source}</td>
                <td><button className="btn small danger" disabled={!!session?.symbols.includes(s.symbol)} onClick={() => remove(s)}>Delete</button></td>
              </tr>
            ))}
            {!symbols.length && <tr><td colSpan={9} className="empty">No data yet. Generate sample data or import a file below.</td></tr>}
          </tbody>
        </table>
      </div>
      <div className="cols">
        <BarsPanel />
        <ImportPanel contracts={contracts.map((c) => c.symbol)} />
        <DatabentoPanel contracts={contracts.filter((c) => !c.perpetual).map((c) => c.symbol)} />
        <BinancePanel contracts={contracts.filter((c) => c.perpetual).map((c) => c.symbol)} />
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

function BinancePanel({ contracts }: { contracts: string[] }) {
  const loadSymbols = useStore((s) => s.loadSymbols)
  const toast = useStore((s) => s.toast)
  const [symbol, setSymbol] = useState(contracts[0] ?? 'BTCUSDT')
  const [days, setDays] = useState(30)
  const [busy, setBusy] = useState(false)
  const go = async () => {
    setBusy(true)
    try { const r = await api.post<SymbolInfo>('/api/data/binance', { symbol, days }); await loadSymbols(); toast('info', `Downloaded ${r.rows.toLocaleString()} bars for ${r.symbol}`) }
    catch (e) { toast('error', (e as Error).message) } finally { setBusy(false) }
  }
  return (
    <section className="panel">
      <h2>Download crypto perpetuals</h2>
      <p className="help">Free 1-minute history from Binance USD-M futures. Perpetuals trade around the clock and are also available for live paper trading.</p>
      <div className="row2">
        <label className="field"><span>Instrument</span>
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>{contracts.map((c) => <option key={c}>{c}</option>)}</select></label>
        <label className="field"><span>Days of history</span><input type="number" min={1} max={730} value={days} onChange={(e) => setDays(+e.target.value)} /></label>
      </div>
      <button className="btn primary" disabled={busy} onClick={go}>{busy ? 'Downloading...' : 'Download'}</button>
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
