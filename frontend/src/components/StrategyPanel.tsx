import { useEffect, useState } from 'react'
import { api } from '../api'
import { fmtTime } from '../format'
import { useStore } from '../store'

export interface ParamSpec { name: string; default: unknown; min: number | null; max: number | null; step: number | null; choices: unknown[] | null; label: string; help: string | null; type: string }
export interface StrategyInfo { id: string; file: string; class?: string; name: string; doc?: string; timeframe?: string; symbol?: string | null; params?: ParamSpec[]; error?: string }
interface Runner { id: string; name: string; ref: string; symbol: string; timeframe: string; params: Record<string, unknown>; status: string; error: string; bars: number; open_orders: number; logs: [number, string][] }

const TFS = ['1m', '2m', '3m', '5m', '10m', '15m', '30m', '1h', '4h']

export function ParamInput({ spec, value, onChange }: { spec: ParamSpec; value: unknown; onChange: (v: unknown) => void }) {
  if (spec.type === 'bool') return <select value={String(value)} onChange={(e) => onChange(e.target.value === 'true')}><option value="true">Yes</option><option value="false">No</option></select>
  if (spec.choices) return <select value={String(value)} onChange={(e) => onChange(e.target.value)}>{spec.choices.map((c) => <option key={String(c)}>{String(c)}</option>)}</select>
  if (spec.type === 'int' || spec.type === 'float') {
    return <input type="number" value={value as number} min={spec.min ?? undefined} max={spec.max ?? undefined}
      step={spec.step ?? (spec.type === 'int' ? 1 : 0.1)} onChange={(e) => onChange(spec.type === 'int' ? Math.round(+e.target.value) : +e.target.value)} />
  }
  return <input type="text" value={String(value ?? '')} onChange={(e) => onChange(e.target.value)} />
}

/** Strategies running inside the current replay or live session. */
export default function StrategyPanel() {
  const session = useStore((s) => s.session)
  const toast = useStore((s) => s.toast)
  const contracts = useStore((s) => s.contracts)
  const [runners, setRunners] = useState<Runner[]>([])
  const [strategies, setStrategies] = useState<StrategyInfo[]>([])
  const [sid, setSid] = useState('')
  const [symbol, setSymbol] = useState('')
  const [tf, setTf] = useState('')
  const [params, setParams] = useState<Record<string, unknown>>({})
  const [adding, setAdding] = useState(false)

  useEffect(() => { api.get<StrategyInfo[]>('/api/strategies').then((s) => setStrategies(s.filter((x) => !x.error))).catch(() => {}) }, [])
  useEffect(() => {
    let stop = false
    const tick = () => api.get<Runner[]>('/api/session/strategies').then((r) => { if (!stop) setRunners(r) }).catch(() => {})
    tick()
    const iv = setInterval(tick, 1500)
    return () => { stop = true; clearInterval(iv) }
  }, [session?.id])

  const strat = strategies.find((s) => s.id === sid) ?? strategies[0]
  useEffect(() => {
    if (!strat) return
    setParams(Object.fromEntries((strat.params ?? []).map((p) => [p.name, p.default])))
    setTf(strat.timeframe ?? '5m')
    const syms = session?.symbols ?? []
    setSymbol(strat.symbol && syms.includes(strat.symbol) ? strat.symbol : syms[0] ?? '')
  }, [strat?.id, session?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!session) return <div className="empty">Start a replay or live session, then run a strategy inside it to watch it trade.</div>

  const attach = async () => {
    if (!strat) return
    try {
      await api.post('/api/session/strategies', { strategy: strat.id, symbol, timeframe: tf, params })
      setAdding(false)
      toast('info', `${strat.name} is running on ${symbol} ${tf}`)
    } catch (e) { toast('error', (e as Error).message) }
  }
  const stop = (r: Runner, flatten: boolean) =>
    api.del(`/api/session/strategies/${r.id}?flatten=${flatten}`).catch((e) => toast('error', e.message))
  const tz = (sym: string) => contracts.find((c) => c.symbol === sym)?.session.tz ?? 'UTC'

  return (
    <div style={{ padding: '6px 10px 12px' }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8 }}>
        <span className="muted">Strategies trade this session's account with the same code as the backtester. Their orders are tagged and appear on the chart.</span>
        <span style={{ flex: 1 }} />
        <button className="btn small primary" onClick={() => setAdding(!adding)}>{adding ? 'Close' : 'Run a strategy'}</button>
      </div>
      {adding && strat && (
        <div className="panel" style={{ marginBottom: 10 }}>
          <div className="row3">
            <label className="field"><span>Strategy</span>
              <select value={strat.id} onChange={(e) => setSid(e.target.value)}>{strategies.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
            <label className="field"><span>Instrument</span>
              <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>{session.symbols.map((s) => <option key={s}>{s}</option>)}</select></label>
            <label className="field"><span>Bar size</span>
              <select value={tf} onChange={(e) => setTf(e.target.value)}>{TFS.map((t) => <option key={t}>{t}</option>)}</select></label>
          </div>
          <div className="params-grid">
            {(strat.params ?? []).map((p) => (
              <label key={p.name} className="field" title={p.help ?? undefined}><span>{p.label}</span>
                <ParamInput spec={p} value={params[p.name]} onChange={(v) => setParams({ ...params, [p.name]: v })} /></label>
            ))}
          </div>
          <button className="btn primary" onClick={attach}>Start {strat.name}</button>
        </div>
      )}
      {!runners.length && !adding && <div className="empty">No strategy running.</div>}
      {runners.length > 0 && (
        <table className="grid">
          <thead><tr><th>Strategy</th><th>Market</th><th>Status</th><th className="num">Bars seen</th><th className="num">Working orders</th><th>Last log</th><th /></tr></thead>
          <tbody>
            {runners.map((r) => (
              <tr key={r.id}>
                <td>{r.name} <span className="faint">{Object.entries(r.params).slice(0, 3).map(([k, v]) => `${k}=${v}`).join(' ')}</span></td>
                <td>{r.symbol} {r.timeframe}</td>
                <td className={r.status === 'error' ? 'down' : r.status === 'running' ? 'up' : ''}>{r.status}{r.error ? `: ${r.error}` : ''}</td>
                <td className="num">{r.bars}</td>
                <td className="num">{r.open_orders}</td>
                <td className="muted" style={{ maxWidth: 360, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {r.logs.length ? `${fmtTime(r.logs[r.logs.length - 1][0], tz(r.symbol), false)} ${r.logs[r.logs.length - 1][1]}` : ''}</td>
                <td style={{ whiteSpace: 'nowrap' }}>
                  <button className="btn small" onClick={() => stop(r, false)}>Stop</button>{' '}
                  <button className="btn small danger" onClick={() => stop(r, true)} title="Cancel its orders and close its position">Stop and flatten</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
