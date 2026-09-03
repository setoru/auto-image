// PROTOTYPE — 布局手感验证专用，验证后整体删除，勿并入正式代码。
// 三个结构上真正不同的布局变体（冻结骨架的候选集）：
//   A 冻结案 —— 左侧栏（会话|产物 两面板）+ 侧栏右侧的混合 tab 栏
//   B 活动栏 —— 左 48px 图标栏 + 二级面板 + 混合 tab 栏（VSCode 同构）
//   C 顶混排 —— 侧栏同 A，但混合 tab 栏与 header 合成一行顶部横栏
// 共用 protoState 的状态机（tab 开合/激活语义在冻结案中定死，变体只换壳）。
import { useState } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import * as ps from './protoState.js'
import { fmtSize } from '../derive.js'

const STATUS_LABEL = { RUNNING: '执行中', READY: '等待指令', ENDED: '已结束' }
const STAGE_LABEL = { GUIDE: '生成指南', INSTALL: '远程安装', VERIFY: '只读验证', ARCHIVE: '打包归档', BUILD: 'RPM 构建' }
const md = (t) => DOMPurify.sanitize(marked.parse(t, { async: false }))

// ---------------------------------------------------------------- 共件

function StageBadge({ stage }) {
  return <span className={`va-art-badge s-${String(stage).toLowerCase()}`}>{stage}</span>
}

// 混合 tab 条（三变体同款，位置由变体摆）：会话 tab 带状态点 + 截断标题，
// 文件 tab 带图标 + 文件名（hover 全路径）；最后一枚会话 tab × 不渲染
function TabStrip() {
  const s = ps.useProto()
  const lastSessionKey = ps.tabKey(ps.sessionTabs().at(-1) ?? {})
  const running = Object.values(s.runs).filter((r) => r.status === 'RUNNING').length
  return (
    <div className="proto-tabstrip">
      {s.tabs.map((t) => {
        const key = ps.tabKey(t)
        const on = key === s.activeKey
        const run = t.kind === 'session' ? s.runs.find((r) => r.runId === t.runId) : null
        return (
          <div
            key={key}
            className={`proto-tab${on ? ' on' : ''}`}
            onClick={() => ps.activate(key)}
            title={t.kind === 'file' ? t.relPath : `${run?.title} · ${t.runId}`}
          >
            {run ? (
              <>
                <span className={`va-tab-dot ${ps.statusDot(run)}`} />
                <span className="proto-tab-name">{run.title}</span>
              </>
            ) : (
              <>
                <span className="proto-tab-ico">{t.entry?.binary ? '📦' : '📄'}</span>
                <span className="proto-tab-name">{t.name}</span>
              </>
            )}
            {(t.kind === 'file' || key !== lastSessionKey) && (
              <button
                className="va-tab-close"
                onClick={(e) => { e.stopPropagation(); ps.closeTab(key) }}
                title={t.kind === 'file' ? '关闭文件标签' : '关闭标签页（会话仍在列表）'}
              >×</button>
            )}
          </div>
        )
      })}
      <button className="va-tab-new" onClick={() => ps.createRun()} title="新建空会话">+</button>
      <span className="va-spacer" />
      {running > 0 && <span className="va-run-count">运行中 {running}</span>}
    </div>
  )
}

// 消息流（假事件 {who, text} 渲染）
function Stream({ run }) {
  return (
    <div className="va-stream proto-stream">
      {run.events.length === 0 && <div className="va-empty-hint">空会话——输入第一条部署指令。</div>}
      {run.events.map((ev, i) =>
        ev.who === 'user'
          ? <div key={i} className="va-user">{ev.text}</div>
          : <div key={i} className="va-msg va-md" dangerouslySetInnerHTML={{ __html: md(ev.text) }} />
      )}
    </div>
  )
}

