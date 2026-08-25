// A 形态 —— Claude Code 会话的 Web 对话界面。
// header（run_id · 会话状态 · 当前阶段 · 时长 · 结束会话）+ 左侧可收起
// 任务详情栏（产物卡 / 阶段卡 / 回合汇总卡）+ 主区双 tab（会话 | 产物）
// + 底部常驻对话输入条。多会话并存时 header 出现切换下拉。
import { useEffect, useRef, useState } from 'react'
import './App.css'
import * as store from './store.js'
import { fmtElapsed, firstPromptPreview } from './derive.js'
import ChatBar from './components/ChatBar.jsx'

const STATUS_LABEL = { RUNNING: '执行中', WAITING_INPUT: '等待指令', CANCELED: '已结束', FAILED: '失败' }
const STAGE_LABEL = { GUIDE: '生成指南', INSTALL: '远程安装', VERIFY: '只读验证', ARCHIVE: '打包归档' }
const STATUS_TONE = { RUNNING: 'running', FAILED: 'bad', CANCELED: 'warn' }

function EventRow({ ev }) {
  if (ev.type === 'stage.changed') {
    return <div className="va-stage-line">─ 进入 {STAGE_LABEL[ev.payload.stage] ?? ev.payload.stage} ─</div>
  }
  if (ev.type === 'user.message') {
    return <div className="va-user">你 ▸ {ev.payload.text}</div>
  }
  if (ev.type === 'turn.stopped') {
    return <div className="va-paused">— 已停止（Esc 等效），等待指令 —</div>
  }
  if (ev.type === 'agent.thinking') {
    return (
      <details className="va-thinking">
        <summary>💭 Thinking</summary>
        <div className="va-thinking-body">{ev.payload.text}</div>
      </details>
    )
  }
  if (ev.type === 'agent.message') {
    return <div className="va-msg">{ev.payload.text}</div>
  }
  if (ev.type === 'agent.tool_started') {
    return <div className="va-tool">▶ {ev.payload.tool} · {ev.payload.summary}</div>
  }
  if (ev.type === 'agent.tool_finished') {
    return <div className="va-tool">✔ {ev.payload.tool} · {ev.payload.summary}</div>
  }
  if (ev.type === 'turn.completed') {
    return (
      <div className="va-result">
        <div className="va-result-title">回合汇总（turn.completed，会话可继续）</div>
        <pre>{ev.payload.result}</pre>
      </div>
    )
  }
  if (ev.type === 'run.failed') {
    return <div className="va-failed">会话异常终止：{ev.payload.message}</div>
  }
  if (ev.type === 'run.canceled') {
    return <div className="va-canceled">— 会话已关闭 —</div>
  }
  return null
}

// 任务详情侧栏：产物卡（产物端点接入前列出占位）+ 阶段卡 + 回合汇总卡
function TaskSide({ run, onClose }) {
  const lastAgentMsg = run
    ? [...run.events].reverse().find((e) => e.type === 'agent.message')?.payload.text ?? ''
    : ''
  return (
    <aside className="va-side">
      <div className="va-side-head">
        <span className="va-side-title-text">任务详情</span>
        <button className="va-side-toggle" onClick={onClose} title="收起侧栏">«</button>
      </div>
      <div className="va-side-cards">
        <div className="va-card">
          <div className="va-card-title">产物 · 0</div>
          <div className="va-card-line">等待阶段产物落盘…</div>
        </div>

        {run && (
          <div className="va-card">
            <div className="va-card-title">{run.result ? '最后阶段' : '当前阶段'}</div>
            <div className="va-card-big">{run.stage ? STAGE_LABEL[run.stage] ?? run.stage : '—'}</div>
            <div className="va-card-line">run：{run.runId}</div>
            {lastAgentMsg && <div className="va-card-line va-card-msg">{lastAgentMsg}</div>}
          </div>
        )}

        {run?.result && (
          <div className="va-card va-card-result">
            <div className="va-card-title">回合汇总（会话可继续）</div>
            <pre>{run.result}</pre>
          </div>
        )}
        {run?.status === 'CANCELED' && (
          <div className="va-card"><div className="va-card-title">会话已关闭</div>云上已提交的操作不受影响</div>
        )}
      </div>
    </aside>
  )
}

