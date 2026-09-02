// 共享会话状态：多会话并行（并发上限内的执行中回合可多个），动作经
// HTTP/SSE 与服务端交互。每个活跃会话一条常驻 EventSource（断线浏览器
// 自动重连并携带 Last-Event-ID，服务端从 seq+1 补发；已收事件按 seq 去重）。
//
// 停止 / 结束 / 克隆的干预端点见后端 web/（run_turn 按 stop_requested 标记
// 区分 turn.stopped 与 turn.completed）；失败时如实把错误显示在提示条上。
import { useSyncExternalStore } from 'react'

// 与服务端内部事件协议一致的事件类型全集（四族：session.* / turn.* /
// user.message / agent.* / stage.*）
export const EVENT_TYPES = [
  'session.started',
  'session.title_changed',
  'session.ended',
  'user.message',
  'agent.thinking',
  'agent.message',
  'agent.tool_started',
  'agent.tool_finished',
  'stage.changed',
  'turn.started',
  'turn.stopped',
  'turn.completed',
  'turn.failed',
  'turn.interrupted',
]

// 会触发产物清单刷新的事件：阶段推进（新产物落盘）与回合/会话收尾。
// 清单是 deploy/ + rpm/ 全量镜像（与查看中的会话无关），任一 run 触发都全局刷新
const REFRESH_EVENT_TYPES = ['stage.changed', 'turn.completed', 'turn.stopped', 'turn.failed', 'session.ended']

const RUNNING = 'RUNNING'
const READY = 'READY'
// 可继续操作的会话状态集合：判定值与服务端状态机一致，单处维护
const OPERABLE = [RUNNING, READY]
export const isOperable = (status) => OPERABLE.includes(status)

// 409 detail 判定值 → 人话提示（判定值与服务端 runs.Conflict.detail 一致，单处维护）
const CONFLICT_HINT = {
  turn_in_progress: '本会话回合执行中，想改方向先点「停止」',
  session_running: '源会话正在执行，回合结束后才能克隆',
  parallel_limit_reached: '执行中回合已达并发上限，稍后再发',
  session_not_active: '会话已结束，不可再操作（可克隆后继续）',
}

const listeners = new Set()
// order 即任务下拉次序：最新在前（服务端列表同序，新建前插）
let state = {
  runs: {},
  order: [],
  viewRunId: null,
  submitError: null,
  now: Date.now(),
  artifacts: { groups: [] }, // deploy/ + rpm/ 全量产物（目录分组，全局不属于任何 run）
  artifact: null,            // 当前查看中的产物内容（单槽，点击整体替换）
  artifactSel: {},           // 批量下载勾选集（relPath → true，随清单刷新剪枝）
  artifactZipping: false,    // zip 打包请求进行中（按钮防重复触发）
}

// 时长走针仅在查看中的会话执行期间（挂起与终态冻结，终态另有 endedAt 兜底）
setInterval(() => {
  if (state.runs[state.viewRunId]?.status === RUNNING) set({ now: Date.now() })
}, 1000)

function set(patch) {
  state = { ...state, ...patch }
  listeners.forEach((l) => l())
}

function setRun(runId, patch) {
  const run = state.runs[runId]
  if (!run) return
  state = { ...state, runs: { ...state.runs, [runId]: { ...run, ...patch } } }
  listeners.forEach((l) => l())
}

export function subscribe(l) {
  listeners.add(l)
  return () => listeners.delete(l)
}
export const getState = () => state
export function useRunState() {
  return useSyncExternalStore(subscribe, getState)
}

// 查看中的会话（header / 输入条 / 消息流都以它为对象）
export function useViewRun() {
  return useRunState().runs[state.viewRunId] ?? null
}

async function postJson(url, body) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    const err = new Error(data.detail || `HTTP ${resp.status}`)
    err.status = resp.status
    err.detail = data.detail
    throw err
  }
  return data
}

let errorTimer = null
function fail(message) {
  clearTimeout(errorTimer)
  set({ submitError: message })
  errorTimer = setTimeout(() => state.submitError && set({ submitError: null }), 4000)
}

