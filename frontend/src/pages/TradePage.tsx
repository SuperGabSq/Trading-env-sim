import { useEffect } from 'react'
import Chart from '../chart/Chart'
import ChartToolbar from '../chart/ChartToolbar'
import BottomPanel from '../components/BottomPanel'
import OrderTicket, { cancelAll, flatten, placeOrder } from '../components/OrderTicket'
import ReplayBar, { control } from '../components/ReplayBar'
import Watchlist from '../components/Watchlist'
import { useStore } from '../store'

export default function TradePage() {
  useHotkeys()
  return (
    <div className="trade">
      <Watchlist />
      <div className="chartwrap">
        <ChartToolbar />
        <Chart />
      </div>
      <ReplayBar />
      <OrderTicket />
      <BottomPanel />
    </div>
  )
}

function useHotkeys() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement
      if (el && (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) return
      const st = useStore.getState()
      const sess = st.session
      if (e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
        const k = e.key.toLowerCase()
        if (k === 'b') { e.preventDefault(); placeOrder('buy', { type: 'market' }) }
        else if (k === 's') { e.preventDefault(); placeOrder('sell', { type: 'market' }) }
        else if (k === 'f') { e.preventDefault(); flatten(st.symbol) }
        else if (k === 'c') { e.preventDefault(); cancelAll(st.symbol) }
        return
      }
      if (!sess || sess.mode !== 'replay') return
      if (e.key === ' ') {
        e.preventDefault()
        control(sess.state === 'playing' ? 'pause' : 'play')
      } else if (e.key === 'ArrowRight' && !e.shiftKey) {
        e.preventDefault()
        control('step', { tf: st.tf })
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
}