export default function App() {
  const s = store.useRunState()
  const run = store.useViewRun()
  const scrollRef = useRef(null)
  const [follow, setFollow] = useState(true)
  const [sideOpen, setSideOpen] = useState(true)
  const [tab, setTab] = useState('chat') // chat | artifact

  const scrollToBottom = () => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }

  useEffect(() => {
    setTab('chat')
    setFollow(true)
  }, [s.viewRunId])

  useEffect(() => {
    if (follow) scrollToBottom()
  }, [run?.events.length, follow])

  // 切回会话 tab（含从产物查看返回）时滚到最新
  useEffect(() => {
    if (tab === 'chat') scrollToBottom()
  }, [tab, s.viewRunId])

  const onScroll = () => {
    const el = scrollRef.current
    setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 40)
  }

  return (
    <div className="va-root">
      <header className="va-head">
        {s.order.length > 1 && (
          <select
            className="va-task-select"
            value={s.viewRunId ?? ''}
            onChange={(e) => store.selectRun(e.target.value)}
            title="切换查看会话"
          >
            {s.order.map((id) => (
              <option key={id} value={id}>
                {firstPromptPreview(s.runs[id])} · {id} · {STATUS_LABEL[s.runs[id].status]}
              </option>
            ))}
          </select>
        )}
        {run && !sideOpen && (
          <button className="va-side-open" onClick={() => setSideOpen(true)} title="展开任务详情栏">
            » 详情
          </button>
        )}
        {run ? (
          <>
            <span className="va-runid">{run.runId}</span>
            <span className={`dot tone-${STATUS_TONE[run.status] ?? 'ok'}`} />
            <span>{STATUS_LABEL[run.status]}</span>
            {run.connection === 'reconnecting' && <span className="va-conn">连接断开，重连中（Last-Event-ID 续传）…</span>}
            <span className="va-spacer" />
            <span className="va-stage">{run.stage ? STAGE_LABEL[run.stage] ?? run.stage : '—'}</span>
            <span className="va-elapsed">{fmtElapsed(run.startedAt, run.endedAt ?? s.now)}</span>
            <button onClick={() => store.cancel()} disabled={!store.isActive(run.status)}>
              结束会话
            </button>
          </>
        ) : (
          <span className="va-runid">auto-image 部署会话</span>
        )}
      </header>

      <div className="va-body">
        {sideOpen && <TaskSide run={run} onClose={() => setSideOpen(false)} />}
        <div className="va-main">
          {!run ? (
            <div className="empty-state">
              <div className="big">未开始</div>
              <div>点底部「+ 新建」创建会话，输入第一条部署指令</div>
            </div>
          ) : (
            <>
              <div className="va-tabs">
                <button className={tab === 'chat' ? 'on' : ''} onClick={() => setTab('chat')}>会话</button>
                <button className={tab === 'artifact' ? 'on' : ''} onClick={() => setTab('artifact')} disabled>
                  产物 · 0
                </button>
              </div>
              <div className="va-tab-body">
                {tab === 'chat' ? (
                  <div className="va-stream" ref={scrollRef} onScroll={onScroll}>
                    {run.events.length === 0 && (
                      <div className="va-empty-hint">空会话——输入第一条部署指令（软件 + 文档链接 + 目标机器）。</div>
                    )}
                    {run.events.map((ev) => <EventRow key={ev.seq} ev={ev} />)}
                    {!follow && (
                      <button
                        className="va-jump"
                        onClick={() => {
                          setFollow(true)
                          scrollToBottom()
                        }}
                      >
                        ↓ 回到最新
                      </button>
                    )}
                  </div>
                ) : (
                  <div className="artifact-empty">产物查看随产物端点接入</div>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {s.submitError && <div className="error-bar">{s.submitError}</div>}

      <ChatBar />
    </div>
  )
}
