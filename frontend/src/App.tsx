import { lazy, Suspense, useEffect } from 'react'
import SessionDialog from './components/SessionDialog'
import Toasts from './components/Toasts'
import TopBar from './components/TopBar'
import DataPage from './pages/DataPage'
import TradePage from './pages/TradePage'
import { useStore } from './store'

const BacktestPage = lazy(() => import('./pages/BacktestPage'))
const BotsPage = lazy(() => import('./pages/BotsPage'))

export default function App() {
  const page = useStore((s) => s.page)
  const loadSymbols = useStore((s) => s.loadSymbols)
  useEffect(() => { loadSymbols().catch((e) => useStore.getState().toast('error', `Cannot reach the simulator: ${e.message}`)) }, [loadSymbols])
  return (
    <div className="app">
      <TopBar />
      {page === 'trade' && <TradePage />}
      {page === 'data' && <DataPage />}
      <Suspense fallback={<div className="page muted">Loading...</div>}>
        {page === 'backtest' && <BacktestPage />}
        {page === 'bots' && <BotsPage />}
      </Suspense>
      <SessionDialog />
      <Toasts />
    </div>
  )
}