// 产物文件内容（假清单 entry 自带 content）
function FileView({ tab }) {
  const e = tab.entry ?? {}
  if (e.binary) {
    return (
      <div className="va-artifact-binary">
        <div className="va-artifact-binary-icon">📦</div>
        <div className="va-artifact-binary-name">{e.name}</div>
        <div className="va-artifact-binary-size">二进制产物 · {fmtSize(e.size)}，不支持在线预览</div>
      </div>
    )
  }
  if (e.name?.endsWith('.json')) return <pre className="va-artifact-raw">{e.content}</pre>
  return <div className="va-artifact-md va-md" dangerouslySetInnerHTML={{ __html: md(e.content ?? '') }} />
}

// 编辑区：按激活 tab 渲染消息流或文件；无会话 tab 时的空态（冻结案）
function Editor() {
  const s = ps.useProto()
  const t = ps.activeTab()
  const lastRun = s.runs.find((r) => r.runId === ps.controlRunId())
  if (!t) {
    return (
      <div className="empty-state">
        <div className="big">未开始</div>
        <div>点「+ 新建」创建会话，或从左侧列表打开历史会话</div>
      </div>
    )
  }
  if (t.kind === 'file') return <FileView tab={t} />
  const run = s.runs.find((r) => r.runId === t.runId)
  return run ? <Stream run={run} /> : <Stream run={lastRun ?? { events: [] }} />
}

// 侧栏小 tab：会话 | 产物（冻结案：两小 tab，默认产物）
function SidePanelSwitch({ side, setSide }) {
  const s = ps.useProto()
  const fileCount = s.tree.reduce((n, r) => n + r.count, 0)
  return (
    <div className="proto-side-tabs">
      <button className={side === 'sessions' ? 'on' : ''} onClick={() => setSide('sessions')}>会话</button>
      <button className={side === 'artifacts' ? 'on' : ''} onClick={() => setSide('artifacts')}>产物 · {fileCount}</button>
    </div>
  )
}

// 会话列表（冻结案行内容）：状态点 + 标题 + 相对时间；二行状态/阶段
function SessionList() {
  const s = ps.useProto()
  const openIds = new Set(ps.sessionTabs().map((t) => t.runId))
  return (
    <div className="proto-session-list">
      {s.runs.map((run) => (
        <div
          key={run.runId}
          className={`proto-session-row${openIds.has(run.runId) ? ' opened' : ''}`}
          onClick={() => ps.openSession(run.runId)}
          title={`${run.title} · ${run.runId}`}
        >
          <span className={`va-tab-dot ${ps.statusDot(run)}`} />
          <span className="proto-session-main">
            <span className="proto-session-title">{run.title}</span>
            <span className="proto-session-sub">
              {run.status === ps.ENDED ? '已结束' : `${STATUS_LABEL[run.status]}${run.stage ? ' · ' + STAGE_LABEL[run.stage] : ''}`}
            </span>
          </span>
          <span className="proto-session-time">{ps.agoText(run.lastActivity)}</span>
        </div>
      ))}
    </div>
  )
}

// 产物目录树：可展开目录 + 文件行（已开 tab 弱标记、勾选、点开文件 tab）
function TreeDir({ node, openSet, setOpenSet }) {
  const s = ps.useProto()
  const open = openSet.has(node.path)
  const rels = ps.subtreeRels(node)
  const selCount = rels.filter((p) => s.sel[p]).length
  const allOn = rels.length > 0 && selCount === rels.length
  const some = selCount > 0 && !allOn
  return (
    <div className="va-art-group">
      <div className="va-art-dir-head" onClick={() => setOpenSet(new Set([...openSet, node.path].filter((p) => p !== node.path || open === false)))}>
        <span className="va-art-dir-arrow">{open ? '▾' : '▸'}</span>
        <input type="checkbox" className="va-art-check" checked={allOn} ref={(el) => el && (el.indeterminate = some)}
          onChange={() => ps.setSel(rels, !allOn)} onClick={(e) => e.stopPropagation()} />
        <span className="va-art-name">{node.name}</span>
        <span className="va-art-count">{node.count}</span>
      </div>
      {open && (
        <div className="va-art-children">
          {node.files.map((f) => {
            const rel = `${node.path}/${f.name}`
            const opened = s.tabs.some((t) => t.kind === 'file' && t.relPath === rel)
            return (
              <div key={f.name} className={`va-art-item${opened ? ' opened' : ''}`} onClick={() => ps.openFile(rel, f)} title={rel}>
                <input type="checkbox" className="va-art-check" checked={!!s.sel[rel]}
                  onChange={() => ps.toggleSel(rel)} onClick={(e) => e.stopPropagation()} />
                {f.stage ? <StageBadge stage={f.stage} /> : null}
                <span className="va-art-name">{f.name}</span>
                {opened && <span className="proto-opened-mark" title="已打开为标签">●</span>}
                <span className="va-art-size">{fmtSize(f.size)}</span>
              </div>
            )
          })}
          {node.dirs.map((d) => <TreeDir key={d.path} node={d} openSet={openSet} setOpenSet={setOpenSet} />)}
        </div>
      )}
    </div>
  )
}

