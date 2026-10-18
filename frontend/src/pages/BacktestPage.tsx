import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AreaSeries, BaselineSeries, CandlestickSeries, createChart, createSeriesMarkers, LineStyle,
  type IChartApi, type SeriesMarker, type Time, type UTCTimestamp,
} from 'lightweight-charts'
import { api } from '../api'
import { fmtMoney, fmtNum, fmtPrice, fmtTime, groupByClass, pnlClass, toChartTime, tradeTicks, tzLabel } from '../format'
import { useStore } from '../store'
import type { BarsColumns, Fill, Trade } from '../types'
import { ParamInput, type StrategyInfo } from '../components/StrategyPanel'

type Summary = Record<string, number | null>
interface Breakdown { by_hour: Row[]; by_weekday: Row[]; by_month: Row[]; long: Summary; short: Summary; exit_reasons: { key: string; trades: number }[] }
interface Row { key: string; trades?: number; net: number | null; win_rate?: number | null }
interface Result {
  strategy: string; config: Record<string, any>; summary: Summary; breakdown: Breakdown; equity: [number, number][]
  trades: Trade[]; fills: Fill[]; days: { date: string; pnl: number; equity: number }[]; logs: [number, string][]
  duration_s: number; bars_processed: number; synthetic_data: boolean; data_source: string; error?: string
  kind?: string; rows?: { params: Record<string, unknown>; summary?: Summary; error?: string }[]; metric?: string
}
interface RunRecord {
  id: string; created: number; strategy: string; symbol: string; timeframe: string; kind: string; status: string
  progress: number; summary: Summary | null; params: Record<string, unknown>; start?: string; end?: string
  combinations?: number; result?: Result; request?: any
}

const TFS = ['1m', '2m', '3m', '5m', '10m', '15m', '30m', '1h', '2h', '4h', '1D']
const css = (n: string) => getComputedStyle(document.documentElement).getPropertyValue(n).trim()

export default function BacktestPage() {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([])
  const [runs, setRuns] = useState<RunRecord[]>([])
  const [openId, setOpenId] = useState<string | null>(null)
  const [open, setOpen] = useState<RunRecord | null>(null)
  const toast = useStore((s) => s.toast)

  const loadRuns = useCallback(() => api.get<RunRecord[]>('/api/backtests').then(setRuns).catch(() => {}), [])
  useEffect(() => {
    api.get<StrategyInfo[]>('/api/strategies').then(setStrategies).catch((e) => toast('error', e.message))
    loadRuns()
  }, [loadRuns, toast])

  // poll the open run until it is finished
  useEffect(() => {
    if (!openId) { setOpen(null); return }
    let stop = false
    const tick = async () => {
      try {
        const r = await api.get<RunRecord>(`/api/backtests/${openId}`)
        if (stop) return
        setOpen(r)
        if (r.status === 'queued' || r.status === 'running') setTimeout(tick, 500)
        else loadRuns()
      } catch (e) { toast('error', (e as Error).message) }
    }
    tick()
    return () => { stop = true }
  }, [openId, loadRuns, toast])

  return (
    <div className="page">
      <h1>Backtest</h1>
      <p className="lead">Run a strategy from the <code>strategies</code> folder over history with the same engine used for replay and live trading. Edit a strategy file and run again: it is reloaded every time.</p>
      <div className="bt-layout">
        <StrategyForm strategies={strategies} onStarted={(id) => { setOpenId(id); loadRuns() }} />
        <div className="bt-main">
          {open ? <RunView run={open} onOpen={(id) => setOpenId(id)} /> : <div className="panel empty">Run a backtest or pick one from the history below.</div>}
          <History runs={runs} openId={openId} onOpen={setOpenId} onChanged={loadRuns} />
        </div>
      </div>
    </div>
  )
}

// ------------------------------------------------------------------------------------------- form

function parseValues(s: string, type: string): unknown[] {
  const t = s.trim()
  if (!t) return []
  const m = t.match(/^(-?[\d.]+)\s*(?:\.\.|:|-)\s*(-?[\d.]+)(?:\s*(?:step|:|by)\s*(-?[\d.]+))?$/)
  if (m && type !== 'str') {
    const a = +m[1], b = +m[2], st = m[3] ? +m[3] : 1
    const out: number[] = []
    for (let v = a; v <= b + 1e-9 && out.length < 500; v += st) out.push(+v.toFixed(6))
    return type === 'int' ? out.map(Math.round) : out
  }
  return t.split(',').map((x) => x.trim()).filter(Boolean).map((x) =>
    type === 'int' ? Math.round(+x) : type === 'float' ? +x : type === 'bool' ? ['true', '1', 'yes'].includes(x.toLowerCase()) : x)
}

