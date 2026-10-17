import { useEffect, useMemo, useRef, useState } from 'react'
import {
  CandlestickSeries, CrosshairMode, HistogramSeries, LineSeries, LineStyle, createChart, createSeriesMarkers,
  type CandlestickData, type HistogramData, type IChartApi, type IPriceLine, type ISeriesApi,
  type ISeriesMarkersPluginApi, type LineData, type MouseEventParams, type SeriesMarker, type Time,
  type UTCTimestamp, type WhitespaceData,
} from 'lightweight-charts'
import { api, socket } from '../api'
import { fmtMoney, fmtPrice, roundTick, toChartTime } from '../format'
import { useStore } from '../store'
import type { BarsColumns, ContractInfo, IndicatorSpec, Order, WireBar } from '../types'
import { DrawingsPrimitive, type Drawing } from './drawings'

const LIMIT = 1500

function css(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

interface Data {
  key: string
  utc: number[]
  candles: CandlestickData[]
  vols: HistogramData[]
  ind: Record<string, Record<string, (number | null)[]>>
  hasMore: boolean
  loading: boolean
}

interface MenuState { x: number; y: number; price: number; drawingId: string | null }

type LineSeriesApi = ISeriesApi<'Line'>

export default function Chart() {
  const symbol = useStore((s) => s.symbol)
  const tf = useStore((s) => s.tf)
  const indicators = useStore((s) => s.indicators)
  const theme = useStore((s) => s.theme)
  const adjusted = useStore((s) => s.adjusted)
  const session = useStore((s) => s.session)
  const orders = useStore((s) => s.orders)
  const positions = useStore((s) => s.positions)
  const fills = useStore((s) => s.fills)
  const last = useStore((s) => s.last[symbol])
  const drawTool = useStore((s) => s.drawTool)
  const contracts = useStore((s) => s.contracts)
  const contract = useMemo(() => contracts.find((c) => c.symbol === symbol), [contracts, symbol])
  const tz = contract?.session.tz ?? 'UTC'

  const wrapRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  const indSeries = useRef<Map<string, Map<string, LineSeriesApi>>>(new Map())
  const drawRef = useRef<DrawingsPrimitive>(new DrawingsPrimitive())
  const dataRef = useRef<Data>({ key: '', utc: [], candles: [], vols: [], ind: {}, hasMore: true, loading: false })
  const orderLines = useRef<Map<number, IPriceLine>>(new Map())
  const posLine = useRef<IPriceLine | null>(null)
  const hLines = useRef<Map<string, IPriceLine>>(new Map())
  const dragRef = useRef<{ order: Order; field: 'price' | 'stop'; line: IPriceLine; price: number } | null>(null)
  const pendingDraw = useRef<Drawing | null>(null)
  const ctxRef = useRef({ symbol, tf, tz, contract, indicators })
  ctxRef.current = { symbol, tf, tz, contract, indicators }

  const [legend, setLegend] = useState<{ o: number; h: number; l: number; c: number; v: number; prev?: number } | null>(null)
  const [indLegend, setIndLegend] = useState<Record<string, number | null>>({})
  const [menu, setMenu] = useState<MenuState | null>(null)
  const [drawings, setDrawings] = useState<Drawing[]>([])
  const [notice, setNotice] = useState('')

  const dec = contract?.price_decimals ?? 2
  const tick = contract?.tick_size ?? 0.25
  const inSession = !!session && session.symbols.includes(symbol)

  // ------------------------------------------------------------------ chart creation
  useEffect(() => {
    const el = wrapRef.current!
    const chart = createChart(el, {
      autoSize: true,
      layout: { background: { color: css('--bg') }, textColor: css('--muted'), fontFamily: css('--font'), fontSize: 11,
        panes: { separatorColor: css('--line'), separatorHoverColor: css('--raised') }, attributionLogo: false },
      grid: { vertLines: { color: css('--chart-grid') }, horzLines: { color: css('--chart-grid') } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: css('--line') },
      timeScale: { borderColor: css('--line'), timeVisible: true, secondsVisible: false, rightOffset: 12, barSpacing: 7 },
      localization: { locale: 'en-US', priceFormatter: (p: number) => fmtPrice(p, ctxRef.current.contract?.price_decimals ?? 2) },
    })
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: css('--up'), downColor: css('--down'), borderVisible: false,
      wickUpColor: css('--up'), wickDownColor: css('--down'),
    })
    const vol = chart.addSeries(HistogramSeries, { priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false })
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } })
    candle.attachPrimitive(drawRef.current)
    chartRef.current = chart
    candleRef.current = candle
    volRef.current = vol
    markersRef.current = createSeriesMarkers(candle, [])
    drawRef.current.xOf = (utc) => xOfUtc(utc)

    chart.subscribeCrosshairMove(onCrosshair)
    chart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
      if (r && r.from < 25) loadOlder()
    })
    return () => {
      chart.remove()
      chartRef.current = null
      orderLines.current.clear()
      posLine.current = null
      hLines.current.clear()
      indSeries.current.clear()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // theme
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    chart.applyOptions({
      layout: { background: { color: css('--bg') }, textColor: css('--muted'), panes: { separatorColor: css('--line') } },
      grid: { vertLines: { color: css('--chart-grid') }, horzLines: { color: css('--chart-grid') } },
      rightPriceScale: { borderColor: css('--line') }, timeScale: { borderColor: css('--line') },
    })
    candleRef.current?.applyOptions({ upColor: css('--up'), downColor: css('--down'), wickUpColor: css('--up'), wickDownColor: css('--down') })
    recolorVolume()
  }, [theme])

  // precision
  useEffect(() => {
    candleRef.current?.applyOptions({ priceFormat: { type: 'price', precision: dec, minMove: tick } })
  }, [dec, tick])

  // ------------------------------------------------------------------ data helpers
  function xOfUtc(utc: number): number | null {
    const chart = chartRef.current
    const d = dataRef.current
    if (!chart || !d.utc.length) return null
    const t = d.utc
    let lo = 0, hi = t.length - 1
    if (utc <= t[0]) return chart.timeScale().logicalToCoordinate(0 as never)
    let logical: number
    if (utc >= t[hi]) {
      const step = t.length > 1 ? t[hi] - t[hi - 1] : 60
      logical = hi + (utc - t[hi]) / step
    } else {
      while (hi - lo > 1) {
        const mid = (lo + hi) >> 1
        if (t[mid] <= utc) lo = mid
        else hi = mid
      }
      logical = lo + (utc - t[lo]) / (t[hi] - t[lo])
    }
    return chart.timeScale().logicalToCoordinate(logical as never)
  }

  function utcOfX(x: number): number | null {
    const chart = chartRef.current
    const t = dataRef.current.utc
    if (!chart || !t.length) return null
    const logical = chart.timeScale().coordinateToLogical(x)
    if (logical === null) return null
    const i = Math.floor(logical)
    if (i < 0) return t[0]
    if (i >= t.length - 1) {
      const step = t.length > 1 ? t[t.length - 1] - t[t.length - 2] : 60
      return Math.round(t[t.length - 1] + (logical - (t.length - 1)) * step)
    }
    return Math.round(t[i] + (logical - i) * (t[i + 1] - t[i]))
  }

  function toBar(b: WireBar): CandlestickData {
    return { time: toChartTime(b.time, ctxRef.current.tf, ctxRef.current.tz) as UTCTimestamp, open: b.open, high: b.high, low: b.low, close: b.close }
  }

  function volBar(b: WireBar): HistogramData {
    return {
      time: toChartTime(b.time, ctxRef.current.tf, ctxRef.current.tz) as UTCTimestamp, value: b.volume,
      color: b.close >= b.open ? css('--up-soft') : css('--down-soft'),
    }
  }

  function recolorVolume() {
    const d = dataRef.current
    if (!volRef.current || !d.candles.length) return
    d.vols = d.vols.map((v, i) => ({ ...v, color: d.candles[i].close >= d.candles[i].open ? css('--up-soft') : css('--down-soft') }))
    volRef.current.setData(d.vols)
  }

  function columnsToWire(c: BarsColumns): WireBar[] {
    return c.time.map((t, i) => ({ time: t, open: c.open[i], high: c.high[i], low: c.low[i], close: c.close[i], volume: c.volume[i] }))
  }

  function lineData(times: number[], vals: (number | null)[] | undefined): (LineData | WhitespaceData)[] {
    const { tf, tz } = ctxRef.current
    return times.map((t, i) => {
      const v = vals?.[i]
      const time = toChartTime(t, tf, tz) as UTCTimestamp
      return v === null || v === undefined ? { time } : { time, value: v }
    })
  }

  // ------------------------------------------------------------------ indicator series
  function rebuildIndicatorSeries(specs: IndicatorSpec[]) {
    const chart = chartRef.current
    if (!chart) return
    for (const m of indSeries.current.values()) for (const s of m.values()) chart.removeSeries(s)
    indSeries.current.clear()
    for (let i = chart.panes().length - 1; i > 0; i--) chart.removePane(i)
    const catalog = useStore.getState().catalog
    let pane = 0
    for (const spec of specs) {
      const info = catalog.find((c) => c.type === spec.type)
      if (!info) continue
      const paneIndex = info.overlay ? 0 : ++pane
      const map = new Map<string, LineSeriesApi>()
      for (const out of info.outputs) {
        const s = chart.addSeries(LineSeries, {
          color: spec.color, lineWidth: out === 'middle' ? 1 : 2,
          lineStyle: out === 'middle' ? LineStyle.Dashed : LineStyle.Solid,
          priceLineVisible: false, lastValueVisible: !info.overlay || out === 'value',
          crosshairMarkerVisible: false,
          priceFormat: { type: 'price', precision: spec.type === 'rsi' ? 1 : ctxRef.current.contract?.price_decimals ?? 2, minMove: 0.01 },
        }, paneIndex)
        map.set(out, s)
      }
      indSeries.current.set(spec.id, map)
    }
    const panes = chart.panes()
    panes[0]?.setStretchFactor(4)
    for (let i = 1; i < panes.length; i++) panes[i].setStretchFactor(1)
  }

  // ------------------------------------------------------------------ subscribe / snapshot / updates
  const indKey = JSON.stringify(indicators.map((i) => [i.id, i.type, i.params, i.color]))
  const catalogReady = useStore((s) => s.catalog.length > 0)

  useEffect(() => {
    if (!catalogReady || !contract) return
    const key = `${symbol}|${tf}|${indKey}|${session?.id ?? ''}|${adjusted && !session}`
    dataRef.current = { key, utc: [], candles: [], vols: [], ind: {}, hasMore: true, loading: false }
    rebuildIndicatorSeries(indicators)
    candleRef.current?.setData([])
    volRef.current?.setData([])
    const specs = indicators.map((i) => ({ id: i.id, type: i.type, params: i.params }))
    if (adjusted && !session) {
      socket.send({ op: 'unsubscribe' })
      api.get<{ bars: BarsColumns; indicators: Record<string, Record<string, (number | null)[]>> }>(
        `/api/bars?symbol=${symbol}&tf=${tf}&limit=${LIMIT}&adjusted=true&indicators=${encodeURIComponent(JSON.stringify(specs))}`,
      ).then((r) => applySnapshot(key, r.bars, r.indicators, true)).catch((e) => setNotice(String(e.message)))
    } else {
      socket.send({ op: 'subscribe', symbol, tf, indicators: specs, limit: LIMIT })
    }
    const offSnap = socket.on('snapshot', (m) => {
      if (m.symbol !== symbol || m.tf !== tf || dataRef.current.key !== key) return
      applySnapshot(key, m.bars, m.indicators, true)
    })
    const offBars = socket.on('bars', (m) => {
      if (m.symbol !== symbol || m.tf !== tf || dataRef.current.key !== key) return
      applyUpdates(m.bars, m.indicators)
    })
    api.get<Drawing[]>(`/api/drawings/${symbol}`).then((d) => setDrawings(Array.isArray(d) ? d : [])).catch(() => setDrawings([]))
    return () => { offSnap(); offBars() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, tf, indKey, session?.id, adjusted, tz, catalogReady])

  function applySnapshot(key: string, cols: BarsColumns, ind: Record<string, Record<string, (number | null)[]>>, fit: boolean) {
    const d = dataRef.current
    if (d.key !== key) return
    const wire = columnsToWire(cols)
    d.utc = wire.map((b) => b.time)
    d.candles = wire.map(toBar)
    d.vols = wire.map(volBar)
    d.ind = ind || {}
    d.hasMore = wire.length >= LIMIT - 1
    candleRef.current?.setData(d.candles)
    volRef.current?.setData(d.vols)
    for (const [id, map] of indSeries.current) for (const [out, s] of map) s.setData(lineData(d.utc, d.ind[id]?.[out]))
    if (fit && d.candles.length) {
      const n = d.candles.length
      chartRef.current?.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 160), to: n + 10 })
    }
    setLegendFromLast()
    refreshMarkers()
    drawRef.current.update()
    setNotice('')
  }

  function applyUpdates(bars: WireBar[], ind: Record<string, Record<string, [number, number | null][]>>) {
    const d = dataRef.current
    if (!candleRef.current || !volRef.current) return
    for (const b of bars) {
      const cb = toBar(b)
      const vb = volBar(b)
      const n = d.utc.length
      try {
        if (n && d.utc[n - 1] === b.time) {
          d.candles[n - 1] = cb
          d.vols[n - 1] = vb
        } else if (!n || b.time > d.utc[n - 1]) {
          d.utc.push(b.time)
          d.candles.push(cb)
          d.vols.push(vb)
        } else continue
        candleRef.current.update(cb)
        volRef.current.update(vb)
      } catch { /* out-of-order update */ }
    }
    for (const [id, outs] of Object.entries(ind || {})) {
      const map = indSeries.current.get(id)
      if (!map) continue
      for (const [out, pts] of Object.entries(outs)) {
        const s = map.get(out)
        if (!s) continue
        for (const [t, v] of pts) {
          const time = toChartTime(t, ctxRef.current.tf, ctxRef.current.tz) as UTCTimestamp
          try { s.update(v === null ? { time } : { time, value: v }) } catch { /* ignore */ }
        }
      }
    }
    if (!crosshairActive.current) setLegendFromLast()
  }

  function loadOlder() {
    const d = dataRef.current
    if (d.loading || !d.hasMore || !d.utc.length) return
    d.loading = true
    const key = d.key
    const { symbol: sym, tf: tfr, indicators: inds } = ctxRef.current
    const specs = inds.map((i) => ({ id: i.id, type: i.type, params: i.params }))
    const adj = useStore.getState().adjusted && !useStore.getState().session
    api.get<{ bars: BarsColumns; indicators: Record<string, Record<string, (number | null)[]>> }>(
      `/api/bars?symbol=${sym}&tf=${tfr}&to=${d.utc[0]}&limit=${LIMIT}&adjusted=${adj}&indicators=${encodeURIComponent(JSON.stringify(specs))}`,
    ).then((r) => {
      if (dataRef.current.key !== key) return
      const wire = columnsToWire(r.bars)
      d.hasMore = wire.length >= LIMIT - 1
      if (!wire.length) return
      const ts = chartRef.current!.timeScale()
      const range = ts.getVisibleLogicalRange()
      d.utc = [...wire.map((b) => b.time), ...d.utc]
      d.candles = [...wire.map(toBar), ...d.candles]
      d.vols = [...wire.map(volBar), ...d.vols]
      for (const [id, outs] of Object.entries(r.indicators || {})) {
        d.ind[id] = d.ind[id] || {}
        for (const [out, vals] of Object.entries(outs)) d.ind[id][out] = [...vals, ...(d.ind[id][out] || [])]
      }
      candleRef.current!.setData(d.candles)
      volRef.current!.setData(d.vols)
      for (const [id, map] of indSeries.current) {
        for (const [out, s] of map) {
          const older = lineData(wire.map((b) => b.time), r.indicators?.[id]?.[out])
          const cur = s.data() as (LineData | WhitespaceData)[]
          s.setData([...older, ...cur])
        }
      }
      if (range) ts.setVisibleLogicalRange({ from: range.from + wire.length, to: range.to + wire.length })
      refreshMarkers()
    }).catch(() => { d.hasMore = false }).finally(() => { d.loading = false })
  }

  // ------------------------------------------------------------------ legend
  const crosshairActive = useRef(false)
  function onCrosshair(p: MouseEventParams<Time>) {
    const candle = candleRef.current
    if (!candle) return
    if (!p.time || !p.seriesData.size) {
      crosshairActive.current = false
      setLegendFromLast()
      return
    }
    crosshairActive.current = true
    const bar = p.seriesData.get(candle) as CandlestickData | undefined
    const vol = volRef.current ? (p.seriesData.get(volRef.current) as HistogramData | undefined) : undefined
    if (bar) {
      const d = dataRef.current
      const idx = d.candles.findIndex((c) => c.time === bar.time)
      setLegend({ o: bar.open, h: bar.high, l: bar.low, c: bar.close, v: vol?.value ?? 0, prev: idx > 0 ? d.candles[idx - 1].close : undefined })
    }
    const vals: Record<string, number | null> = {}
    for (const [id, map] of indSeries.current) {
      const first = map.values().next().value as LineSeriesApi | undefined
      const ld = first ? (p.seriesData.get(first) as LineData | undefined) : undefined
      vals[id] = ld?.value ?? null
    }
    setIndLegend(vals)
  }

  function setLegendFromLast() {
    const d = dataRef.current
    const n = d.candles.length
    if (!n) { setLegend(null); return }
    const b = d.candles[n - 1]
    setLegend({ o: b.open, h: b.high, l: b.low, c: b.close, v: d.vols[n - 1]?.value ?? 0, prev: n > 1 ? d.candles[n - 2].close : undefined })
    const vals: Record<string, number | null> = {}
    for (const [id, map] of indSeries.current) {
      const first = map.values().next().value as LineSeriesApi | undefined
      const arr = first ? (first.data() as LineData[]) : []
      const lastPt = arr[arr.length - 1]
      vals[id] = lastPt && 'value' in lastPt ? lastPt.value : null
    }
    setIndLegend(vals)
  }

  // ------------------------------------------------------------------ markers (fills)
  function refreshMarkers() {
    const d = dataRef.current
    const m = markersRef.current
    if (!m) return
    const { symbol: sym } = ctxRef.current
    const own = useStore.getState().fills.filter((f) => f.symbol === sym).slice(0, 400)
    const out: SeriesMarker<Time>[] = []
    for (const f of own) {
      const t = d.utc
      if (!t.length || f.ts < t[0]) continue
      let lo = 0, hi = t.length - 1
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1
        if (t[mid] <= f.ts) lo = mid
        else hi = mid - 1
      }
      const buy = f.side === 'buy'
      out.push({
        time: d.candles[lo].time, position: buy ? 'belowBar' : 'aboveBar', shape: buy ? 'arrowUp' : 'arrowDown',
        color: buy ? css('--up') : css('--down'), text: `${buy ? '+' : '-'}${f.qty}`, size: 0.9,
      })
    }
    out.sort((a, b) => (a.time as number) - (b.time as number))
    m.setMarkers(out)
  }
  useEffect(() => { refreshMarkers() }, [fills, symbol]) // eslint-disable-line react-hooks/exhaustive-deps

  // ------------------------------------------------------------------ order & position lines
  useEffect(() => {
    const candle = candleRef.current
    if (!candle) return
    const want = Object.values(orders).filter((o) => o.symbol === symbol && (o.price !== null || o.stop !== null))
    const keep = new Set(want.map((o) => o.id))
    for (const [id, line] of orderLines.current) {
      if (!keep.has(id)) { candle.removePriceLine(line); orderLines.current.delete(id) }
    }
    for (const o of want) {
      if (dragRef.current?.order.id === o.id) continue
      const field = linePriceField(o)
      const price = (field === 'price' ? o.price : o.stop) as number
      const opts = {
        price, color: o.side === 'buy' ? css('--up') : css('--down'), lineWidth: 1 as const,
        lineStyle: o.status === 'pending' ? LineStyle.Dotted : o.type === 'limit' ? LineStyle.Solid : LineStyle.Dashed,
        axisLabelVisible: true, title: orderTitle(o),
      }
      const existing = orderLines.current.get(o.id)
      if (existing) existing.applyOptions(opts)
      else orderLines.current.set(o.id, candle.createPriceLine(opts))
    }
  }, [orders, symbol, theme])

  useEffect(() => {
    const candle = candleRef.current
    if (!candle) return
    const p = positions[symbol]
    if (!p || !p.qty) {
      if (posLine.current) { candle.removePriceLine(posLine.current); posLine.current = null }
      return
    }
    const pv = contract?.point_value ?? 1
    const pnl = last !== undefined ? (last - p.avg_price) * p.qty * pv : 0
    const opts = {
      price: p.avg_price, color: css('--amber'), lineWidth: 2 as const, lineStyle: LineStyle.Solid, axisLabelVisible: true,
      title: `${p.qty > 0 ? 'Long' : 'Short'} ${Math.abs(p.qty)}  ${fmtMoney(pnl, true)}`,
    }
    if (posLine.current) posLine.current.applyOptions(opts)
    else posLine.current = candle.createPriceLine(opts)
  }, [positions, symbol, last, contract, theme])

  // horizontal-line drawings are price lines
  useEffect(() => {
    drawRef.current.drawings = drawings
    drawRef.current.update()
    const candle = candleRef.current
    if (!candle) return
    const hl = drawings.filter((d) => d.kind === 'hline')
    const ids = new Set(hl.map((d) => d.id))
    for (const [id, line] of hLines.current) if (!ids.has(id)) { candle.removePriceLine(line); hLines.current.delete(id) }
    for (const d of hl) {
      const opts = { price: d.a.price, color: d.color, lineWidth: 1 as const, lineStyle: LineStyle.LargeDashed, axisLabelVisible: true, title: '' }
      const e = hLines.current.get(d.id)
      if (e) e.applyOptions(opts)
      else hLines.current.set(d.id, candle.createPriceLine(opts))
    }
  }, [drawings])

  function saveDrawings(next: Drawing[]) {
    setDrawings(next)
    api.put(`/api/drawings/${ctxRef.current.symbol}`, next).catch(() => {})
  }

  // ------------------------------------------------------------------ pointer interaction: drag lines, draw, menus
  useEffect(() => {
    const el = wrapRef.current!
    const local = (e: MouseEvent) => {
      const r = el.getBoundingClientRect()
      return { x: e.clientX - r.left, y: e.clientY - r.top }
    }
    const inPlot = (x: number) => {
      const chart = chartRef.current
      if (!chart) return false
      return x < el.clientWidth - chart.priceScale('right').width()
    }
    const nearOrder = (y: number): { order: Order; field: 'price' | 'stop'; line: IPriceLine } | null => {
      const candle = candleRef.current
      if (!candle) return null
      const st = useStore.getState()
      for (const [id, line] of orderLines.current) {
        const o = st.orders[id]
        if (!o) continue
        const yy = candle.priceToCoordinate(line.options().price)
        if (yy !== null && Math.abs(yy - y) <= 5) return { order: o, field: linePriceField(o), line }
      }
      return null
    }

    const onDown = (e: MouseEvent) => {
      if (e.button !== 0) return
      const { x, y } = local(e)
      const tool = useStore.getState().drawTool
      const candle = candleRef.current
      if (!candle) return
      if (tool !== 'none' && inPlot(x)) {
        e.stopPropagation()
        e.preventDefault()
        const price = candle.coordinateToPrice(y)
        const t = utcOfX(x)
        if (price === null || t === null) return
        const color = css('--info')
        if (tool === 'hline') {
          saveDrawings([...drawRef.current.drawings, { id: crypto.randomUUID(), kind: 'hline', a: { t, price }, color: css('--muted') }])
          useStore.getState().set({ drawTool: 'none' })
          return
        }
        if (!pendingDraw.current) {
          pendingDraw.current = { id: crypto.randomUUID(), kind: tool, a: { t, price }, b: { t, price }, color }
          drawRef.current.preview = pendingDraw.current
        } else {
          const done = { ...pendingDraw.current, b: { t, price } }
          pendingDraw.current = null
          drawRef.current.preview = null
          saveDrawings([...drawRef.current.drawings, done])
          useStore.getState().set({ drawTool: 'none' })
        }
        drawRef.current.update()
        return
      }
      const hit = nearOrder(y)
      if (hit && inPlot(x)) {
        e.stopPropagation()
        e.preventDefault()
        dragRef.current = { ...hit, price: hit.line.options().price }
        chartRef.current?.applyOptions({ handleScroll: false, handleScale: false })
      }
    }
    const onMove = (e: MouseEvent) => {
      const { x, y } = local(e)
      const candle = candleRef.current
      if (!candle) return
      if (pendingDraw.current) {
        const price = candle.coordinateToPrice(y)
        const t = utcOfX(x)
        if (price !== null && t !== null) {
          pendingDraw.current.b = { t, price }
          drawRef.current.update()
        }
        return
      }
      const drag = dragRef.current
      if (drag) {
        const p = candle.coordinateToPrice(y)
        if (p === null) return
        const price = roundTick(p, ctxRef.current.contract?.tick_size ?? 0.25)
        drag.price = price
        drag.line.applyOptions({ price, title: `${orderTitle(drag.order)}  ${fmtPrice(price, ctxRef.current.contract?.price_decimals ?? 2)}` })
        return
      }
      const tool = useStore.getState().drawTool
      el.style.cursor = tool !== 'none' ? 'crosshair' : nearOrder(y) && inPlot(x) ? 'ns-resize' : ''
    }
    const onUp = () => {
      const drag = dragRef.current
      if (!drag) return
      dragRef.current = null
      chartRef.current?.applyOptions({ handleScroll: true, handleScale: true })
      const orig = drag.field === 'price' ? drag.order.price : drag.order.stop
      if (orig === drag.price) return
      api.patch(`/api/orders/${drag.order.id}`, { [drag.field]: drag.price })
        .catch((err) => {
          useStore.getState().toast('error', `Could not move order: ${err.message}`)
          drag.line.applyOptions({ price: orig as number })
        })
    }
    const onContext = (e: MouseEvent) => {
      e.preventDefault()
      const { x, y } = local(e)
      const candle = candleRef.current
      if (!candle || !inPlot(x)) return
      const price = candle.coordinateToPrice(y)
      if (price === null) return
      setMenu({ x: e.clientX, y: e.clientY, price: roundTick(price, ctxRef.current.contract?.tick_size ?? 0.25), drawingId: drawRef.current.findDrawing(x, y) })
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        pendingDraw.current = null
        drawRef.current.preview = null
        drawRef.current.update()
        useStore.getState().set({ drawTool: 'none' })
        setMenu(null)
      }
    }
    el.addEventListener('mousedown', onDown, true)
    window.addEventListener('mousemove', onMove, true)
    window.addEventListener('mouseup', onUp, true)
    el.addEventListener('contextmenu', onContext)
    window.addEventListener('keydown', onKey)
    return () => {
      el.removeEventListener('mousedown', onDown, true)
      window.removeEventListener('mousemove', onMove, true)
      window.removeEventListener('mouseup', onUp, true)
      el.removeEventListener('contextmenu', onContext)
      window.removeEventListener('keydown', onKey)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (drawTool === 'none') {
      pendingDraw.current = null
      drawRef.current.preview = null
      drawRef.current.update()
    }
  }, [drawTool])

  // ------------------------------------------------------------------ render
  const chg = legend && legend.prev !== undefined ? legend.c - legend.prev : null
  const catalog = useStore((s) => s.catalog)
  return (
    <div className="chart">
      <div ref={wrapRef} style={{ position: 'absolute', inset: 0 }} />
      <div className="legend">
        <div className="ohlc">
          <span className="title">{symbol} {tf}</span>
          {legend && <>
            <span><b>O</b>{fmtPrice(legend.o, dec)}</span>
            <span><b>H</b>{fmtPrice(legend.h, dec)}</span>
            <span><b>L</b>{fmtPrice(legend.l, dec)}</span>
            <span><b>C</b><span className={chg ? (chg > 0 ? 'up' : 'down') : ''}>{fmtPrice(legend.c, dec)}</span></span>
            {chg !== null && <span className={chg > 0 ? 'up' : chg < 0 ? 'down' : ''}>{chg > 0 ? '+' : ''}{fmtPrice(chg, dec)}</span>}
            <span><b>Vol</b>{Math.round(legend.v).toLocaleString()}</span>
          </>}
        </div>
        {indicators.map((i) => {
          const info = catalog.find((c) => c.type === i.type)
          const label = `${info?.label ?? i.type}${'period' in i.params ? ` ${i.params.period}` : ''}`
          const v = indLegend[i.id]
          return (
            <div className="ind" key={i.id}>
              <i className="swatch" style={{ background: i.color }} />
              <span className="muted">{label}</span>
              <span>{v === null || v === undefined ? '' : fmtPrice(v, i.type === 'rsi' ? 1 : dec)}</span>
              <button title={`Remove ${label}`} aria-label={`Remove ${label}`}
                onClick={() => useStore.getState().setPrefs({ indicators: useStore.getState().indicators.filter((x) => x.id !== i.id) })}>×</button>
            </div>
          )
        })}
      </div>
      {notice && <div className="chart-notice">{notice}</div>}
      {session && !inSession && <div className="chart-notice">{symbol} is not part of this session: full history shown, trading disabled</div>}
      {menu && <ChartMenu menu={menu} contract={contract} onClose={() => setMenu(null)}
        onDeleteDrawing={(id) => saveDrawings(drawRef.current.drawings.filter((d) => d.id !== id))}
        onHLine={(price) => saveDrawings([...drawRef.current.drawings, { id: crypto.randomUUID(), kind: 'hline', a: { t: 0, price }, color: css('--muted') }])}
        onClearDrawings={() => saveDrawings([])}
        hasDrawings={drawings.length > 0}
      />}
    </div>
  )
}

