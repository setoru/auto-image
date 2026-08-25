// A 形态 —— Claude Code 会话的 Web 对话界面。
// header（run_id · 会话状态 · 当前阶段 · 时长 · 结束会话）+ 左侧可收起
// 任务详情栏（产物卡）+ 主区双 tab（会话 | 产物）
// + 底部常驻对话输入条。多会话并存时 header 出现切换下拉。
import { useEffect, useRef, useState } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import './App.css'
import * as store from './store.js'
import { fmtElapsed, firstPromptPreview, resumeMark, fmtSize } from './derive.js'
import ChatBar from './components/ChatBar.jsx'

const STATUS_LABEL = { RUNNING: '执行中', WAITING_INPUT: '等待指令', CANCELED: '已关闭', FAILED: '失败', ENDED: '已结束' }
// 下拉三态（执行中/挂起/已结束）：所有终态（含重启找回的 ENDED）归「已结束」
const TASK_STATUS_LABEL = { RUNNING: '执行中', WAITING_INPUT: '挂起' }
const STAGE_LABEL = { GUIDE: '生成指南', INSTALL: '远程安装', VERIFY: '只读验证', ARCHIVE: '打包归档' }
const STATUS_TONE = { RUNNING: 'running', FAILED: 'bad', CANCELED: 'warn', ENDED: 'warn' }

function EventRow({ ev }) {
  if (ev.type === 'stage.changed') {
    return <div className="va-stage-line">─ 进入 {STAGE_LABEL[ev.payload.stage] ?? ev.payload.stage} ─</div>
  }
  if (ev.type === 'user.message') {
    return <div className="va-user">你 ▸ {ev.payload.text}</div>
  }
  if (ev.type === 'turn.stopped') {
    return (
      <div className="va-paused">
        — 已停止（Esc 等效），等待指令 · 已提交的云操作不受停止影响，无法撤销 —
      </div>
    )
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
    return <div className="va-msg va-md" dangerouslySetInnerHTML={{ __html: mdToHtml(ev.payload.text) }} />
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
        <div className="va-md" dangerouslySetInnerHTML={{ __html: mdToHtml(ev.payload.result) }} />
      </div>
    )
  }
  if (ev.type === 'run.failed') {
    return <div className="va-failed">会话异常终止：{ev.payload.message}</div>
  }
  if (ev.type === 'run.canceled') {
    return <div className="va-canceled">— 会话已关闭 —</div>
  }
  if (ev.type === 'run.ended') {
    return <div className="va-canceled">— 历史会话（服务重启找回，只读）—</div>
  }
  return null
}

// 阶段徽标（产物卡分组与产物 tab 头共用）
function StageBadge({ stage }) {
  return <span className={`va-art-badge s-${stage.toLowerCase()}`}>{stage}</span>
}

// 产物卡：按阶段分组列出已解锁文件（带阶段徽标），点击在主区产物 tab 查看
function ArtifactCard({ run }) {
  const files = run?.artifacts?.files ?? []
  // 分组顺序取自 STAGE_LABEL 的键序（与流水线推进一致；清单由服务端按已进入阶段解锁）
  const groups = Object.keys(STAGE_LABEL)
    .map((stage) => ({ stage, items: files.filter((f) => f.stage === stage) }))
    .filter((g) => g.items.length > 0)
  return (
    <div className="va-card">
      <div className="va-card-title">产物 · {files.length}</div>
      {groups.length === 0 && <div className="va-card-line">等待阶段产物落盘…</div>}
      {groups.map(({ stage, items }) => (
        <div key={stage} className="va-art-group">
          <div className="va-art-stage">
            <StageBadge stage={stage} />
            {STAGE_LABEL[stage]}
          </div>
          {items.map((f) => (
            <button
              key={f.name}
              className={`va-art-item${run.artifact?.name === f.name ? ' on' : ''}`}
              onClick={() => store.openArtifact(run.runId, f.name)}
              title={f.name}
            >
              <span className="va-art-name">{f.name}</span>
              <span className="va-art-size">{fmtSize(f.size)}</span>
            </button>
          ))}
        </div>
      ))}
    </div>
  )
}

// markdown → 消毒后 HTML 的单点：产物与消息流共用（内容都系 agent 转述
// 外部文档/工具输出，同威胁模型，HTML 一律消毒再进 DOM）
const mdToHtml = (text) => DOMPurify.sanitize(marked.parse(text, { async: false }))

// 产物 tab：markdown 经 marked 渲染（表格/代码块/验证契约 blockquote），json 原文展示
function ArtifactView({ run }) {
  const artifact = run.artifact
  if (!artifact) {
    return <div className="artifact-empty">点击左侧产物卡中的文件查看（随阶段推进解锁）</div>
  }
  const isJson = artifact.name.endsWith('.json')
  const html = isJson ? '' : mdToHtml(artifact.content)
  return (
    <div className="va-artifact">
      <div className="va-artifact-head">
        <StageBadge stage={artifact.stage} />
        <span className="va-artifact-name">{artifact.name}</span>
        <span className="va-artifact-dir">{run.artifacts.outputDir ?? ''}</span>
      </div>
      {isJson ? (
        <pre className="va-artifact-raw">{artifact.content}</pre>
      ) : (
        <div className="va-artifact-md va-md" dangerouslySetInnerHTML={{ __html: html }} />
      )}
    </div>
  )
}

// 任务详情侧栏：产物卡（阶段与回合汇总常驻 header 与消息流，不重复设卡）
function TaskSide({ run, onClose }) {
  return (
    <aside className="va-side">
      <div className="va-side-head">
        <span className="va-side-title-text">任务详情</span>
        <button className="va-side-toggle" onClick={onClose} title="收起侧栏">«</button>
      </div>
      <div className="va-side-cards">
        <ArtifactCard run={run} />
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
  const artifactCount = run?.artifacts?.files?.length ?? 0

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

  // 侧栏点开产物后自动切到产物 tab
  useEffect(() => {
    if (run?.artifact) setTab('artifact')
  }, [run?.artifact?.name])

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
            title="切换查看会话（历史会话只读回放）"
          >
            {s.order.map((id) => (
              <option key={id} value={id}>
                {firstPromptPreview(s.runs[id])} · {id} · {TASK_STATUS_LABEL[s.runs[id].status] ?? '已结束'}
                {resumeMark(s.runs[id], s.runs)}
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
                <button
                  className={tab === 'artifact' ? 'on' : ''}
                  onClick={() => setTab('artifact')}
                  disabled={artifactCount === 0}
                >
                  产物 · {artifactCount}
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
                  <ArtifactView run={run} />
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
