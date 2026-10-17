import { create } from 'zustand'
import { api, socket } from './api'
import type {
  Account, CatalogItem, ContractInfo, DayRecord, Fill, IndicatorSpec, Order, Position, SessionState,
  SymbolInfo, Trade, TradingState,
} from './types'

export type Page = 'trade' | 'backtest' | 'data' | 'bots'

export interface Toast { id: number; kind: string; text: string }

export interface TicketState {
  qty: number
  type: 'market' | 'limit' | 'stop'
  price: number | null
  tif: 'gtc' | 'day'
  bracket: boolean
  tp: number
  sl: number
}

export type DrawTool = 'none' | 'hline' | 'trend' | 'rect'

interface Prefs {
  symbol: string
  tf: string
  indicators: IndicatorSpec[]
  theme: 'dark' | 'light'
  ticket: TicketState
  adjusted: boolean
}

const DEFAULT_PREFS: Prefs = {
  symbol: 'MES',
  tf: '5m',
  indicators: [
    { id: 'ema-21', type: 'ema', params: { period: 21, source: 'close' }, color: '#e9b44c' },
    { id: 'vwap', type: 'vwap', params: { source: 'hlc3' }, color: '#6aa7e8' },
  ],
  theme: 'dark',
  ticket: { qty: 1, type: 'market', price: null, tif: 'gtc', bracket: true, tp: 8, sl: 8 },
  adjusted: false,
}

function loadPrefs(): Prefs {
  try {
    const raw = localStorage.getItem('tsim:prefs')
    if (raw) return { ...DEFAULT_PREFS, ...JSON.parse(raw) }
  } catch { /* storage unavailable */ }
  return DEFAULT_PREFS
}

function savePrefs(p: Prefs) {
  try { localStorage.setItem('tsim:prefs', JSON.stringify(p)) } catch { /* ignore */ }
}

interface Store extends Prefs {
  page: Page
  connected: boolean
  symbols: SymbolInfo[]
  contracts: ContractInfo[]
  catalog: CatalogItem[]
  session: SessionState | null
  account: Account | null
  positions: Record<string, Position>
  orders: Record<number, Order>
  fills: Fill[]
  trades: Trade[]
  days: DayRecord[]
  last: Record<string, number>
  quotes: Record<string, { bid: number; ask: number; ts: number }>
  clock: number | null
  toasts: Toast[]
  drawTool: DrawTool
  showSessionDialog: boolean
  set: (p: Partial<Store>) => void
  setPrefs: (p: Partial<Prefs>) => void
  toast: (kind: string, text: string) => void
  loadSymbols: () => Promise<void>
  applyTradingState: (s: TradingState | null) => void
}

let toastId = 1

function applyTheme(t: 'dark' | 'light') {
  document.documentElement.dataset.theme = t
}
applyTheme(loadPrefs().theme)

export const useStore = create<Store>((set, get) => ({
  ...loadPrefs(),
  page: 'trade',
  connected: false,
  symbols: [],
  contracts: [],
  catalog: [],
  session: null,
  account: null,
  positions: {},
  orders: {},
  fills: [],
  trades: [],
  days: [],
  last: {},
  quotes: {},
  clock: null,
  toasts: [],
  drawTool: 'none',
  showSessionDialog: false,
  set: (p) => set(p),
  setPrefs: (p) => {
    if (p.theme) applyTheme(p.theme)
    set(p as Partial<Store>)
    const s = get()
    savePrefs({ symbol: s.symbol, tf: s.tf, indicators: s.indicators, theme: s.theme, ticket: s.ticket, adjusted: s.adjusted })
  },
  toast: (kind, text) => {
    const id = toastId++
    set({ toasts: [...get().toasts.slice(-4), { id, kind, text }] })
    setTimeout(() => set({ toasts: get().toasts.filter((t) => t.id !== id) }), kind === 'error' ? 9000 : 4500)
  },
  loadSymbols: async () => {
    const [symbols, contracts, catalog] = await Promise.all([
      api.get<SymbolInfo[]>('/api/symbols'),
      api.get<ContractInfo[]>('/api/contracts'),
      api.get<CatalogItem[]>('/api/indicators/catalog'),
    ])
    const s = get()
    const patch: Partial<Store> = { symbols, contracts, catalog }
    if (symbols.length && !symbols.find((x) => x.symbol === s.symbol)) patch.symbol = symbols[0].symbol
    set(patch)
  },
  applyTradingState: (st) => {
    if (!st) {
      set({ account: null, positions: {}, orders: {}, fills: [], trades: [], days: [] })
      return
    }
    const positions: Record<string, Position> = {}
    for (const p of st.positions) positions[p.symbol] = p
    const orders: Record<number, Order> = {}
    for (const o of st.orders) orders[o.id] = o
    set({
      account: st.account, positions, orders, fills: st.fills.slice().reverse(), trades: st.trades,
      days: st.days, last: { ...get().last, ...st.last },
    })
  },
}))

