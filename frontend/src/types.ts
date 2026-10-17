export interface ContractInfo {
  symbol: string
  name: string
  exchange: string
  tick_size: number
  tick_value: number
  point_value: number
  price_decimals: number
  session: { name: string; tz: string; open: string; close: string; rth: [string, string]; day_margin_end: string | null }
  months: string
  perpetual: boolean
  margin: Record<string, number>
  fees_per_side: number
  fees_per_side_rate: number
}

export interface Roll { ts: number; from_contract: string; to_contract: string; gap: number }

export interface SymbolInfo {
  symbol: string
  source: string
  synthetic: boolean
  base_tf: string
  rows: number
  first_ts: number | null
  last_ts: number | null
  rolls: Roll[]
  notes: string
  last_close: number | null
  contract?: ContractInfo
}

export interface IndicatorSpec {
  id: string
  type: string
  params: Record<string, number | string>
  color: string
  pane?: number
}

export interface CatalogItem { type: string; label: string; params: Record<string, number | string>; outputs: string[]; overlay: boolean }

export interface Order {
  id: number
  account: string
  symbol: string
  side: 'buy' | 'sell'
  qty: number
  type: 'market' | 'limit' | 'stop' | 'stop_limit' | 'trailing_stop'
  price: number | null
  stop: number | null
  trail_ticks: number | null
  tif: string
  reduce_only: boolean
  parent_id: number | null
  oco: string | null
  tag: string
  status: 'pending' | 'working' | 'filled' | 'canceled' | 'rejected'
  filled_qty: number
  avg_fill: number | null
  created_ts: number
  updated_ts: number
  reason: string
  triggered: boolean
  child_ids: number[]
}

export interface Fill {
  id: number; order_id: number; account: string; symbol: string; side: 'buy' | 'sell'; qty: number
  price: number; fee: number; ts: number; realized: number; position_after: number; tag: string
}

export interface Position { account: string; symbol: string; qty: number; avg_price: number; realized: number; open_ts: number | null }

export interface Trade {
  id: number; account: string; symbol: string; side: 'long' | 'short'; qty: number
  entry_price: number; entry_ts: number; exit_price: number | null; exit_ts: number | null
  pnl: number; fees: number; net: number; mae_ticks: number; mfe_ticks: number; exit_reason: string; entry_tag: string
}

export interface Account {
  id: string; starting_balance: number; balance: number; equity: number; unrealized: number; day_pnl: number
  day_realized: number; day_fees: number; total_fees: number; margin_used: number; overnight_margin: number
  buying_power: number; trades: number; locked: string; ts: number
}

export interface DayRecord { date: string; equity: number; pnl: number; realized: number; fees: number }

export interface SessionState {
  id: string
  mode: 'replay' | 'live'
  symbols: string[]
  state: 'paused' | 'playing' | 'ended' | 'stopped' | 'running'
  speed: number
  time: number | null
  start: number | null
  next: number | null
  end: number | null
  fill: { slippage_ticks: number; limit_through_ticks: number; intrabar: string; roll_fees: boolean; spread_model: boolean; max_volume_pct: number | null; latency_ms: number }
  risk: { daily_loss_limit: number | null; max_position: number | null; check_margin: boolean; auto_liquidate: boolean }
  account: Account
  live: null | { source: string; status: string; detail?: string; symbols: string[] }
  strategies: StrategyRunInfo[]
}

export interface StrategyRunInfo { id: string; name: string; symbol: string; timeframe: string; params: Record<string, unknown>; status: string; account: string }

export interface TradingState {
  account: Account
  positions: (Position & { unrealized: number; last: number | null })[]
  orders: Order[]
  fills: Fill[]
  trades: Trade[]
  days: DayRecord[]
  last: Record<string, number>
}

export interface BarsColumns { time: number[]; open: number[]; high: number[]; low: number[]; close: number[]; volume: number[] }

export interface WireBar { time: number; open: number; high: number; low: number; close: number; volume: number }