function linePriceField(o: Order): 'price' | 'stop' {
  if (o.type === 'limit') return 'price'
  if (o.type === 'stop_limit') return o.triggered ? 'price' : 'stop'
  return 'stop'
}

function orderTitle(o: Order): string {
  const kind: Record<string, string> = { limit: 'LMT', stop: 'STP', stop_limit: 'STP LMT', trailing_stop: 'TRAIL', market: 'MKT' }
  if (o.tag === 'tp') return `TP ${o.qty}${o.status === 'pending' ? ' (pending)' : ''}`
  if (o.tag === 'sl') return `SL ${o.qty}${o.status === 'pending' ? ' (pending)' : ''}`
  return `${o.side === 'buy' ? 'Buy' : 'Sell'} ${kind[o.type]} ${o.qty}`
}

function ChartMenu({ menu, contract, onClose, onDeleteDrawing, onHLine, onClearDrawings, hasDrawings }: {
  menu: MenuState; contract?: ContractInfo; onClose: () => void; onDeleteDrawing: (id: string) => void
  onHLine: (price: number) => void; onClearDrawings: () => void; hasDrawings: boolean
}) {
  const { session, symbol, ticket, last } = useStore.getState()
  const dec = contract?.price_decimals ?? 2
  const lastPx = last[symbol]
  const canTrade = !!session && session.symbols.includes(symbol)
  useEffect(() => {
    const close = () => onClose()
    window.addEventListener('mousedown', close)
    window.addEventListener('wheel', close)
    return () => { window.removeEventListener('mousedown', close); window.removeEventListener('wheel', close) }
  }, [onClose])
  const place = (side: 'buy' | 'sell', type: 'limit' | 'stop') => {
    const body: Record<string, unknown> = { symbol, side, qty: ticket.qty, type, tif: ticket.tif }
    if (type === 'limit') body.price = menu.price
    else body.stop = menu.price
    if (ticket.bracket) { body.tp_ticks = ticket.tp; body.sl_ticks = ticket.sl }
    api.post('/api/orders', body).catch((e) => useStore.getState().toast('error', e.message))
    onClose()
  }
  const px = fmtPrice(menu.price, dec)
  const below = lastPx === undefined || menu.price < lastPx
  const bracket = ticket.bracket ? ` +${ticket.tp}/-${ticket.sl}` : ''
  const left = Math.min(menu.x, window.innerWidth - 260)
  const top = Math.min(menu.y, window.innerHeight - 260)
  return (
    <div className="menu" style={{ left, top }} onMouseDown={(e) => e.stopPropagation()} role="menu">
      {canTrade ? (below ? <>
        <button role="menuitem" onClick={() => place('buy', 'limit')}><span className="up">Buy limit {ticket.qty} @ {px}</span><span className="faint">{bracket}</span></button>
        <button role="menuitem" onClick={() => place('sell', 'stop')}><span className="down">Sell stop {ticket.qty} @ {px}</span><span className="faint">{bracket}</span></button>
      </> : <>
        <button role="menuitem" onClick={() => place('sell', 'limit')}><span className="down">Sell limit {ticket.qty} @ {px}</span><span className="faint">{bracket}</span></button>
        <button role="menuitem" onClick={() => place('buy', 'stop')}><span className="up">Buy stop {ticket.qty} @ {px}</span><span className="faint">{bracket}</span></button>
      </>) : <div className="hint">Start a replay or live session to trade {symbol} from the chart.</div>}
      <hr />
      <button role="menuitem" onClick={() => { onHLine(menu.price); onClose() }}><span>Horizontal line at {px}</span></button>
      {menu.drawingId && <button role="menuitem" onClick={() => { onDeleteDrawing(menu.drawingId!); onClose() }}><span>Delete this drawing</span></button>}
      {hasDrawings && <button role="menuitem" onClick={() => { onClearDrawings(); onClose() }}><span className="muted">Remove all drawings</span></button>}
    </div>
  )
}
