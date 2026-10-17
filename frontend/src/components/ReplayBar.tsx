import { api } from '../api'
import { fmtTime, tzLabel, tzOffset, wallToUtc, weekday } from '../format'
import { useStore } from '../store'
import type { ContractInfo } from '../types'

const SPEEDS: [number, string][] = [
  [1, '1x real time'], [2, '2x'], [5, '5x'], [10, '10x'], [30, '30x'], [60, '60x (1 min bar/s)'],
  [120, '120x'], [300, '300x'], [600, '600x'], [1800, '1800x'], [3600, '3600x (1 h/s)'], [0, 'Max'],
]

export async function control(action: string, extra: Record<string, unknown> = {}) {
  try {
    await api.post('/api/session/control', { action, ...extra })
  } catch (e) {
    useStore.getState().toast('error', (e as Error).message)
  }
}

export default function ReplayBar() {
  const session = useStore((s) => s.session)
  const clock = useStore((s) => s.clock)
  const symbol = useStore((s) => s.symbol)
  const tf = useStore((s) => s.tf)
  const contracts = useStore((s) => s.contracts)
  const set = useStore((s) => s.set)
  const c = contracts.find((x) => x.symbol === (session?.symbols.includes(symbol) ? symbol : session?.symbols[0] ?? symbol))
  const tz = c?.session.tz ?? 'UTC'

  if (!session) {
    return (
      <div className="replay">
        <div className="replay-inner">
          <span className="muted">Viewing history. Start a session to trade:</span>
          <button className="btn primary" onClick={() => set({ showSessionDialog: true })}>Start replay</button>
          <button className="btn" onClick={() => { set({ showSessionDialog: true }); sessionStorageSet('tsim:dialogTab', 'live') }}>Go live</button>
        </div>
      </div>
    )
  }

  const t = clock ?? session.time
  const replay = session.mode === 'replay'
  const playing = session.state === 'playing'
  const ended = session.state === 'ended'

  return (
    <div className="replay">
      <div className="replay-inner">
        <span className={`mode-pill ${replay ? '' : 'live'}`}>{replay ? 'Replay' : 'Live'}</span>
        {replay && (
          <div className="transport">
            <button className="play" aria-label={playing ? 'Pause' : 'Play'} title={playing ? 'Pause (Space)' : 'Play (Space)'} disabled={ended}
              onClick={() => control(playing ? 'pause' : 'play')}>
              {playing ? <PauseIcon /> : <PlayIcon />}
            </button>
            <button aria-label="Step one bar" title={`Step one ${tf} bar (Right arrow)`} disabled={ended} onClick={() => control('step', { tf })}><StepIcon /></button>
          </div>
        )}
        {replay && (
          <select aria-label="Replay speed" value={session.speed} onChange={(e) => control('speed', { speed: +e.target.value })} style={{ width: 150 }}>
            {SPEEDS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
        )}
        <div className="clock">
          {t ? `${weekday(t, tz)} ${fmtTime(t, tz)}` : '-'} <span className="muted">{tzLabel(tz)}</span>
          <small>{replay ? (ended ? 'End of data' : playing ? 'Playing' : 'Paused') : session.live?.status ?? session.state}{session.live?.detail ? `: ${session.live.detail}` : ''}</small>
        </div>
        {c && t && <SessionRibbon contract={c} t={t} canJump={replay && !ended} />}
        <button className="btn ghost" onClick={() => set({ showSessionDialog: true })}>New session</button>
        <button className="btn danger" onClick={() => api.del('/api/session').catch(() => {})}>Stop</button>
      </div>
    </div>
  )
}

function sessionStorageSet(k: string, v: string) {
  try { sessionStorage.setItem(k, v) } catch { /* ignore */ }
}

function hm(s: string): number {
  const [h, m] = s.split(':').map(Number)
  return h * 60 + m
}

/** One trading day as a strip: overnight, regular hours, the margin switch and where the clock is. */
function SessionRibbon({ contract, t, canJump }: { contract: ContractInfo; t: number; canJump: boolean }) {
  const s = contract.session
  const tz = s.tz
  const open = hm(s.open), close = hm(s.close)
  const overnight = open >= close && !(open === 0 && close === 1440)
  const local = new Date((t + tzOffset(tz, t)) * 1000)
  const minOfDay = local.getUTCHours() * 60 + local.getUTCMinutes()
  const tradeDate = new Date(Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate()))
  if (overnight && minOfDay >= open) tradeDate.setUTCDate(tradeDate.getUTCDate() + 1)
  const startDay = new Date(tradeDate)
  if (overnight) startDay.setUTCDate(startDay.getUTCDate() - 1)
  const iso = (d: Date) => d.toISOString().slice(0, 10)
  const pad = (n: number) => String(n).padStart(2, '0')
  const wall = (d: Date, m: number) => `${iso(d)}T${pad(Math.floor(m / 60))}:${pad(m % 60)}`
  const start = wallToUtc(wall(startDay, open), tz)
  const total = (overnight ? 1440 - open + close : close - open) * 60
  const frac = (x: number) => Math.max(0, Math.min(1, (x - start) / total))
  const rthA = wallToUtc(wall(tradeDate, hm(s.rth[0])), tz)
  const rthB = wallToUtc(wall(tradeDate, hm(s.rth[1])), tz)
  const marginEnd = s.day_margin_end
  const ticks: [number, string][] = overnight
    ? [[start, s.open], [rthA, s.rth[0]], [rthB, s.rth[1]]]
    : [[start, '00:00'], [start + total / 4, '06:00'], [start + total / 2, '12:00'], [start + (3 * total) / 4, '18:00']]
  const p = frac(t)
  const jump = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!canJump) return
    const r = e.currentTarget.getBoundingClientRect()
    const ts = Math.round(start + ((e.clientX - r.left) / r.width) * total)
    if (ts > t) control('jump', { ts })
    else useStore.getState().toast('info', 'Replay only moves forward. Start a new session to go back.')
  }
  return (
    <div className="ribbon" onClick={jump} title={canJump ? 'Click ahead of the clock to fast-forward' : undefined}
      role="img" aria-label={`Trade date ${iso(tradeDate)}, ${Math.round(p * 100)}% through the session`}>
      <div className="track" />
      <div className="rth" style={{ left: `${frac(rthA) * 100}%`, width: `${(frac(rthB) - frac(rthA)) * 100}%` }} />
      <div className="passed" style={{ width: `${p * 100}%` }} />
      {marginEnd && overnight && <div className="mark" title={`Overnight margin from ${marginEnd}`} style={{ left: `${frac(wallToUtc(wall(tradeDate, hm(marginEnd)), tz)) * 100}%` }} />}
      <div className="head" style={{ left: `calc(${p * 100}% - 1px)` }} />
      {ticks.map(([x, l]) => <span key={l} className="tick" style={{ left: `${frac(x) * 100}%` }}>{l}</span>)}
    </div>
  )
}

const PlayIcon = () => <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden><path d="M2 1l9 5-9 5z" fill="currentColor" /></svg>
const PauseIcon = () => <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden><path d="M2 1h3v10H2zM7 1h3v10H7z" fill="currentColor" /></svg>
const StepIcon = () => <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden><path d="M1 1l7 5-7 5zM9 1h2v10H9z" fill="currentColor" /></svg>