// 409 家族提示条文案：命中判定值给人话提示，其余如实透传服务端 detail
const conflictText = (detail) => `409 — ${detail}：${CONFLICT_HINT[detail]}`
function conflictMessage(err) {
  return err.status === 409 && CONFLICT_HINT[err.detail]
    ? conflictText(err.detail)
    : err.detail || err.message
}

// ---------- SSE ----------

function onStreamEvent(runId, es, e) {
  const event = { seq: Number(e.lastEventId), type: e.type, payload: JSON.parse(e.data) }
  appendTo(runId, event)
  // 阶段推进与收尾都可能带来新落盘的产物，触发清单刷新
  if (REFRESH_EVENT_TYPES.includes(event.type)) refreshArtifacts()
  // session.ended 后服务端会正常结束流，主动 close 避免 EventSource 无限重连
  // （唯一会话终态事件：显式结束与重启找回的历史收尾）
  if (event.type === 'session.ended') es.close()
}

function attachStream(runId) {
  const es = new EventSource(`/api/runs/${runId}/events`)
  es.onopen = () => setRun(runId, { connection: 'live' })
  es.onerror = () => {
    if (es.readyState !== EventSource.CLOSED) setRun(runId, { connection: 'reconnecting' })
  }
  for (const type of EVENT_TYPES) es.addEventListener(type, (e) => onStreamEvent(runId, es, e))
  setRun(runId, { es })
}

// SSE 断线重连后服务端会全量重放，按 seq 去重；状态随事件类型同步推进
// （重放的 stage.changed / 收尾事件会重复触发清单刷新，幂等无害）。
// 单向推进：历史回放中的回合事件不把 ENDED 会话拉回可操作态。
function appendTo(runId, event) {
  const run = state.runs[runId]
  if (!run || run.events.some((ev) => ev.seq === event.seq)) return
  const patch = { events: [...run.events, event] }
  // 最后活动时刻以服务端事件 ts 为准（刷新/SSE 重放后不漂移）
  if (event.payload.ts) patch.lastEventAt = event.payload.ts * 1000
  if (event.type === 'stage.changed') patch.stage = event.payload.stage
  if (event.type === 'session.title_changed') patch.title = event.payload.title
  if (event.type === 'turn.completed') patch.result = event.payload.result
  if (isOperable(run.status) || event.type === 'session.started') {
    if (event.type === 'turn.started') patch.status = RUNNING
    // 回合完成/被停止/失败 ≠ 会话结束：一律回 READY
    if (event.type === 'turn.completed' || event.type === 'turn.stopped' || event.type === 'turn.failed') {
      patch.status = READY
    }
    if (event.type === 'turn.interrupted') patch.status = READY
    if (event.type === 'session.ended') {
      patch.status = 'ENDED'
      patch.endedAt = event.payload.ts ? event.payload.ts * 1000 : Date.now()
    }
  }
  setRun(runId, patch)
}

// ---------- HTTP ----------

// run 对象的唯一构造点：服务端摘要（loadRuns）与新建响应（createRun）
// 共用同一形状，字段差异由 overrides 给出
function makeRun(overrides) {
  return {
    runId: null,
    status: null,
    stage: null,
    firstPrompt: null,
    title: null,
    resumedFrom: null,
    events: [],
    result: null,
    connection: 'idle',
    startedAt: null,
    endedAt: null,
    lastEventAt: null,
    es: null,
    ...overrides,
  }
}

// 启动加载：拉全量 run 摘要恢复任务下拉（服务重启后经 transcript 重放，
// 全部可续聊、ENDED 只读回看）；首屏即回放查看中的那条。尽力而为，失败从空开始。
// ENDED 会话重放完自动关流（session.ended），READY/RUNNING 常驻等待续聊。
export async function loadRuns() {
  try {
    const resp = await fetch('/api/runs')
    if (!resp.ok) return
    const { runs } = await resp.json()
    if (!runs?.length) return
    const map = {}
    const order = []
    for (const s of runs) {
      map[s.run_id] = makeRun({
        runId: s.run_id,
        status: s.status,
        stage: s.stage,
        firstPrompt: s.first_prompt,
        title: s.title ?? null,
        resumedFrom: s.resumed_from,
        startedAt: s.started_at * 1000,
        endedAt: s.ended_at ? s.ended_at * 1000 : null,
        lastEventAt: s.last_event_at ? s.last_event_at * 1000 : null,
      })
      order.push(s.run_id)
    }
    set({
      runs: { ...state.runs, ...map },
      order: [...order, ...state.order],
      viewRunId: state.viewRunId ?? order[0],
    })
    attachStream(state.viewRunId)
  } catch {
    // 历史加载失败不打断使用：界面从空会话开始
  }
}