function ArtifactTree() {
  const s = ps.useProto()
  // 默认展开最新组（根 deploy 即最新），手动态覆盖
  const [manual, setManual] = useState(null)
  const openSet = manual ?? new Set(['deploy', 'deploy/httpd'])
  const rels = s.tree.flatMap(ps.subtreeRels)
  const selCount = rels.filter((p) => s.sel[p]).length
  return (
    <div className="proto-art-panel">
      <div className="va-art-tools">
        <button onClick={() => ps.setSel(rels, true)}>全选</button>
        <button onClick={() => ps.setSel(rels, false)} disabled={selCount === 0}>清空</button>
        <button className="va-art-zip" disabled={selCount === 0} title="（原型）打包下载">{selCount ? `下载 zip (${selCount})` : '下载 zip'}</button>
      </div>
      {s.tree.map((r) => <TreeDir key={r.path} node={r} openSet={openSet} setOpenSet={setManual} />)}
    </div>
  )
}

// header：绑定最后激活的会话 tab（冻结案 Q6），激活文件 tab 不改它
function Head({ wide }) {
  const s = ps.useProto()
  const run = s.runs.find((r) => r.runId === ps.controlRunId())
  if (!run) return <header className={`va-head${wide ? ' proto-head-wide' : ''}`}><span className="va-runid">auto-image 部署会话</span></header>
  return (
    <header className={`va-head${wide ? ' proto-head-wide' : ''}`}>
      <span className="va-runid">{run.runId}</span>
      <span className={`dot ${run.status === 'RUNNING' ? 'tone-running' : run.status === 'ENDED' ? 'tone-warn' : 'tone-ok'}`} />
      <span>{STATUS_LABEL[run.status]}</span>
      <span className="va-spacer" />
      <span className="va-stage">{run.stage ? STAGE_LABEL[run.stage] : null}</span>
      <span className="va-elapsed">总计时间 {String(Math.floor(run.elapsed / 60)).padStart(2, '0')}:{String(run.elapsed % 60).padStart(2, '0')}</span>
      <button disabled={run.status === ps.ENDED} onClick={() => { /* 原型：无后端动作 */ }}>结束会话</button>
    </header>
  )
}

// 底部输入条（冻结案 Q6）：跟最后激活会话 tab，草稿随 tab 独立保留
function ChatBar() {
  const s = ps.useProto()
  const runId = ps.controlRunId()
  const run = s.runs.find((r) => r.runId === runId)
  return (
    <div className="chat-bar">
      <button className="chat-continue" disabled={!run || run.status === 'RUNNING'} title="从当前会话分叉（原型：无动作）">⑂ 克隆</button>
      <input
        disabled={!run || run.status !== ps.READY}
        value={s.drafts[runId] ?? ''}
        placeholder={run?.status === ps.ENDED ? '会话已结束——克隆后继续对话' : '输入部署指令：软件 + 文档链接 + 目标机器…'}
        onChange={(e) => ps.setDrafts({ ...s.drafts, [runId]: e.target.value })}
      />
      <button className="chat-send" disabled={run?.status !== ps.READY}>{run?.status === 'RUNNING' ? '■ 停止' : '发送 ⏎'}</button>
    </div>
  )
}

