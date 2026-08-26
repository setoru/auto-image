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

// 回合汇总与最后一条 agent 消息同文时降级为轻量状态线：正常完成的回合
// result 就是最后一条 assistant 文本（CLI Result 语义），重复成框是噪音；
// 异常收尾（无最终文本 / 被停止 / 失败摘要）才保留汇总框。
// 线上不断言「可继续」——活会话输入条已表达，历史回放里则与只读语义相悖
function turnCompletedRow(ev, prev) {
  const dup =
    prev?.type === 'agent.message' &&
    String(prev.payload.text ?? '').trim() === String(ev.payload.result ?? '').trim() &&
    ev.payload.result?.trim()
  if (dup) return <div className="va-stage-line">─ 回合完成 ─</div>
  return (
    <div className="va-result">
      <div className="va-result-title">回合汇总（turn.completed，会话可继续）</div>
      <div className="va-md" dangerouslySetInnerHTML={{ __html: mdToHtml(ev.payload.result) }} />
    </div>
  )
}

// 工具事件索引：started/finished 按 id 关联（旧事件无 id 时不入索引，
// 各自独立成行兜底）
function toolIndex(events) {
  const startedById = new Map()
  const finishedIds = new Set()
  for (const ev of events) {
    if (!ev.payload?.id) continue
    if (ev.type === 'agent.tool_started') startedById.set(ev.payload.id, ev)
    if (ev.type === 'agent.tool_finished') finishedIds.add(ev.payload.id)
  }
  return { startedById, finishedIds }
}

// 工具展开体的一块：小标题 + 浅底圆角内容块，输入/输出各一块
function IoBlock({ title, text }) {
  return (
    <div className="va-tool-io">
      <div className="va-tool-io-title">{title}</div>
      <pre className="va-tool-io-text">{text}</pre>
    </div>
  )
}

// Edit/Write 的输入块：diff 文本按行首染色（+ 绿 / - 红 / 头部灰）
function IoDiff({ text }) {
  return (
    <div className="va-tool-io">
      <div className="va-tool-io-title">diff</div>
      <pre className="va-tool-io-text va-diff">
        {text.split('\n').map((line, i) => {
          const cls = line.startsWith('+++') || line.startsWith('---')
            ? 'meta'
            : line.startsWith('+')
              ? 'add'
              : line.startsWith('-')
                ? 'del'
                : 'ctx'
          return <span key={i} className={cls}>{line}{'\n'}</span>
        })}
      </pre>
    </div>
  )
}

// TodoWrite 的输入块：checkbox 列表（☑ 完成 / ◐ 进行中 / ☐ 待办）
function IoTodos({ todos }) {
  const MARK = { completed: '☑', in_progress: '◐', pending: '☐' }
  return (
    <div className="va-tool-io">
      <div className="va-tool-io-title">todos</div>
      <div className="va-todos">
        {todos.map((t, i) => (
          <div key={i} className={`va-todo ${t.status}`}>{MARK[t.status] ?? '☐'} {t.content}</div>
        ))}
      </div>
    </div>
  )
}

function EventRow({ ev, prev, tools }) {
  if (ev.type === 'resumed.history') {
    return <div className="va-stage-line">─ 已接续 {ev.payload.resumed_from} · 以下为带入的历史 ─</div>
  }
  if (ev.type === 'stage.changed') {
    return <div className="va-stage-line">─ 进入 {STAGE_LABEL[ev.payload.stage] ?? ev.payload.stage} ─</div>
  }
  if (ev.type === 'user.message') {
    return <div className="va-user">{ev.payload.text}</div>
  }
  if (ev.type === 'turn.stopped') {
    return (
      <div className="va-paused">
        — 已停止（Esc 等效），等待指令 · 已提交的云操作不受停止影响，无法撤销 —
      </div>
    )
  }
  if (ev.type === 'run.interrupted') {
    return (
      <div className="va-paused">
        — 服务重启，上一回合被中断 · 已提交的云操作不受影响，无法撤销 —
      </div>
    )
  }
  if (ev.type === 'agent.thinking') {
    return (
      <details className="va-thinking">
        <summary>Thinking &gt;</summary>
        <div className="va-thinking-body">{ev.payload.text}</div>
      </details>
    )
  }
  if (ev.type === 'agent.message') {
    return <div className="va-msg va-md" dangerouslySetInnerHTML={{ __html: mdToHtml(ev.payload.text) }} />
  }
  if (ev.type === 'agent.tool_started' || ev.type === 'agent.tool_finished') {
    // 同 id 已有 finished：行移到 finished 位置渲染成 ✓，此处跳过
    if (ev.type === 'agent.tool_started' && ev.payload.id && tools.finishedIds.has(ev.payload.id)) {
      return null
    }
    const started = ev.type === 'agent.tool_finished' && ev.payload.id
      ? tools.startedById.get(ev.payload.id)
      : null
    const head = started?.payload ?? ev.payload // 行显示入参主参数；旧事件兜底自身摘要
    const input = head.detail ?? head.summary // 输入全文（k: v 行），折叠行截断的完整版
    const output = ev.type === 'agent.tool_finished' // 完成后附结果全文
      ? ev.payload.detail ?? ev.payload.summary
      : null
    const mark = ev.type === 'agent.tool_started' ? '▶' : '✓'
    return (
      <details className="va-tool">
        <summary>{mark} <b>{head.tool}</b>({head.summary})</summary>
        <div className="va-tool-detail">
          {head.diff ? <IoDiff text={head.diff} /> : head.todos ? <IoTodos todos={head.todos} /> : <IoBlock title="输入" text={input} />}
          {output !== null && <IoBlock title="输出" text={output} />}
        </div>
      </details>
    )
  }
  if (ev.type === 'turn.completed') {
    return turnCompletedRow(ev, prev)
  }
  if (ev.type === 'run.failed') {
    return <div className="va-failed">会话异常终止：{ev.payload.message}</div>
  }
  if (ev.type === 'run.canceled') {
    return <div className="va-canceled">— 会话已关闭 —</div>
  }
  if (ev.type === 'run.ended') {
    return <div className="va-canceled">— 历史会话回放完毕（重启找回，只读）—</div>
  }
  return null
}