function StrategyForm({ strategies, onStarted }: { strategies: StrategyInfo[]; onStarted: (id: string) => void }) {
  const symbols = useStore((s) => s.symbols)
  const toast = useStore((s) => s.toast)
  const valid = strategies.filter((s) => !s.error)
  const [sid, setSid] = useState('')
  const strat = valid.find((s) => s.id === sid) ?? valid[0]
  const [symbol, setSymbol] = useState('')
  const [tf, setTf] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [balance, setBalance] = useState(50000)
  const [params, setParams] = useState<Record<string, unknown>>({})
  const [sweep, setSweep] = useState(false)
  const [sweepVals, setSweepVals] = useState<Record<string, string>>({})
  const [metric, setMetric] = useState('net_pnl')
  const [slip, setSlip] = useState(0)
  const [spread, setSpread] = useState(true)
  const [volPct, setVolPct] = useState('')
  const [latency, setLatency] = useState(0)
  const [through, setThrough] = useState(1)
  const [intrabar, setIntrabar] = useState('tv')
  const [res, setRes] = useState('1m')
  const [lossLimit, setLossLimit] = useState('')
  const [busy, setBusy] = useState(false)
  const [showDoc, setShowDoc] = useState(false)
  const [source, setSource] = useState<string | null>(null)

  useEffect(() => {
    if (!strat) return
    setParams(Object.fromEntries((strat.params ?? []).map((p) => [p.name, p.default])))
    setTf(strat.timeframe ?? '5m')
    const sym = strat.symbol && symbols.find((s) => s.symbol === strat.symbol) ? strat.symbol : symbols[0]?.symbol ?? 'MES'
    setSymbol(sym)
    setSweepVals({})
    setSource(null)
  }, [strat?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const symInfo = symbols.find((s) => s.symbol === symbol)
  const combos = useMemo(() => {
    if (!sweep || !strat) return 0
    let n = 1
    let any = false
    for (const p of strat.params ?? []) {
      const v = parseValues(sweepVals[p.name] ?? '', p.type)
      if (v.length) { n *= v.length; any = true }
    }
    return any ? n : 0
  }, [sweep, sweepVals, strat])

  const body = () => ({
    strategy: strat!.id, symbol, timeframe: tf, start: start || null, end: end || null, balance, params,
    slippage_ticks: slip, limit_through_ticks: through, intrabar, fill_resolution: res, spread_model: spread,
    max_volume_pct: volPct ? +volPct : null, latency_ms: latency,
    daily_loss_limit: lossLimit ? +lossLimit : null,
  })

  const run = async () => {
    if (!strat) return
    setBusy(true)
    try {
      if (sweep) {
        const g: Record<string, unknown[]> = {}
        for (const p of strat.params ?? []) {
          const v = parseValues(sweepVals[p.name] ?? '', p.type)
          if (v.length) g[p.name] = v
        }
        const r = await api.post<{ id: string }>('/api/backtests/sweep', { base: body(), grid: g, metric })
        onStarted(r.id)
      } else {
        const r = await api.post<{ id: string }>('/api/backtests', body())
        onStarted(r.id)
      }
    } catch (e) { toast('error', (e as Error).message) } finally { setBusy(false) }
  }

  const broken = strategies.filter((s) => s.error)
  return (
    <section className="panel bt-form">
      <h2>Strategy</h2>
      {broken.map((b) => <p key={b.id} className="help down">{b.name}: {b.error}</p>)}
      <label className="field"><span>Strategy</span>
        <select value={strat?.id ?? ''} onChange={(e) => setSid(e.target.value)}>
          {valid.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select></label>
      {strat?.doc && (
        <div className="help">
          {showDoc ? strat.doc : strat.doc.split('\n')[0]}{' '}
          <button className="btn small ghost" onClick={() => setShowDoc(!showDoc)}>{showDoc ? 'Less' : 'More'}</button>
          <button className="btn small ghost" onClick={() => api.get<{ source: string }>(`/api/strategies/source?id=${encodeURIComponent(strat.id)}`).then((r) => setSource(source ? null : r.source))}>{source ? 'Hide code' : 'View code'}</button>
        </div>
      )}
      {source && <pre className="code" style={{ maxHeight: 320 }}>{source}</pre>}
      <div className="row2">
        <label className="field"><span>Instrument</span>
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
            {groupByClass(symbols, (s) => s.contract?.asset_class).map((g) => (
              <optgroup key={g.id} label={g.label}>
                {g.items.map((s) => <option key={s.symbol} value={s.symbol}>{s.symbol}{s.synthetic ? ' (sample)' : ''}{s.base_tf !== '1m' ? ` (${s.base_tf})` : ''}</option>)}
              </optgroup>))}
          </select></label>
        <label className="field"><span>Bar size</span>
          <select value={tf} onChange={(e) => setTf(e.target.value)}>{TFS.map((t) => <option key={t}>{t}</option>)}</select></label>
      </div>
      <div className="row2">
        <label className="field"><span>From (trade date)</span><input type="date" value={start} onChange={(e) => setStart(e.target.value)} /></label>
        <label className="field"><span>To (inclusive)</span><input type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></label>
      </div>
      {symInfo && <p className="help">Data: {fmtTime(symInfo.first_ts, 'UTC').slice(0, 10)} to {fmtTime(symInfo.last_ts, 'UTC').slice(0, 10)}. Leave dates empty to use all of it.</p>}
      <label className="field"><span>Starting balance ($)</span><input type="number" step={1000} value={balance} onChange={(e) => setBalance(+e.target.value)} /></label>

      <div className="seg" style={{ width: '100%', margin: '6px 0 10px' }}>
        <button style={{ flex: 1 }} aria-pressed={!sweep} onClick={() => setSweep(false)}>Single run</button>
        <button style={{ flex: 1 }} aria-pressed={sweep} onClick={() => setSweep(true)}>Parameter sweep</button>
      </div>
      <fieldset className="params">
        <legend>Parameters</legend>
        {sweep && <p className="help">Enter values to test, like <code>4,6,8</code> or <code>4..12 step 2</code>. Empty fields keep the value on the left.</p>}
        {(strat?.params ?? []).map((p) => (
          <div key={p.name} className={sweep ? 'param-row sweep' : 'param-row'}>
            <label className="field" title={p.help ?? undefined}><span>{p.label}</span>
              <ParamInput spec={p} value={params[p.name]} onChange={(v) => setParams({ ...params, [p.name]: v })} /></label>
            {sweep && (p.type === 'int' || p.type === 'float' || p.type === 'bool' || p.type === 'choice') && (
              <label className="field"><span>Test values</span>
                <input type="text" value={sweepVals[p.name] ?? ''} placeholder={p.type === 'bool' ? 'true,false' : ''}
                  onChange={(e) => setSweepVals({ ...sweepVals, [p.name]: e.target.value })} /></label>
            )}
          </div>
        ))}
      </fieldset>
      {sweep && (
        <label className="field"><span>Rank results by</span>
          <select value={metric} onChange={(e) => setMetric(e.target.value)}>
            <option value="net_pnl">Net P&L</option><option value="profit_factor">Profit factor</option>
            <option value="sharpe">Sharpe</option><option value="recovery_factor">Recovery factor</option>
            <option value="expectancy_ticks">Expectancy (ticks)</option><option value="win_rate">Win rate</option>
          </select></label>
      )}
      <details>
        <summary className="muted" style={{ cursor: 'pointer', margin: '4px 0 10px' }}>Fill model and risk</summary>
        <label className="check"><input type="checkbox" checked={spread} onChange={(e) => setSpread(e.target.checked)} />Cross the bid-ask spread</label>
        <div className="row2">
          <label className="field"><span>Extra slippage (ticks/side)</span><input type="number" min={0} step={0.5} value={slip} onChange={(e) => setSlip(+e.target.value)} /></label>
          <label className="field"><span>Limit fills need (ticks through)</span><input type="number" min={0} value={through} onChange={(e) => setThrough(+e.target.value)} /></label>
        </div>
        <div className="row2">
          <label className="field"><span>Inside a bar</span>
            <select value={intrabar} onChange={(e) => setIntrabar(e.target.value)}><option value="tv">TradingView path</option><option value="worst">Worst case</option></select></label>
          <label className="field"><span>Simulate fills on</span>
            <select value={res} onChange={(e) => setRes(e.target.value)}><option value="1m">1-minute bars (more precise)</option><option value="chart">Strategy bars (like TradingView)</option></select></label>
        </div>
        <div className="row2">
          <label className="field"><span>Max % of bar volume</span><input type="number" min={1} max={100} placeholder="no limit" value={volPct} onChange={(e) => setVolPct(e.target.value)} /></label>
          <label className="field"><span>Latency (ms)</span><input type="number" min={0} step={50} value={latency} onChange={(e) => setLatency(+e.target.value)} /></label>
        </div>
        <label className="field"><span>Daily loss limit ($, optional)</span><input type="number" min={0} value={lossLimit} onChange={(e) => setLossLimit(e.target.value)} /></label>
      </details>
      <button className="btn primary" style={{ width: '100%', padding: '8px' }} disabled={busy || !strat || (sweep && combos === 0)} onClick={run}>
        {sweep ? (combos ? `Run ${combos} backtests` : 'Enter values to test') : 'Run backtest'}
      </button>
    </section>
  )
}

// ------------------------------------------------------------------------------------------- views

function RunView({ run, onOpen }: { run: RunRecord; onOpen: (id: string) => void }) {
  if (run.status === 'queued' || run.status === 'running') {
    return (
      <section className="panel">
        <h2>{run.strategy.split(':').pop()} on {run.symbol}</h2>
        <p className="help">{run.status === 'queued' ? 'Waiting for the previous run to finish...' : `Running... ${Math.round(run.progress * 100)}%`}</p>
        <div className="progress"><div style={{ width: `${run.progress * 100}%` }} /></div>
        <button className="btn small" onClick={() => api.post(`/api/backtests/${run.id}/cancel`)}>Cancel</button>
      </section>
    )
  }
  const r = run.result
  if (run.status === 'error' || r?.error) {
    return <section className="panel"><h2>Backtest failed</h2><pre className="code">{r?.error}{'\n\n'}{(r as any)?.trace}</pre></section>
  }
  if (run.status === 'cancelled' || !r) return <section className="panel"><h2>Cancelled</h2></section>
  if (r.kind === 'sweep') return <SweepView run={run} result={r} onOpen={onOpen} />
  return <Report run={run} r={r} />
}

function Report({ run, r }: { run: RunRecord; r: Result }) {
  const contracts = useStore((s) => s.contracts)
  const c = contracts.find((x) => x.symbol === run.symbol)
  const tz = c?.session.tz ?? 'UTC'
  const s = r.summary
  const [sel, setSel] = useState<Trade | null>(null)
  const [limit, setLimit] = useState(200)
  const stat = (label: string, v: React.ReactNode, cls = '') => <div className="stat"><span>{label}</span><strong className={cls}>{v}</strong></div>
  const cfg = r.config
  return (
    <>
      <section className="panel">
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap', alignItems: 'baseline' }}>
          <h2>{r.strategy} <span className="muted" style={{ fontWeight: 400 }}>{run.symbol} {cfg.timeframe}</span></h2>
          <span className="muted" style={{ fontSize: 12 }}>
            {fmtTime(cfg.start_ts, tz).slice(0, 10)} to {fmtTime(cfg.end_ts, tz).slice(0, 10)}, {r.bars_processed.toLocaleString()} bars in {r.duration_s}s
          </span>
        </div>
        {r.synthetic_data && <p className="help amber">This ran on generated sample data. The numbers test the strategy code, not the market.</p>}
        <div className="stats">
          {stat('Net P&L', fmtMoney(s.net_pnl, true), pnlClass(s.net_pnl))}
          {stat('Return', s.return_pct !== null ? `${s.return_pct}%` : '-', pnlClass(s.return_pct))}
          {stat('Trades', s.trades)}
          {stat('Win rate', s.win_rate !== null ? `${s.win_rate}%` : '-')}
          {stat('Profit factor', fmtNum(s.profit_factor))}
          {stat('Expectancy', `${fmtMoney(s.expectancy, true)} (${fmtNum(s.expectancy_ticks, 1)} ticks)`, pnlClass(s.expectancy))}
          {stat('Max drawdown', `${fmtMoney(s.max_drawdown)} (${fmtNum(s.max_drawdown_pct, 1)}%)`)}
          {stat('Sharpe (daily)', fmtNum(s.sharpe))}
          {stat('Sortino', fmtNum(s.sortino))}
          {stat('Avg win / loss', `${fmtMoney(s.avg_win)} / ${fmtMoney(s.avg_loss)}`)}
          {stat('Fees', `${fmtMoney(s.fees)}${s.fees_pct_of_gross_profit !== null ? ` (${s.fees_pct_of_gross_profit}% of gross profit)` : ''}`)}
          {stat('Avg MAE / MFE', `${fmtNum(s.avg_mae_ticks, 1)} / ${fmtNum(s.avg_mfe_ticks, 1)} ticks`)}
          {stat('Largest win / loss', `${fmtMoney(s.largest_win)} / ${fmtMoney(s.largest_loss)}`)}
          {stat('Max losing streak', s.max_consecutive_losses)}
          {stat('Avg duration', s.avg_duration_min !== null ? `${s.avg_duration_min} min` : '-')}
          {stat('Time in market', s.exposure_pct !== null ? `${s.exposure_pct}%` : '-')}
        </div>
        <EquityChart equity={r.equity} balance={(cfg.balance as number) ?? 50000} tz={tz} />
        <p className="help" style={{ marginTop: 6 }}>Equity at every bar close; the lower pane is the distance below the running peak.
          Settings: {cfg.spread_model ? 'bid-ask spread modelled, ' : ''}extra slippage {cfg.slippage_ticks} tick(s), limit fills {cfg.limit_through_ticks} tick(s) through, {cfg.intrabar === 'tv' ? 'TradingView' : 'worst-case'} bar path, fills on {cfg.fill_resolution === '1m' ? '1-minute' : 'strategy'} bars.</p>
      </section>
      <div className="cols" style={{ margin: '16px 0' }}>
        <section className="panel"><h2>By entry hour</h2><p className="help">Net P&L of trades by the hour they were entered (exchange time).</p><Diverging rows={r.breakdown.by_hour} /></section>
        <section className="panel"><h2>By weekday</h2><Diverging rows={r.breakdown.by_weekday.map((x) => ({ ...x, key: x.key.slice(2) }))} />
          <h2 style={{ marginTop: 14 }}>By month</h2><Diverging rows={r.breakdown.by_month} /></section>
        <section className="panel"><h2>Long vs short</h2>
          <table className="grid"><thead><tr><th>Side</th><th className="num">Trades</th><th className="num">Net</th><th className="num">Win rate</th><th className="num">PF</th></tr></thead>
            <tbody>{(['long', 'short'] as const).map((k) => { const x = r.breakdown[k]; return (
              <tr key={k}><td>{k === 'long' ? 'Long' : 'Short'}</td><td className="num">{x.trades}</td><td className={`num ${pnlClass(x.net)}`}>{fmtMoney(x.net, true)}</td>
                <td className="num">{x.win_rate ?? '-'}{x.win_rate !== undefined && x.win_rate !== null ? '%' : ''}</td><td className="num">{fmtNum(x.profit_factor)}</td></tr>) })}</tbody></table>
          <h2 style={{ marginTop: 14 }}>How trades ended</h2>
          <table className="grid"><tbody>{r.breakdown.exit_reasons.map((x) => <tr key={x.key}><td>{x.key}</td><td className="num">{x.trades}</td></tr>)}</tbody></table>
        </section>
      </div>
      {sel && <TradeInspector run={run} r={r} trade={sel} onClose={() => setSel(null)} />}
      <section className="panel" style={{ padding: 0 }}>
        <div style={{ padding: '12px 16px 4px' }}><h2>Trades</h2><p className="help">Click a trade to see it on the chart.</p></div>
        <div className="table-wrap" style={{ maxHeight: 480 }}>
          <table className="grid">
            <thead><tr><th>#</th><th>Entry ({tzLabel(tz)})</th><th>Side</th><th className="num">Qty</th><th className="num">Entry</th><th className="num">Exit</th>
              <th className="num">Ticks</th><th className="num">Net</th><th className="num">MAE</th><th className="num">MFE</th><th className="num">Minutes</th><th>Exit</th></tr></thead>
            <tbody>
              {r.trades.slice(0, limit).map((t, i) => {
                const ticks = tradeTicks(t, c)
                return (
                  <tr key={t.id} onClick={() => setSel(t)} style={{ cursor: 'pointer', background: sel?.id === t.id ? 'var(--raised)' : undefined }}>
                    <td className="faint">{i + 1}</td><td>{fmtTime(t.entry_ts, tz)}</td>
                    <td className={t.side === 'long' ? 'up' : 'down'}>{t.side === 'long' ? 'Long' : 'Short'}</td>
                    <td className="num">{t.qty}</td><td className="num">{fmtPrice(t.entry_price, c?.price_decimals)}</td><td className="num">{fmtPrice(t.exit_price, c?.price_decimals)}</td>
                    <td className={`num ${pnlClass(ticks)}`}>{fmtNum(ticks, 1)}</td><td className={`num ${pnlClass(t.net)}`}>{fmtMoney(t.net, true)}</td>
                    <td className="num">{fmtNum(t.mae_ticks, 1)}</td><td className="num">{fmtNum(t.mfe_ticks, 1)}</td>
                    <td className="num">{t.exit_ts ? Math.round((t.exit_ts - t.entry_ts) / 60) : ''}</td><td className="muted">{t.exit_reason}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {r.trades.length > limit && <div style={{ padding: 10 }}><button className="btn small" onClick={() => setLimit(limit + 500)}>Show more ({r.trades.length - limit} left)</button></div>}
          {!r.trades.length && <div className="empty">No trades in this period.</div>}
        </div>
      </section>
      {r.logs.length > 0 && (
        <section className="panel" style={{ marginTop: 16 }}><h2>Strategy log</h2>
          <pre className="code" style={{ maxHeight: 260 }}>{r.logs.slice(-300).map(([t, m]) => `${fmtTime(t, tz)}  ${m}`).join('\n')}</pre></section>
      )}
    </>
  )
}

function Diverging({ rows }: { rows: Row[] }) {
  if (!rows.length) return <p className="help">No trades.</p>
  const max = Math.max(...rows.map((r) => Math.abs(r.net ?? 0)), 1)
  return (
    <div className="div-bars" role="table">
      {rows.map((r) => {
        const v = r.net ?? 0
        const w = (Math.abs(v) / max) * 50
        const tip = `${r.key}: ${fmtMoney(v, true)}${r.trades !== undefined ? `, ${r.trades} trades, ${r.win_rate}% winners` : ''}`
        return (
          <div className="div-row" role="row" key={r.key} title={tip}>
            <span className="k" role="cell">{r.key}</span>
            <span className="track" role="cell" aria-label={tip}>
              <span className="zero" />
              <span className={`bar ${v >= 0 ? 'pos' : 'neg'}`} style={v >= 0 ? { left: '50%', width: `${w}%` } : { right: '50%', width: `${w}%` }} />
            </span>
            <span className={`v ${pnlClass(v)}`} role="cell">{fmtMoney(v, true)}</span>
          </div>
        )
      })}
    </div>
  )
}

function EquityChart({ equity, balance, tz }: { equity: [number, number][]; balance: number; tz: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const [hover, setHover] = useState<{ t: number; eq: number; dd: number } | null>(null)
  useEffect(() => {
    if (!ref.current || !equity.length) return
    const chart: IChartApi = createChart(ref.current, {
      autoSize: true,
      layout: { background: { color: 'transparent' }, textColor: css('--muted'), fontFamily: css('--font'), fontSize: 11, attributionLogo: false,
        panes: { separatorColor: css('--line') } },
      grid: { vertLines: { visible: false }, horzLines: { color: css('--chart-grid') } },
      rightPriceScale: { borderVisible: false }, timeScale: { borderVisible: false, timeVisible: true },
      localization: { locale: 'en-US', priceFormatter: (p: number) => fmtMoney(p) },
      crosshair: { horzLine: { visible: false } },
    })
    const eq = chart.addSeries(BaselineSeries, {
      baseValue: { type: 'price', price: balance }, lineWidth: 2,
      topLineColor: css('--up'), topFillColor1: css('--up-soft'), topFillColor2: 'transparent',
      bottomLineColor: css('--down'), bottomFillColor1: 'transparent', bottomFillColor2: css('--down-soft'),
      priceLineVisible: false,
    })
    const dd = chart.addSeries(AreaSeries, { lineColor: css('--down'), topColor: css('--down-soft'), bottomColor: css('--down-soft'),
      lineWidth: 1, priceLineVisible: false, invertFilledArea: true } as never, 1)
    const pts = new Map<number, number>()
    let peak = -Infinity
    const eqData: { time: UTCTimestamp; value: number }[] = []
    const ddData: { time: UTCTimestamp; value: number }[] = []
    for (const [t, v] of equity) {
      const time = toChartTime(t, '1m', tz) as UTCTimestamp
      if (pts.has(time)) continue
      pts.set(time, v)
      peak = Math.max(peak, v)
      eqData.push({ time, value: v })
      ddData.push({ time, value: v - peak })
    }
    eq.setData(eqData)
    dd.setData(ddData)
    eq.createPriceLine({ price: balance, color: css('--faint'), lineStyle: LineStyle.Dotted, lineWidth: 1, axisLabelVisible: false, title: '' })
    chart.panes()[0]?.setStretchFactor(3)
    chart.panes()[1]?.setStretchFactor(1)
    chart.timeScale().fitContent()
    chart.subscribeCrosshairMove((p) => {
      if (!p.time) { setHover(null); return }
      const e = p.seriesData.get(eq) as { value: number } | undefined
      const d = p.seriesData.get(dd) as { value: number } | undefined
      if (e) setHover({ t: p.time as number, eq: e.value, dd: d?.value ?? 0 })
    })
    return () => chart.remove()
  }, [equity, balance, tz])
  const last = equity[equity.length - 1]
  return (
    <div style={{ position: 'relative' }}>
      <div className="eq-legend">
        <span className="muted">Equity</span> <b>{fmtMoney(hover ? hover.eq : last?.[1])}</b>
        {hover && <><span className="muted" style={{ marginLeft: 10 }}>Below peak</span> <b className="down">{fmtMoney(hover.dd)}</b>
          <span className="muted" style={{ marginLeft: 10 }}>{new Date(hover.t * 1000).toISOString().slice(0, 16).replace('T', ' ')}</span></>}
      </div>
      <div ref={ref} style={{ height: 320 }} role="img" aria-label="Equity curve and drawdown" />
    </div>
  )
}

function TradeInspector({ run, r, trade, onClose }: { run: RunRecord; r: Result; trade: Trade; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const contracts = useStore((s) => s.contracts)
  const c = contracts.find((x) => x.symbol === run.symbol)
  const tz = c?.session.tz ?? 'UTC'
  const tf = r.config.timeframe as string
  useEffect(() => {
    if (!ref.current) return
    const chart = createChart(ref.current, {
      autoSize: true,
      layout: { background: { color: 'transparent' }, textColor: css('--muted'), fontFamily: css('--font'), fontSize: 11, attributionLogo: false },
      grid: { vertLines: { color: css('--chart-grid') }, horzLines: { color: css('--chart-grid') } },
      rightPriceScale: { borderVisible: false }, timeScale: { borderVisible: false, timeVisible: true },
      localization: { locale: 'en-US' },
    })
    const s = chart.addSeries(CandlestickSeries, { upColor: css('--up'), downColor: css('--down'), borderVisible: false, wickUpColor: css('--up'), wickDownColor: css('--down'),
      priceFormat: { type: 'price', precision: c?.price_decimals ?? 2, minMove: c?.tick_size ?? 0.25 } })
    const tfSec = (parseInt(tf) || 1) * (tf.endsWith('h') ? 3600 : tf.endsWith('D') ? 86400 : 60)
    const to = (trade.exit_ts ?? trade.entry_ts) + 30 * tfSec
    api.get<{ bars: BarsColumns }>(`/api/bars?symbol=${run.symbol}&tf=${tf}&to=${to}&limit=120`).then(({ bars }) => {
      const data = bars.time.map((t, i) => ({ time: toChartTime(t, tf, tz) as UTCTimestamp, open: bars.open[i], high: bars.high[i], low: bars.low[i], close: bars.close[i] }))
      s.setData(data)
      const inWin = r.fills.filter((f) => f.ts >= bars.time[0] && f.ts <= bars.time[bars.time.length - 1] + tfSec)
      const markers: SeriesMarker<Time>[] = []
      for (const f of inWin) {
        let k = 0
        while (k + 1 < bars.time.length && bars.time[k + 1] <= f.ts) k++
        const buy = f.side === 'buy'
        const mine = f.ts >= trade.entry_ts && f.ts <= (trade.exit_ts ?? trade.entry_ts)
        markers.push({ time: data[k].time, position: buy ? 'belowBar' : 'aboveBar', shape: buy ? 'arrowUp' : 'arrowDown',
          color: mine ? (buy ? css('--up') : css('--down')) : css('--faint'), size: mine ? 1.2 : 0.6,
          text: mine ? `${buy ? 'Buy' : 'Sell'} ${f.qty} @ ${fmtPrice(f.price, c?.price_decimals)}` : '' })
      }
      markers.sort((a, b) => (a.time as number) - (b.time as number))
      createSeriesMarkers(s, markers)
      s.createPriceLine({ price: trade.entry_price, color: css('--amber'), lineWidth: 1, lineStyle: LineStyle.Dashed, title: 'Entry', axisLabelVisible: true })
      if (trade.exit_price) s.createPriceLine({ price: trade.exit_price, color: css('--muted'), lineWidth: 1, lineStyle: LineStyle.Dashed, title: 'Exit', axisLabelVisible: true })
      chart.timeScale().fitContent()
    }).catch(() => {})
    return () => chart.remove()
  }, [trade.id]) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <section className="panel" style={{ marginBottom: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <h2>Trade on {fmtTime(trade.entry_ts, tz)}: <span className={pnlClass(trade.net)}>{fmtMoney(trade.net, true)}</span> <span className="muted" style={{ fontWeight: 400 }}>({trade.exit_reason})</span></h2>
        <button className="icon-btn" onClick={onClose} aria-label="Close trade chart">✕</button>
      </div>
      <div ref={ref} style={{ height: 340 }} />
    </section>
  )
}

function SweepView({ run, result, onOpen }: { run: RunRecord; result: Result; onOpen: (id: string) => void }) {
  const toast = useStore((s) => s.toast)
  const rows = result.rows ?? []
  const keys = Object.keys(result.rows?.[0]?.params ?? {})
  const openFull = async (params: Record<string, unknown>) => {
    try {
      const req = run.request?.request ?? {}
      const r = await api.post<{ id: string }>('/api/backtests', { ...req, params: { ...(req.params ?? {}), ...params } })
      onOpen(r.id)
    } catch (e) { toast('error', (e as Error).message) }
  }
  return (
    <section className="panel" style={{ padding: 0 }}>
      <div style={{ padding: '12px 16px 4px' }}>
        <h2>Parameter sweep: {result.strategy} <span className="muted" style={{ fontWeight: 400 }}>{run.symbol} {run.timeframe}, {rows.length} combinations</span></h2>
        <p className="help">Sorted by {result.metric?.replace(/_/g, ' ')}. The best in-sample result is usually optimistic: confirm it on a later period before trusting it.</p>
      </div>
      <div className="table-wrap" style={{ maxHeight: 560 }}>
        <table className="grid">
          <thead><tr>{keys.map((k) => <th key={k}>{k.replace(/_/g, ' ')}</th>)}<th className="num">Trades</th><th className="num">Net</th><th className="num">PF</th>
            <th className="num">Win %</th><th className="num">Max DD</th><th className="num">Sharpe</th><th /></tr></thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                {keys.map((k) => <td key={k}>{String(row.params[k])}</td>)}
                {row.summary ? <>
                  <td className="num">{row.summary.trades}</td><td className={`num ${pnlClass(row.summary.net_pnl)}`}>{fmtMoney(row.summary.net_pnl, true)}</td>
                  <td className="num">{fmtNum(row.summary.profit_factor)}</td><td className="num">{row.summary.win_rate ?? '-'}</td>
                  <td className="num">{fmtMoney(row.summary.max_drawdown)}</td><td className="num">{fmtNum(row.summary.sharpe)}</td>
                  <td><button className="btn small" onClick={() => openFull(row.params)}>Full report</button></td>
                </> : <td colSpan={7} className="down">{row.error}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function History({ runs, openId, onOpen, onChanged }: { runs: RunRecord[]; openId: string | null; onOpen: (id: string) => void; onChanged: () => void }) {
  if (!runs.length) return null
  return (
    <section className="panel" style={{ padding: 0, marginTop: 16 }}>
      <div style={{ padding: '12px 16px 4px' }}><h2>History</h2></div>
      <div className="table-wrap" style={{ maxHeight: 320 }}>
        <table className="grid">
          <thead><tr><th>When</th><th>Strategy</th><th>Market</th><th>Type</th><th className="num">Trades</th><th className="num">Net</th><th className="num">PF</th><th>Status</th><th /></tr></thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id} onClick={() => onOpen(r.id)} style={{ cursor: 'pointer', background: r.id === openId ? 'var(--raised)' : undefined }}>
                <td>{new Date(r.created * 1000).toLocaleString()}</td>
                <td>{r.strategy.split(':').pop()}</td>
                <td>{r.symbol} {r.timeframe}</td>
                <td className="muted">{r.kind === 'sweep' ? `Sweep (${r.combinations})` : 'Single'}</td>
                <td className="num">{r.summary?.trades ?? ''}</td>
                <td className={`num ${pnlClass(r.summary?.net_pnl)}`}>{r.summary ? fmtMoney(r.summary.net_pnl, true) : ''}</td>
                <td className="num">{r.summary ? fmtNum(r.summary.profit_factor) : ''}</td>
                <td>{r.status === 'running' ? `${Math.round(r.progress * 100)}%` : r.status}</td>
                <td><button className="btn small ghost" aria-label="Delete run" onClick={(e) => { e.stopPropagation(); api.del(`/api/backtests/${r.id}`).then(onChanged) }}>Delete</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
