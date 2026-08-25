import { useEffect, useRef, useState } from 'react'
import './App.css'

// 与服务端内部事件协议一致的事件类型全集
const EVENT_TYPES = [
  'run.started',
  'user.message',
  'agent.thinking',
  'agent.message',
  'agent.tool_started',
  'agent.tool_finished',
  'stage.changed',
  'turn.stopped',
  'turn.completed',
  'run.canceled',
  'run.failed',
]

const STATUS_LABEL = {
  WAITING_INPUT: '等待输入',
  RUNNING: '执行中',
  CANCELED: '已关闭',
  FAILED: '失败',
}

async function postJson(url, body) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`)
  return data
}

function EventItem({ ev }) {
  const { type, payload } = ev
  if (type === 'user.message') {
    return (
      <div className="ev user">
        <span className="who">你 ▸</span> {payload.text}
      </div>
    )
  }
  if (type === 'agent.thinking') {
    return (
      <details className="ev thinking">
        <summary>💭 Thinking</summary>
        <p>{payload.text}</p>
      </details>
    )
  }
  if (type === 'agent.message') {
    return <div className="ev message">{payload.text}</div>
  }
  if (type === 'agent.tool_started') {
    return (
      <div className="ev tool">
        ▶ {payload.tool} <span className="summary">{payload.summary}</span>
      </div>
    )
  }
  if (type === 'agent.tool_finished') {
    return (
      <div className="ev tool">
        ✔ {payload.tool} <span className="summary">{payload.summary}</span>
      </div>
    )
  }
  if (type === 'stage.changed') {
    return (
      <div className="ev stage-divider">
        <span>─ 进入 {payload.stage} ─</span>
      </div>
    )
  }
  if (type === 'turn.completed') {
    return (
      <div className="ev turn-card">
        <div className="card-title">回合汇总</div>
        <div className="card-body">{payload.result}</div>
      </div>
    )
  }
  if (type === 'run.failed') {
    return (
      <div className="ev run-failed">
        会话异常终止：{payload.message}
      </div>
    )
  }
  if (type === 'run.canceled') {
    return <div className="ev run-canceled">会话已关闭</div>
  }
  return null
}

export default function App() {
  const [runId, setRunId] = useState(null)
  const [status, setStatus] = useState(null)
  const [events, setEvents] = useState([])
  const [reconnecting, setReconnecting] = useState(false)
  const [input, setInput] = useState('')
  const [error, setError] = useState(null)
  const listRef = useRef(null)

  useEffect(() => {
    if (!runId) return
    // 浏览器 EventSource 断线自动重连并携带 Last-Event-ID，服务端从 seq+1 补发
    const es = new EventSource(`/api/runs/${runId}/events`)
    es.onopen = () => setReconnecting(false)
    es.onerror = () => setReconnecting(true)
    const onEvent = (e) => {
      const seq = Number(e.lastEventId)
      const payload = JSON.parse(e.data)
      setEvents((prev) =>
        prev.some((ev) => ev.seq === seq) ? prev : [...prev, { seq, type: e.type, payload }],
      )
      if (e.type === 'turn.completed') setStatus('WAITING_INPUT')
      if (e.type === 'run.failed') setStatus('FAILED')
      if (e.type === 'run.canceled') setStatus('CANCELED')
      // 终态后服务端会关闭流，主动 close 避免 EventSource 无限重连
      if (e.type === 'run.failed' || e.type === 'run.canceled') es.close()
    }
    for (const type of EVENT_TYPES) es.addEventListener(type, onEvent)
    return () => es.close()
  }, [runId])

  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [events, reconnecting])

  async function newSession() {
    setError(null)
    try {
      const data = await postJson('/api/runs', {})
      setEvents([])
      setStatus(data.status)
      setRunId(data.run_id)
    } catch (err) {
      setError(`新建会话失败：${err.message}`)
    }
  }

  async function send() {
    const text = input.trim()
    if (!text || !runId) return
    setError(null)
    try {
      const data = await postJson(`/api/runs/${runId}/messages`, { text })
      setStatus(data.status)
      setInput('')
    } catch (err) {
      setError(`发送失败：${err.message}`)
    }
  }

  const inputDisabled = !runId || status === 'RUNNING' || status === 'CANCELED' || status === 'FAILED'

  return (
    <div className="app">
      <header className="header">
        <span className="run-id">{runId ?? '未创建会话'}</span>
        <span className={`status status-${status ?? 'none'}`}>{STATUS_LABEL[status] ?? '—'}</span>
        <button onClick={newSession}>+ 新建</button>
      </header>

      <main className="stream" ref={listRef}>
        {reconnecting && (
          <div className="reconnect-banner">连接已断开，正在重连并从断点续传…</div>
        )}
        {events.length === 0 && (
          <div className="empty">新建一个会话，输入第一条部署指令（软件 + 文档链接 + 目标机器）。</div>
        )}
        {events.map((ev) => (
          <EventItem key={ev.seq} ev={ev} />
        ))}
      </main>

      {error && <div className="error-banner">{error}</div>}

      <footer className="composer">
        <input
          value={input}
          placeholder={runId ? '输入指令，回车发送…' : '先新建会话'}
          disabled={inputDisabled}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') send()
          }}
        />
        <button onClick={send} disabled={inputDisabled || !input.trim()}>
          发送
        </button>
      </footer>
    </div>
  )
}