// 阶段徽标（产物文件行与产物 tab 头共用）
function StageBadge({ stage }) {
  return <span className={`va-art-badge s-${stage.toLowerCase()}`}>{stage}</span>
}

// 产物卡：deploy/ 全量镜像，按目录分组（组头带文件数，点击展开/收起），
// 点击文件在主区产物 tab 查看。约定命名的带阶段徽标，非约定的（.v1 备份、
// 杂项）无徽标平铺；目录按最新落盘时间降序（服务端排好）
function ArtifactCard() {
  const s = store.useRunState()
  const groups = s.artifacts.groups
  const [toggles, setToggles] = useState({})
  const fileCount = groups.reduce((n, g) => n + g.files.length, 0)
  return (
    <div className="va-card">
      <div className="va-card-title">产物 · {fileCount}</div>
      {groups.length === 0 && <div className="va-card-line">deploy/ 下暂无产物</div>}
      {groups.map((g, i) => {
        const open = toggles[g.dir] ?? i === 0 // 未动过的目录默认展开最新一组
        return (
          <div key={g.dir} className="va-art-group">
            <button
              className="va-art-dir-head"
              onClick={() => setToggles({ ...toggles, [g.dir]: !open })}
              title={g.dir}
            >
              <span className="va-art-dir-arrow">{open ? '▾' : '▸'}</span>
              <span className="va-art-name">{g.dir || '(根目录)'}</span>
              <span className="va-art-count">{g.files.length}</span>
            </button>
            {open &&
              g.files.map((f) => (
                <button
                  key={f.name}
                  className={`va-art-item${s.artifact?.dir === g.dir && s.artifact?.name === f.name ? ' on' : ''}`}
                  onClick={() => store.openArtifact(g.dir ? `${g.dir}/${f.name}` : f.name)}
                  title={f.name}
                >
                  {f.stage ? <StageBadge stage={f.stage} /> : null}
                  <span className="va-art-name">{f.name}</span>
                  <span className="va-art-size">{fmtSize(f.size)}</span>
                </button>
              ))}
          </div>
        )
      })}
    </div>
  )
}

// markdown → 消毒后 HTML 的单点：产物与消息流共用（内容都系 agent 转述
// 外部文档/工具输出，同威胁模型，HTML 一律消毒再进 DOM）
const mdToHtml = (text) => DOMPurify.sanitize(marked.parse(text, { async: false }))

// 产物 tab：markdown 经 marked 渲染（表格/代码块/验证契约 blockquote），json 原文展示
function ArtifactView() {
  const artifact = store.useRunState().artifact
  if (!artifact) {
    return <div className="artifact-empty">点击左侧产物卡中的文件查看</div>
  }
  const isJson = artifact.name.endsWith('.json')
  const html = isJson ? '' : mdToHtml(artifact.content)
  return (
    <div className="va-artifact">
      <div className="va-artifact-head">
        {artifact.stage && <StageBadge stage={artifact.stage} />}
        <span className="va-artifact-name">{artifact.name}</span>
        <span className="va-artifact-dir">{artifact.dir}</span>
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
        <ArtifactCard />
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
  const artifactCount = s.artifacts.groups.reduce((n, g) => n + g.files.length, 0)
  const tools = run ? toolIndex(run.events) : { startedById: new Map(), finishedIds: new Set() }

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
    if (s.artifact) setTab('artifact')
  }, [s.artifact?.dir, s.artifact?.name])

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
                    {run.events.map((ev, i) => (
                      <EventRow key={ev.seq} ev={ev} prev={run.events[i - 1]} tools={tools} />
                    ))}
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
                  <ArtifactView />
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