export const contractOf = (symbol: string): ContractInfo | undefined =>
  useStore.getState().contracts.find((c) => c.symbol === symbol)

// ---------------------------------------------------------------- wire socket events into the store
const verb = (side: string) => (side === 'buy' ? 'Bought' : 'Sold')

export function wireSocket() {
  const st = useStore
  socket.on('_open', () => st.getState().set({ connected: true }))
  socket.on('_close', () => st.getState().set({ connected: false }))
  socket.on('hello', (m) => {
    st.getState().set({ session: m.session })
    st.getState().applyTradingState(m.state)
  })
  socket.on('session', (m) => {
    const prev = st.getState().session
    st.getState().set({ session: m.data })
    if (!m.data) st.getState().applyTradingState(null)
    else if (m.data.account) st.getState().set({ account: m.data.account, clock: m.data.time })
    if (m.data && prev && prev.state !== 'ended' && m.data.state === 'ended') st.getState().toast('warning', 'Replay reached the end of the data')
  })
  socket.on('state', (m) => st.getState().applyTradingState(m.data))
  socket.on('account', (m) => st.getState().set({ account: m.data }))
  socket.on('clock', (m) => st.getState().set({ clock: m.data.time, last: { ...st.getState().last, ...m.data.last } }))
  socket.on('order', (m) => {
    const o: Order = m.data
    const orders = { ...st.getState().orders }
    if (o.status === 'working' || o.status === 'pending') orders[o.id] = o
    else delete orders[o.id]
    st.getState().set({ orders })
    if (o.status === 'rejected') st.getState().toast('error', `Order rejected: ${o.reason}`)
  })
  socket.on('fill', (m) => {
    const f: Fill = m.data
    st.getState().set({ fills: [f, ...st.getState().fills].slice(0, 500) })
    const c = contractOf(f.symbol)
    st.getState().toast(f.side, `${verb(f.side)} ${f.qty} ${f.symbol} @ ${f.price.toFixed(c?.price_decimals ?? 2)}${f.tag && f.tag !== 'manual' ? ` (${f.tag})` : ''}`)
  })
  socket.on('position', (m) => {
    const p: Position = m.data
    st.getState().set({ positions: { ...st.getState().positions, [p.symbol]: p } })
  })
  socket.on('trade', (m) => st.getState().set({ trades: [...st.getState().trades, m.data] }))
  socket.on('day', (m) => st.getState().set({ days: [...st.getState().days, m.data] }))
  socket.on('message', (m) => st.getState().toast(m.data.level, m.data.text))
  socket.on('quote', (m) => st.getState().set({ quotes: { ...st.getState().quotes, [m.data.symbol]: m.data } }))
  socket.on('roll', (m) => st.getState().toast('info', `${m.data.symbol} rolled ${m.data.from} -> ${m.data.to} (gap ${m.data.gap > 0 ? '+' : ''}${m.data.gap})`))
  socket.on('bars', (m) => {
    if (m.price != null) st.getState().set({ last: { ...st.getState().last, [m.symbol]: m.price } })
  })
  socket.connect()
}
