import type {
  IChartApi, IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive, SeriesAttachedParameter,
  SeriesType, Time,
} from 'lightweight-charts'

export interface Anchor { t: number; price: number } // t = UTC epoch seconds
export interface Drawing { id: string; kind: 'hline' | 'trend' | 'rect'; a: Anchor; b?: Anchor; color: string }

/** Draws trend lines and rectangles anchored to (time, price) so they survive timeframe changes. */
export class DrawingsPrimitive implements ISeriesPrimitive<Time> {
  drawings: Drawing[] = []
  preview: Drawing | null = null
  selected: string | null = null
  xOf: (utc: number) => number | null = () => null
  private chart: IChartApi | null = null
  private series: ISeriesApi<SeriesType> | null = null
  private request: (() => void) | null = null
  private view: IPrimitivePaneView

  constructor() {
    const self = this
    const renderer: IPrimitivePaneRenderer = {
      draw(target) {
        target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
          const all = self.preview ? [...self.drawings, self.preview] : self.drawings
          for (const d of all) {
            if (d.kind === 'hline' || !d.b || !self.series) continue
            const x1 = self.xOf(d.a.t), x2 = self.xOf(d.b.t)
            const y1 = self.series.priceToCoordinate(d.a.price), y2 = self.series.priceToCoordinate(d.b.price)
            if (x1 === null || x2 === null || y1 === null || y2 === null) continue
            const sel = d.id === self.selected || d === self.preview
            ctx.lineWidth = sel ? 2 : 1.5
            ctx.strokeStyle = d.color
            if (d.kind === 'trend') {
              ctx.beginPath()
              ctx.moveTo(x1, y1)
              ctx.lineTo(x2, y2)
              ctx.stroke()
              if (sel) for (const [x, y] of [[x1, y1], [x2, y2]]) { ctx.beginPath(); ctx.arc(x, y, 3.5, 0, Math.PI * 2); ctx.fillStyle = d.color; ctx.fill() }
            } else {
              const x = Math.min(x1, x2), y = Math.min(y1, y2)
              const w = Math.abs(x2 - x1), h = Math.abs(y2 - y1)
              ctx.globalAlpha = 0.12
              ctx.fillStyle = d.color
              ctx.fillRect(x, y, w, h)
              ctx.globalAlpha = 1
              ctx.strokeRect(x, y, w, h)
            }
          }
          void mediaSize
        })
      },
    }
    this.view = { renderer: () => renderer }
  }

  attached(p: SeriesAttachedParameter<Time>) {
    this.chart = p.chart as IChartApi
    this.series = p.series
    this.request = p.requestUpdate
  }

  detached() {
    this.chart = null
    this.series = null
    this.request = null
  }

  updateAllViews() { /* coordinates are computed at draw time */ }

  paneViews() { return [this.view] }

  update() { this.request?.() }

  /** Drawing id near a point (px), for selection / deletion. */
  findDrawing(x: number, y: number): string | null {
    if (!this.series) return null
    for (const d of [...this.drawings].reverse()) {
      if (d.kind === 'hline') {
        const yy = this.series.priceToCoordinate(d.a.price)
        if (yy !== null && Math.abs(yy - y) < 6) return d.id
        continue
      }
      if (!d.b) continue
      const x1 = this.xOf(d.a.t), x2 = this.xOf(d.b.t)
      const y1 = this.series.priceToCoordinate(d.a.price), y2 = this.series.priceToCoordinate(d.b.price)
      if (x1 === null || x2 === null || y1 === null || y2 === null) continue
      if (d.kind === 'trend') {
        if (distToSegment(x, y, x1, y1, x2, y2) < 6) return d.id
      } else {
        const inX = x >= Math.min(x1, x2) - 4 && x <= Math.max(x1, x2) + 4
        const inY = y >= Math.min(y1, y2) - 4 && y <= Math.max(y1, y2) + 4
        const nearEdge = Math.min(Math.abs(x - x1), Math.abs(x - x2), Math.abs(y - y1), Math.abs(y - y2)) < 6
        if (inX && inY && nearEdge) return d.id
      }
    }
    void this.chart
    return null
  }
}

function distToSegment(px: number, py: number, x1: number, y1: number, x2: number, y2: number): number {
  const dx = x2 - x1, dy = y2 - y1
  const len = dx * dx + dy * dy
  let t = len ? ((px - x1) * dx + (py - y1) * dy) / len : 0
  t = Math.max(0, Math.min(1, t))
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))
}
