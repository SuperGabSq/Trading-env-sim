import { Component, type ReactNode } from 'react'

export default class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="page">
        <h1>The interface hit an error</h1>
        <p className="lead">Your session keeps running on the server. Reload the page to reconnect.</p>
        <pre className="code">{String(this.state.error.stack || this.state.error)}</pre>
        <button className="btn primary" onClick={() => location.reload()}>Reload</button>
      </div>
    )
  }
}
