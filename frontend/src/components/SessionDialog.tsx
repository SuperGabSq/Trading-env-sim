import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { groupByClass, tzLabel, utcToWall, wallToUtc } from '../format'
import { useStore } from '../store'

interface LiveSource { id: string; label: string; symbols: string[]; available: boolean; note: string }

function getTab(): 'replay' | 'live' {
  try {
    const t = sessionStorage.getItem('tsim:dialogTab')
    sessionStorage.removeItem('tsim:dialogTab')
    return t === 'live' ? 'live' : 'replay'
  } catch { return 'replay' }
}

export default function SessionDialog() {
  const show = useStore((s) => s.showSessionDialog)
  const set = useStore((s) => s.set)
  if (!show) return null
  return <Dialog onClose={() => set({ showSessionDialog: false })} />
}

function Dialog({ onClose }: { onClose: () => void }) {
  const symbols = useStore((s) => s.symbols)
  const contracts = useStore((s) => s.contracts)
  const current = useStore((s) => s.symbol)
  const toast = useStore((s) => s.toast)
  const [tab, setTab] = useState<'replay' | 'live'>(getTab)
  const minute = symbols.filter((s) => s.base_tf === '1m')
  const [picked, setPicked] = useState<string[]>(() => (minute.find((s) => s.symbol === current) ? [current] : minute.slice(0, 1).map((s) => s.symbol)))
  const first = minute.find((s) => s.symbol === picked[0])
  const tz = first?.contract?.session.tz ?? 'America/Chicago'
  const defaultStart = useMemo(() => {
    if (!first?.last_ts) return ''
    let t = first.last_ts - 5 * 86400
    const day = new Date(t * 1000).getUTCDay()
    if (day === 0) t -= 2 * 86400
    if (day === 6) t -= 86400
    return utcToWall(t, tz).slice(0, 10) + (tz === 'UTC' ? 'T00:00' : 'T08:00')
  }, [first?.last_ts, tz])
  const [start, setStart] = useState(defaultStart)
  useEffect(() => { if (!start && defaultStart) setStart(defaultStart) }, [defaultStart, start])
  const [balance, setBalance] = useState(50000)
  const [speed, setSpeed] = useState(60)
  const [slip, setSlip] = useState(0)
  const [spread, setSpread] = useState(true)
  const [volPct, setVolPct] = useState('')
  const [latency, setLatency] = useState(0)
  const [through, setThrough] = useState(1)
  const [intrabar, setIntrabar] = useState('tv')
  const [rollFees, setRollFees] = useState(true)
  const [lossLimit, setLossLimit] = useState<string>('')
  const [maxPos, setMaxPos] = useState<string>('')
  const [margin, setMargin] = useState(true)
  const [busy, setBusy] = useState(false)
  const [sources, setSources] = useState<LiveSource[]>([])
  const [source, setSource] = useState('')
  const [liveSymbols, setLiveSymbols] = useState<string[]>([])
  const [simStart, setSimStart] = useState('')
  const [simSpeed, setSimSpeed] = useState(1)

  useEffect(() => {
    api.get<LiveSource[]>('/api/live/sources').then((s) => {
      setSources(s)
      const firstAvail = s.find((x) => x.available) ?? s[0]
      if (firstAvail) { setSource(firstAvail.id); setLiveSymbols(firstAvail.symbols.slice(0, 1)) }
    }).catch(() => setSources([]))
  }, [])

  const fill = { slippage_ticks: slip, limit_through_ticks: through, intrabar, roll_fees: rollFees, spread_model: spread,
    max_volume_pct: volPct ? +volPct : null, latency_ms: latency }
  const risk = {
    daily_loss_limit: lossLimit ? +lossLimit : null, max_position: maxPos ? +maxPos : null,
    check_margin: margin, auto_liquidate: margin,
  }

  const startReplay = async () => {
    if (!picked.length || !start) return
    setBusy(true)
    try {
      await api.post('/api/session/replay', { symbols: picked, start: wallToUtc(start, tz), balance, speed, fill, risk })
      if (!picked.includes(current)) useStore.getState().setPrefs({ symbol: picked[0] })
      onClose()
    } catch (e) {
      toast('error', (e as Error).message)
    } finally { setBusy(false) }
  }

  const startLive = async () => {
    if (!source || !liveSymbols.length) return
    setBusy(true)
    try {
      const extra = source === 'simulated' ? { start: simStart ? wallToUtc(simStart, tz) : null, speed: simSpeed } : {}
      await api.post('/api/session/live', { source, symbols: liveSymbols, balance, fill, risk, ...extra })
      if (!liveSymbols.includes(current)) useStore.getState().setPrefs({ symbol: liveSymbols[0] })
      onClose()
    } catch (e) {
      toast('error', (e as Error).message)
    } finally { setBusy(false) }
  }

  const src = sources.find((s) => s.id === source)

  return (
    <div className="overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="sess-title">
        <header>
          <h2 id="sess-title">New trading session</h2>
          <button className="icon-btn" onClick={onClose} aria-label="Close">✕</button>
        </header>
        <div className="body">
          <div className="seg" role="tablist" style={{ marginBottom: 12 }}>
            <button role="tab" aria-pressed={tab === 'replay'} onClick={() => setTab('replay')}>Bar replay</button>
            <button role="tab" aria-pressed={tab === 'live'} onClick={() => setTab('live')}>Live paper trading</button>
          </div>

          {tab === 'replay' ? (
            <>
              <p className="help">Replay history bar by bar and trade it as if it were live. Charts never show data past the replay clock.</p>
              <fieldset>
                <legend>Instruments</legend>
                {groupByClass(minute, (s) => s.contract?.asset_class).map((g) => (
                  <div key={g.id} className="check-group">
                    <div className="group-label">{g.label}</div>
                    {g.items.map((s) => (
                      <label key={s.symbol} className="check">
                        <input type="checkbox" checked={picked.includes(s.symbol)}
                          onChange={(e) => setPicked(e.target.checked ? [...picked, s.symbol] : picked.filter((x) => x !== s.symbol))} />
                        {s.symbol} <span className="muted">{s.contract?.name}</span> {s.synthetic && <span className="badge synthetic">sample</span>}
                      </label>
                    ))}
                  </div>
                ))}
                {!minute.length && <p className="help">No 1-minute data available. Import or generate data first.</p>}
              </fieldset>
              <div className="row2">
                <label className="field"><span>Start ({tzLabel(tz)})</span>
                  <input type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} /></label>
                <label className="field"><span>Speed</span>
                  <select value={speed} onChange={(e) => setSpeed(+e.target.value)}>
                    {[1, 5, 10, 30, 60, 120, 300, 600].map((v) => <option key={v} value={v}>{v}x</option>)}
                    <option value={0}>Max</option>
                  </select></label>
              </div>
            </>
          ) : (
            <>
              <p className="help">Paper trade against a real-time feed. Orders fill with the same engine as replays and backtests.</p>
              <label className="field"><span>Data source</span>
                <select value={source} onChange={(e) => {
                  setSource(e.target.value)
                  const s = sources.find((x) => x.id === e.target.value)
                  setLiveSymbols(s ? s.symbols.slice(0, 1) : [])
                }}>
                  {sources.map((s) => <option key={s.id} value={s.id}>{s.label}{s.available ? '' : ' (not available)'}</option>)}
                </select></label>
              {src && <p className="help">{src.note}</p>}
              {source === 'simulated' && (
                <div className="row2">
                  <label className="field"><span>Start ({tzLabel(tz)}, optional)</span>
                    <input type="datetime-local" value={simStart} onChange={(e) => setSimStart(e.target.value)} /></label>
                  <label className="field"><span>Speed</span>
                    <select value={simSpeed} onChange={(e) => setSimSpeed(+e.target.value)}>
                      <option value={1}>Real time</option><option value={2}>2x</option><option value={5}>5x</option><option value={10}>10x</option>
                    </select></label>
                </div>
              )}
              {src && (
                <fieldset>
                  <legend>Instruments</legend>
                  {groupByClass(src.symbols, (s) => contracts.find((c) => c.symbol === s)?.asset_class).map((g) => (
                    <div key={g.id} className="check-group">
                      {src.symbols.length > 6 && <div className="group-label">{g.label}</div>}
                      <div className="check-row">
                        {g.items.map((s) => (
                          <label key={s} className="check">
                            <input type="checkbox" checked={liveSymbols.includes(s)}
                              onChange={(e) => setLiveSymbols(e.target.checked ? [...liveSymbols, s] : liveSymbols.filter((x) => x !== s))} />
                            {s}
                          </label>
                        ))}
                      </div>
                    </div>
                  ))}
                </fieldset>
              )}
            </>
          )}

          <fieldset>
            <legend>Account and risk</legend>
            <div className="row3">
              <label className="field"><span>Starting balance ($)</span>
                <input type="number" min={100} step={1000} value={balance} onChange={(e) => setBalance(+e.target.value)} /></label>
              <label className="field"><span>Daily loss limit ($)</span>
                <input type="number" min={0} placeholder="none" value={lossLimit} onChange={(e) => setLossLimit(e.target.value)} /></label>
              <label className="field"><span>Max contracts</span>
                <input type="number" min={1} placeholder="none" value={maxPos} onChange={(e) => setMaxPos(e.target.value)} /></label>
            </div>
            <label className="check"><input type="checkbox" checked={margin} onChange={(e) => setMargin(e.target.checked)} />
              Enforce margin (reject orders, liquidate on margin calls and at the overnight switch)</label>
          </fieldset>

          <fieldset>
            <legend>Fill model</legend>
            <label className="check"><input type="checkbox" checked={spread} onChange={(e) => setSpread(e.target.checked)} />
              Cross the bid-ask spread on market and stop orders (real quotes when the feed has them, otherwise the contract's typical spread, wider overnight and at the open)</label>
            <div className="row3">
              <label className="field"><span>Extra slippage (ticks per side)</span>
                <input type="number" min={0} step={0.5} value={slip} onChange={(e) => setSlip(+e.target.value)} /></label>
              <label className="field"><span>Limit fills need (ticks through)</span>
                <input type="number" min={0} step={1} value={through} onChange={(e) => setThrough(+e.target.value)} /></label>
              <label className="field"><span>Inside a bar</span>
                <select value={intrabar} onChange={(e) => setIntrabar(e.target.value)}>
                  <option value="tv">TradingView path</option>
                  <option value="worst">Worst case for the position</option>
                </select></label>
            </div>
            <div className="row2">
              <label className="field"><span>Max share of traded volume (%)</span>
                <input type="number" min={1} max={100} placeholder="no limit" value={volPct} onChange={(e) => setVolPct(e.target.value)} /></label>
              <label className="field"><span>Order latency (ms)</span>
                <input type="number" min={0} step={50} value={latency} onChange={(e) => setLatency(+e.target.value)} /></label>
            </div>
            <label className="check"><input type="checkbox" checked={rollFees} onChange={(e) => setRollFees(e.target.checked)} />
              Charge fees when a position is rolled to the next contract</label>
          </fieldset>
        </div>
        <footer>
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          {tab === 'replay'
            ? <button className="btn primary" disabled={busy || !picked.length || !start} onClick={startReplay}>Start replay</button>
            : <button className="btn primary" disabled={busy || !src?.available || !liveSymbols.length} onClick={startLive}>Go live</button>}
        </footer>
      </div>
    </div>
  )
}
