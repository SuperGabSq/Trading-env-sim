import { useState } from 'react'
import { useStore, type DrawTool } from '../store'
import type { IndicatorSpec } from '../types'

const TFS = ['1m', '2m', '5m', '15m', '30m', '1h', '4h', '1D', '1W']
const COLORS = ['#e9b44c', '#6aa7e8', '#c78bf0', '#5fd0d6', '#f08b5a', '#9fb4c8']

export default function ChartToolbar() {
  const tf = useStore((s) => s.tf)
  const setPrefs = useStore((s) => s.setPrefs)
  const drawTool = useStore((s) => s.drawTool)
  const set = useStore((s) => s.set)
  const adjusted = useStore((s) => s.adjusted)
  const session = useStore((s) => s.session)
  const [open, setOpen] = useState(false)

  const tool = (t: DrawTool, label: string, title: string) => (
    <button aria-pressed={drawTool === t} title={title} onClick={() => set({ drawTool: drawTool === t ? 'none' : t })}>{label}</button>
  )

  return (
    <div className="toolbar">
      <div className="seg" role="group" aria-label="Timeframe">
        {TFS.map((t) => (
          <button key={t} aria-pressed={tf === t} onClick={() => setPrefs({ tf: t })}>{t}</button>
        ))}
      </div>
      <span className="tool-sep" />
      <div style={{ position: 'relative' }}>
        <button className="btn" onClick={() => setOpen(!open)} aria-expanded={open}>Indicators</button>
        {open && <IndicatorPopover onClose={() => setOpen(false)} />}
      </div>
      <span className="tool-sep" />
      <div className="seg" role="group" aria-label="Drawing tools">
        {tool('hline', 'Level', 'Horizontal line (one click)')}
        {tool('trend', 'Trend', 'Trend line (two clicks)')}
        {tool('rect', 'Box', 'Rectangle (two clicks)')}
      </div>
      <span className="tool-sep" />
      <label className="check" title={session ? 'Sessions always show traded (unadjusted) prices' : 'Remove roll gaps from the continuous series'}>
        <input type="checkbox" checked={adjusted && !session} disabled={!!session}
          onChange={(e) => setPrefs({ adjusted: e.target.checked })} />
        Back-adjusted
      </label>
    </div>
  )
}

function IndicatorPopover({ onClose }: { onClose: () => void }) {
  const catalog = useStore((s) => s.catalog)
  const indicators = useStore((s) => s.indicators)
  const setPrefs = useStore((s) => s.setPrefs)
  const [type, setType] = useState(catalog[0]?.type ?? 'ema')
  const info = catalog.find((c) => c.type === type)
  const [period, setPeriod] = useState<number>(Number(info?.params.period ?? 20))

  const add = () => {
    if (!info) return
    const params: Record<string, number | string> = { ...info.params }
    if ('period' in params) params.period = period
    const spec: IndicatorSpec = {
      id: `${type}-${Date.now().toString(36)}`, type, params,
      color: COLORS[indicators.length % COLORS.length],
    }
    setPrefs({ indicators: [...indicators, spec] })
    onClose()
  }

  return (
    <div className="popover" style={{ top: 32, left: 0 }}>
      <div className="row">
        <label htmlFor="ind-type">Indicator</label>
        <select id="ind-type" value={type} onChange={(e) => {
          setType(e.target.value)
          const p = catalog.find((c) => c.type === e.target.value)?.params.period
          if (p !== undefined) setPeriod(Number(p))
        }} style={{ width: 150 }}>
          {catalog.map((c) => <option key={c.type} value={c.type}>{c.label}</option>)}
        </select>
      </div>
      {info && 'period' in info.params && (
        <div className="row">
          <label htmlFor="ind-period">Length</label>
          <input id="ind-period" type="number" min={1} max={500} value={period} onChange={(e) => setPeriod(+e.target.value)} style={{ width: 150 }} />
        </div>
      )}
      <p className="help">Computed by the same Python code strategies use, so the chart shows what a bot sees.</p>
      <div className="row" style={{ justifyContent: 'flex-end' }}>
        <button className="btn ghost" onClick={onClose}>Close</button>
        <button className="btn primary" onClick={add}>Add to chart</button>
      </div>
    </div>
  )
}
