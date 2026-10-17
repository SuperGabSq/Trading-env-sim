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
