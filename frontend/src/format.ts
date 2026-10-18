import type { AssetClass, ContractInfo } from './types'

const offsetCache = new Map<string, number>()
const dtfCache = new Map<string, Intl.DateTimeFormat>()

function dtf(tz: string): Intl.DateTimeFormat {
  let f = dtfCache.get(tz)
  if (!f) {
    f = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    })
    dtfCache.set(tz, f)
  }
  return f
}

/** Seconds to add to a UTC epoch to get wall-clock time in `tz` (cached per hour). */
export function tzOffset(tz: string, utc: number): number {
  if (tz === 'UTC') return 0
  const hour = Math.floor(utc / 3600)
  const key = tz + hour
  const hit = offsetCache.get(key)
  if (hit !== undefined) return hit
  const parts = Object.fromEntries(dtf(tz).formatToParts(new Date(hour * 3600 * 1000)).map((p) => [p.type, p.value]))
  const asUtc = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second) / 1000
  const off = asUtc - hour * 3600
  offsetCache.set(key, off)
  return off
}

export const isDaily = (tf: string) => tf.endsWith('D') || tf.endsWith('W')

/** Chart x value: bars are shown in the exchange's wall-clock time. */
export function toChartTime(utc: number, tf: string, tz: string): number {
  return isDaily(tf) ? utc : utc + tzOffset(tz, utc)
}

export function fmtTime(utc: number | null | undefined, tz: string, withDate = true, withSeconds = false): string {
  if (!utc) return '-'
  const d = new Date((utc + tzOffset(tz, utc)) * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  const t = `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}${withSeconds ? ':' + p(d.getUTCSeconds()) : ''}`
  if (!withDate) return t
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${t}`
}

export function weekday(utc: number, tz: string): string {
  const d = new Date((utc + tzOffset(tz, utc)) * 1000)
  return ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getUTCDay()]
}

export function tzLabel(tz: string): string {
  if (tz === 'America/Chicago') return 'CT'
  if (tz === 'America/New_York') return 'ET'
  return tz === 'UTC' ? 'UTC' : tz
}

/** Wall-clock (in tz) string 'YYYY-MM-DDTHH:MM' -> UTC epoch. */
export function wallToUtc(value: string, tz: string): number {
  const [d, t] = value.split('T')
  const [y, m, day] = d.split('-').map(Number)
  const [hh, mm] = (t || '00:00').split(':').map(Number)
  const guess = Date.UTC(y, m - 1, day, hh, mm) / 1000
  const off = tzOffset(tz, guess - tzOffset(tz, guess))
  return guess - off
}

export function utcToWall(utc: number, tz: string): string {
  return fmtTime(utc, tz).replace(' ', 'T')
}

export function fmtPrice(v: number | null | undefined, decimals = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '-'
  return v.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
}

export function fmtMoney(v: number | null | undefined, sign = false): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '-'
  const s = Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  if (v < 0) return `-$${s}`
  return `${sign && v > 0 ? '+' : ''}$${s}`
}

export function pnlClass(v: number | null | undefined): string {
  if (!v) return ''
  return v > 0 ? 'up' : 'down'
}

export function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '-'
  return v.toLocaleString('en-US', { maximumFractionDigits: digits })
}

export function roundTick(p: number, tick: number): number {
  const decimals = (String(tick).split('.')[1] || '').length
  return +(Math.round(p / tick) * tick).toFixed(decimals)
}

// ---------------------------------------------------------------- instruments


export const ASSET_CLASSES: { id: AssetClass; label: string }[] = [
  { id: 'future', label: 'Futures' },
  { id: 'crypto', label: 'Crypto' },
  { id: 'forex', label: 'Forex & gold' },
  { id: 'stock', label: 'Stocks' },
  { id: 'etf', label: 'ETFs' },
]

export const assetLabel = (a: string | undefined) => ASSET_CLASSES.find((x) => x.id === a)?.label ?? 'Other'

/** USD value of a 1.0 price move for one unit of quantity (USD-base forex pairs convert at the price). */
export function usdPerPoint(c: ContractInfo | undefined, price: number | undefined): number {
  if (!c) return 0
  if (c.quote && c.quote !== 'USD') return price ? c.point_value / price : 0
  return c.point_value
}

export const tickValueUsd = (c: ContractInfo | undefined, price: number | undefined) =>
  c ? usdPerPoint(c, price) * c.tick_size : 0

/** Profit in ticks of a closed trade, from its prices (correct for any quote currency). */
export function tradeTicks(t: { side: string; entry_price: number; exit_price: number | null; qty: number; pnl: number },
  c: ContractInfo | undefined): number {
  if (!c) return 0
  if (t.exit_price === null || t.exit_price === undefined) return t.qty ? t.pnl / (t.qty * c.tick_value) : 0
  return ((t.exit_price - t.entry_price) / c.tick_size) * (t.side === 'long' ? 1 : -1)
}

/** Label for the quantity field: Contracts, Shares, Micro lots... */
export function qtyLabel(c: ContractInfo | undefined): string {
  if (!c) return 'Contracts'
  if (c.asset_class === 'stock' || c.asset_class === 'etf') return 'Shares'
  if (c.unit.startsWith('micro lot')) return 'Micro lots'
  if (c.unit === 'ounce') return 'Ounces'
  return 'Contracts'
}

/** Short explanation of what the quantity means, e.g. "100 = 1 standard lot". */
export function qtyHint(c: ContractInfo | undefined, qty: number): string {
  if (!c) return ''
  if (c.unit.startsWith('micro lot')) {
    const units = qty * 1000
    return `${units.toLocaleString('en-US')} ${c.symbol.slice(0, 3)} (${(qty / 100).toLocaleString('en-US', { maximumFractionDigits: 2 })} lots)`
  }
  if (c.unit.startsWith('contract (')) return c.unit.replace('contract (', '1 = ').replace(')', '')
  return ''
}

/** Grouped and sorted for <optgroup> menus and the watchlist. */
export function groupByClass<T>(items: T[], cls: (x: T) => string | undefined): { id: string; label: string; items: T[] }[] {
  const out: { id: string; label: string; items: T[] }[] = []
  for (const a of [...ASSET_CLASSES.map((x) => x.id as string), 'other']) {
    const its = items.filter((x) => (cls(x) ?? 'other') === a || (a === 'other' && !ASSET_CLASSES.some((y) => y.id === cls(x))))
    if (its.length) out.push({ id: a, label: assetLabel(a), items: its })
  }
  return out
}