// 新建 = 一步创建空会话（READY），无中间表单；新建不受其他会话执行影响
export async function createRun() {
  try {
    const data = await postJson('/api/runs', {})
    const run = makeRun({
      runId: data.run_id,
      status: data.status,
      resumedFrom: data.resumed_from ?? null,
      connection: 'live',
      startedAt: Date.now(),
    })
    set({
      runs: { ...state.runs, [run.runId]: run },
      order: [run.runId, ...state.order],
      viewRunId: run.runId,
      submitError: null,
    })
    attachStream(run.runId)
  } catch (err) {
    fail(`新建会话失败：${conflictMessage(err)}`)
  }
}

// 克隆 = 从查看中的会话（READY/ENDED）分叉新会话：事件流转录、标题继承
export async function cloneRun() {
  const src = state.runs[state.viewRunId]
  if (!src) return
  try {
    const data = await postJson(`/api/runs/${src.runId}/clone`, {})
    const run = makeRun({
      runId: data.run_id,
      status: data.status,
      resumedFrom: data.resumed_from ?? null,
      connection: 'live',
      startedAt: Date.now(),
    })
    set({
      runs: { ...state.runs, [run.runId]: run },
      order: [run.runId, ...state.order],
      viewRunId: run.runId,
      submitError: null,
    })
    attachStream(run.runId)
  } catch (err) {
    fail(`克隆失败：${conflictMessage(err)}`)
  }
}

// 停止 = CLI 的 Esc：打断查看中会话的当前回合（只作用当前会话，不误停别人）
export async function stop() {
  const run = state.runs[state.viewRunId]
  if (!run || run.status !== RUNNING) return
  try {
    await postJson(`/api/runs/${run.runId}/stop`, {})
  } catch (err) {
    fail(`停止失败：${err.message}`)
  }
}

// 向查看中的会话发指令：执行中发送由服务端 409（turn_in_progress）拒绝，
// 想改方向先显式停止。返回是否投递成功（失败时输入由调用方保留）。
export async function send(text) {
  const trimmed = (text ?? '').trim()
  const run = state.runs[state.viewRunId]
  if (!run || !trimmed) return false
  if (!isOperable(run.status)) {
    // 只读会话（已结束）不静默吞掉输入，给出出路提示
    fail('该会话只读（已结束）——「+ 新建」或克隆该会话后继续')
    return false
  }
  try {
    const data = await postJson(`/api/runs/${run.runId}/messages`, { text: trimmed })
    setRun(run.runId, { status: data.status })
    return true
  } catch (err) {
    fail(`发送失败：${conflictMessage(err)}`)
    return false
  }
}

// 结束查看中的会话（显式、不可逆；执行中或挂起均可）
export async function endRun() {
  const run = state.runs[state.viewRunId]
  if (!run || !isOperable(run.status)) return
  try {
    await postJson(`/api/runs/${run.runId}/end`, {})
  } catch (err) {
    fail(`结束会话失败：${err.message}`)
  }
}

export function selectRun(runId) {
  const run = state.runs[runId]
  if (!run) return
  set({ viewRunId: runId })
  // 切换到的会话尚无事件流（列表加载来的历史）：接上即回放（终态重放完自动关流）
  if (!run.es) attachStream(runId)
}

// ---------- 产物 ----------

// 清单组 → 组内全部文件的根前缀相对路径（勾选/下载的寻址形态）
export function groupRelPaths(group) {
  return group.files.map((f) => (group.dir ? `${group.dir}/${f.name}` : f.name))
}