// ---------------------------------------------------------------- 变体 A
// 冻结案：header 全宽 → 左侧栏（小 tab：会话|产物）+ 右侧混合 tab 栏（只压
// 主区）→ 编辑区 → 输入条全宽。骨架按 Q1 方案 A 原样落地。
export function VariantA() {
  const [side, setSide] = useState('artifacts')
  const [sideOpen, setSideOpen] = useState(true)
  return (
    <div className="va-root">
      <Head wide />
      <div className="va-body">
        {sideOpen && (
          <aside className="va-side proto-side">
            <SidePanelSwitch side={side} setSide={setSide} />
            <div className="proto-side-body">
              {side === 'sessions' ? <SessionList /> : <ArtifactTree />}
            </div>
          </aside>
        )}
        <button
          className={`va-side-pin${sideOpen ? ' open' : ''}`}
          onClick={() => setSideOpen(!sideOpen)}
          title={sideOpen ? '收起侧栏' : '展开侧栏'}
        ><span className="va-btn-sym" aria-hidden="true">{sideOpen ? '«' : '»'}</span></button>
        <div className="va-main">
          <TabStrip />
          <div className="va-tab-body"><Editor /></div>
        </div>
      </div>
      <ChatBar />
    </div>
  )
}

// ---------------------------------------------------------------- 变体 B
// 活动栏（Q1 被否的方案 B 复活参战）：48px 图标栏（会话/产物/设置）+ 二级
// 面板 + 混合 tab 栏。与 A 的区别：侧栏两面板由活动栏二选一独占（不能同屏
// 切换），骨架更 VSCode，代价是左列更宽、点击距离多一跳。
export function VariantB() {
  const [act, setAct] = useState('artifacts') // sessions | artifacts
  return (
    <div className="va-root">
      <Head wide />
      <div className="va-body proto-b-body">
        <nav className="proto-activity">
          <button className={act === 'sessions' ? 'on' : ''} onClick={() => setAct('sessions')} title="会话列表">💬</button>
          <button className={act === 'artifacts' ? 'on' : ''} onClick={() => setAct('artifacts')} title="产物">📦</button>
          <span className="va-spacer" />
          <button title="设置（占位）">⚙</button>
        </nav>
        <aside className="proto-b-panel">
          <div className="proto-b-panel-title">{act === 'sessions' ? '会话' : '产物'}</div>
          <div className="proto-side-body">
            {act === 'sessions' ? <SessionList /> : <ArtifactTree />}
          </div>
        </aside>
        <div className="va-main">
          <TabStrip />
          <div className="va-tab-body"><Editor /></div>
        </div>
      </div>
      <ChatBar />
    </div>
  )
}

// ---------------------------------------------------------------- 变体 C
// 顶混排：侧栏同 A，但混合 tab 栏上移与 header 合成一行（header 左段 +
// tab 栏右段）。tab 栏获得全宽呼吸空间、省一行竖向空间；代价是 header 里
// 的会话元信息被 tab 栏挤压，需精简。检验「tab 栏只压主区」是否真必要。
export function VariantC() {
  const [side, setSide] = useState('artifacts')
  return (
    <div className="va-root">
      <div className="proto-c-toprow">
        <Head />
        <div className="proto-c-tabs"><TabStrip /></div>
      </div>
      <div className="va-body">
        <aside className="va-side proto-side">
          <SidePanelSwitch side={side} setSide={setSide} />
          <div className="proto-side-body">
            {side === 'sessions' ? <SessionList /> : <ArtifactTree />}
          </div>
        </aside>
        <div className="va-main">
          <div className="va-tab-body"><Editor /></div>
        </div>
      </div>
      <ChatBar />
    </div>
  )
}

export const VARIANTS = [
  { key: 'A', name: '冻结案 — 侧栏 + 侧栏右混合 tab 栏', Comp: VariantA },
  { key: 'B', name: '活动栏 — 48px 图标栏 + 独占面板', Comp: VariantB },
  { key: 'C', name: '顶混排 — tab 栏与 header 同行', Comp: VariantC },
]
