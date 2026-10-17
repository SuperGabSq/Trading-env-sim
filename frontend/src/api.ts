export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(method: string, path: string, body?: unknown, raw?: BodyInit): Promise<T> {
  const init: RequestInit = { method, headers: {} }
  if (raw !== undefined) {
    init.body = raw
    ;(init.headers as Record<string, string>)['Content-Type'] = 'application/octet-stream'
  } else if (body !== undefined) {
    init.body = JSON.stringify(body)
    ;(init.headers as Record<string, string>)['Content-Type'] = 'application/json'
  }
  const res = await fetch(path, init)
  if (!res.ok) {
    let msg = res.statusText
    try {
      const j = await res.json()
      msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail ?? j)
    } catch { /* not json */ }
    throw new ApiError(res.status, msg)
  }
  const ct = res.headers.get('content-type') || ''
  return (ct.includes('json') ? res.json() : res.text()) as Promise<T>
}

export const api = {
  get: <T>(p: string) => request<T>('GET', p),
  post: <T>(p: string, b?: unknown) => request<T>('POST', p, b ?? {}),
  put: <T>(p: string, b?: unknown) => request<T>('PUT', p, b ?? {}),
  patch: <T>(p: string, b?: unknown) => request<T>('PATCH', p, b ?? {}),
  del: <T>(p: string) => request<T>('DELETE', p),
  upload: <T>(p: string, data: BodyInit) => request<T>('POST', p, undefined, data),
}

// ---------------------------------------------------------------- WebSocket with reconnect + bus
type Handler = (msg: any) => void

class Socket {
  private ws: WebSocket | null = null
  private handlers = new Map<string, Set<Handler>>()
  private queue: string[] = []
  private retry = 0
  private lastSub: unknown = null
  connected = false

  connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/ws`)
    this.ws = ws
    ws.onopen = () => {
      this.connected = true
      this.retry = 0
      this.emit('_open', {})
      if (this.lastSub) ws.send(JSON.stringify(this.lastSub))
      for (const q of this.queue.splice(0)) ws.send(q)
    }
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data)
        this.emit(msg.type, msg)
        this.emit('*', msg)
      } catch { /* ignore */ }
    }
    ws.onclose = () => {
      this.connected = false
      this.emit('_close', {})
      const delay = Math.min(5000, 300 * 2 ** this.retry++)
      setTimeout(() => this.connect(), delay)
    }
  }

  send(msg: Record<string, unknown>) {
    const s = JSON.stringify(msg)
    if (msg.op === 'subscribe') this.lastSub = msg
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(s)
    else if (msg.op !== 'subscribe') this.queue.push(s)
  }

  on(type: string, fn: Handler): () => void {
    if (!this.handlers.has(type)) this.handlers.set(type, new Set())
    this.handlers.get(type)!.add(fn)
    return () => this.handlers.get(type)?.delete(fn)
  }

  private emit(type: string, msg: unknown) {
    this.handlers.get(type)?.forEach((fn) => fn(msg))
  }
}

export const socket = new Socket()