// 勾选集剪枝：清单刷新后消失的文件移出勾选（否则 zip 请求会带上已
// 不存在的路径——服务端会跳过，但计数与按钮文案先骗了人）
function pruneSelection(groups) {
  const listed = new Set()
  for (const g of groups) for (const p of groupRelPaths(g)) listed.add(p)
  const next = {}
  for (const p of Object.keys(state.artifactSel)) if (listed.has(p)) next[p] = true
  return next
}

// 清单刷新：阶段推进/终态事件触发（无 run 参数，全局镜像）
export async function refreshArtifacts() {
  try {
    const resp = await fetch('/api/artifacts')
    if (!resp.ok) return
    const data = await resp.json()
    set({ artifacts: data, artifactSel: pruneSelection(data.groups) })
  } catch {
    // 清单刷新是尽力而为：失败不打断会话观察，下次阶段事件再试
  }
}

// 勾选单个产物（行内复选框）
export function toggleArtifactSel(relPath) {
  const next = { ...state.artifactSel }
  if (next[relPath]) delete next[relPath]
  else next[relPath] = true
  set({ artifactSel: next })
}

// 批量勾选/取消一组路径（组头全选、卡头全选用）
export function setArtifactSel(relPaths, on) {
  const next = { ...state.artifactSel }
  for (const p of relPaths) {
    if (on) next[p] = true
    else delete next[p]
  }
  set({ artifactSel: next })
}

export function clearArtifactSel() {
  set({ artifactSel: {} })
}

// 查看单个产物：文本内容按需拉取（缓存于全局单槽），产物 tab 渲染；
// 二进制产物（清单带 binary 标记，如 rpms/ 下的 .rpm 包）不拉内容，
// 直接以占位视图呈现（元信息来自清单条目）+ 下载按钮。
// relPath 形如 "rpm/nginx/1.25.3/nginx-rpm-result.md"；逐段编码（整段
// encode 会把 / 也编码）
export async function openArtifact(relPath, entry) {
  if (entry?.binary) {
    const cut = relPath.lastIndexOf('/')
    set({
      artifact: {
        dir: cut > 0 ? relPath.slice(0, cut) : '',
        name: entry.name,
        stage: entry.stage ?? null,
        size: entry.size ?? null,
        binary: true,
      },
    })
    return
  }
  try {
    const resp = await fetch(`/api/artifacts/file/${relPath.split('/').map(encodeURIComponent).join('/')}`)
    const data = await resp.json().catch(() => ({}))
    if (!resp.ok) {
      fail(`打开产物失败：${data.detail || `HTTP ${resp.status}`}`)
      return
    }
    set({ artifact: data })
  } catch (err) {
    fail(`打开产物失败：${err.message}`)
  }
}

// 单文件下载：服务端带附件头，临时 <a> 触发浏览器下载（不离开当前页）
export function downloadArtifact(relPath) {
  const a = document.createElement('a')
  a.href = `/api/artifacts/download/${relPath.split('/').map(encodeURIComponent).join('/')}`
  document.body.appendChild(a)
  a.click()
  a.remove()
}

// 批量下载：勾选集 POST 到 zip 端点，blob 经 objectURL 触发下载；文件名
// 取服务端 Content-Disposition（auto-image-artifacts-<n>-<时间戳>.zip）
export async function downloadArtifactZip() {
  const paths = Object.keys(state.artifactSel)
  if (!paths.length || state.artifactZipping) return
  set({ artifactZipping: true })
  try {
    const resp = await fetch('/api/artifacts/zip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths }),
    })
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}))
      fail(`打包下载失败：${data.detail || `HTTP ${resp.status}`}`)
      return
    }
    const disposition = resp.headers.get('Content-Disposition') || ''
    const match = disposition.match(/filename="?([^";]+)"?/)
    const url = URL.createObjectURL(await resp.blob())
    const a = document.createElement('a')
    a.href = url
    a.download = match ? match[1] : 'auto-image-artifacts.zip'
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch (err) {
    fail(`打包下载失败：${err.message}`)
  } finally {
    set({ artifactZipping: false })
  }
}

// 启动即恢复任务列表（含服务重启后经 transcript 重建的历史）与产物清单
// （loadRuns 无历史时提前 return，产物首刷不能依赖它）
loadRuns()
refreshArtifacts()
