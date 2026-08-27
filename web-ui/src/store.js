// 共享会话状态：多会话并存（挂起可多个、执行至多一个），动作经 HTTP/SSE
// 与服务端交互。每个活跃会话一条常驻 EventSource（断线浏览器自动重连并
// 携带 Last-Event-ID，服务端从 seq+1 补发；已收事件按 seq 去重）。
//
// 停止 / 关闭 / 续接的干预端点见后端 web/（run_agent 按 stop_requested 标记
// 区分 turn.stopped 与 turn.completed）；失败时如实把错误显示在提示条上。
import { useSyncExternalStore } from 'react'

// 与服务端内部事件协议一致的事件类型全集
export const EVENT_TYPES = [
  'run.started',
  'run.title_changed',
  'user.message',
  'agent.thinking',
  'agent.message',
  'agent.tool_started',
  'agent.tool_finished',
  'stage.changed',
  'turn.stopped',
  'turn.completed',
  'run.interrupted',
  'run.canceled',
  'run.failed',
  'run.ended',
]

// 会触发产物清单刷新的事件：阶段推进（新产物落盘）与回合/会话收尾。
// 清单是 deploy/ 全量镜像（与查看中的会话无关），任一 run 触发都全局刷新
const REFRESH_EVENT_TYPES = ['stage.changed', 'turn.completed', 'turn.stopped', 'run.canceled', 'run.failed', 'run.ended']

const RUNNING = 'RUNNING'
const WAITING_INPUT = 'WAITING_INPUT'
// 活跃（可继续操作）状态集合：判定值与服务端状态机一致，单处维护
const ACTIVE = [RUNNING, WAITING_INPUT]
export const isActive = (status) => ACTIVE.includes(status)

// 409 detail 判定值 → 人话提示（判定值与服务端 Conflict.detail 一致，单处维护）
const CONFLICT_HINT = {
  deployment_in_progress: '已有会话在执行（挂起中的会话不阻塞）',
  execution_in_progress: '有会话正在执行，须先停止当前回合',
  run_not_active: '会话已结束，不可再操作',
  session_in_use: '源会话尚未结束，不能续接',
}

const listeners = new Set()
// order 即任务下拉次序：最新在前（服务端列表同序，新建前插）
let state = {
  runs: {},
  order: [],
  viewRunId: null,
  submitError: null,
  now: Date.now(),
  artifacts: { groups: [] }, // deploy/ 全量产物（目录分组，全局不属于任何 run）
  artifact: null,            // 当前查看中的产物内容（单槽，点击整体替换）
}

// 时长走针仅在会话执行期间（挂起与终态冻结，终态另有 endedAt 兜底）
setInterval(() => {
  if (executingRunId()) set({ now: Date.now() })
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

export function executingRunId() {
  return state.order.find((id) => state.runs[id]?.status === RUNNING) ?? null
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
  // 阶段推进与终态都可能带来新落盘的产物，触发清单刷新
  if (REFRESH_EVENT_TYPES.includes(event.type)) refreshArtifacts()
  // 终态事件后服务端会正常结束流，主动 close 避免 EventSource 无限重连
  // （run.ended 是重启找回历史的收尾：只读回放完毕即关流）
  if (event.type === 'run.failed' || event.type === 'run.canceled' || event.type === 'run.ended') es.close()
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
// （重放的 stage.changed / 终态事件会重复触发清单刷新，幂等无害）。
// 终态单向：历史回放中的 turn.* 不把已终态的 run 拉回挂起。
function appendTo(runId, event) {
  const run = state.runs[runId]
  if (!run || run.events.some((ev) => ev.seq === event.seq)) return
  const patch = { events: [...run.events, event] }
  // 最后活动时刻以服务端事件 ts 为准（刷新/SSE 重放后不漂移）
  if (event.payload.ts) patch.lastEventAt = event.payload.ts * 1000
  if (event.type === 'stage.changed') patch.stage = event.payload.stage
  if (event.type === 'run.title_changed') patch.title = event.payload.title
  if (event.type === 'turn.completed') patch.result = event.payload.result
  if (isActive(run.status)) {
    if (event.type === 'turn.completed' || event.type === 'turn.stopped') {
      patch.status = WAITING_INPUT // 回合完成/被停止 ≠ 会话结束
    }
    if (event.type === 'run.failed') {
      patch.status = 'FAILED'
      patch.endedAt = Date.now()
    }
    if (event.type === 'run.canceled') {
      patch.status = 'CANCELED'
      patch.endedAt = Date.now()
    }
    if (event.type === 'run.ended') patch.status = 'ENDED'
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

// 启动加载：拉全量 run 摘要恢复任务下拉（服务重启后历史经 transcript 重建，
// 终态只读回看、挂起可续聊）；首屏即回放查看中的那条。尽力而为，失败从空开始。
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

// 新建 = 一步创建空会话（WAITING_INPUT），无中间表单；执行中置灰由 UI 保证；
// resumeFrom 给定时从该终态会话续接上下文（「↩ 接续此会话」）
export async function createRun(resumeFrom = null) {
  if (executingRunId()) {
    fail(conflictText('deployment_in_progress'))
    return
  }
  try {
    const data = await postJson('/api/runs', resumeFrom ? { resume_from: resumeFrom } : {})
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

// 停止 = CLI 的 Esc：打断执行中的回合（作用于当前执行中的会话，不一定是查看中的）
export async function stop() {
  const runId = executingRunId()
  if (!runId) return
  try {
    await postJson(`/api/runs/${runId}/stop`, {})
  } catch (err) {
    fail(`停止失败：${err.message}`)
  }
}

// 向查看中的会话发指令：挂起会话须无其他执行；执行中发送由服务端先停止再投递。
// 返回是否投递成功（失败时输入由调用方保留）。
export async function send(text) {
  const trimmed = (text ?? '').trim()
  const run = state.runs[state.viewRunId]
  if (!run || !trimmed) return false
  if (!isActive(run.status)) {
    // 只读会话（已结束/重启找回的历史）不静默吞掉输入，给出出路提示
    fail('该会话只读（已结束或重启找回的历史）——「+ 新建」或接续该会话后继续')
    return false
  }
  if (run.status === WAITING_INPUT && executingRunId()) {
    fail(conflictText('execution_in_progress'))
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

// 关闭查看中的会话（执行中或挂起均可关闭）
export async function cancel() {
  const run = state.runs[state.viewRunId]
  if (!run || !isActive(run.status)) return
  try {
    await postJson(`/api/runs/${run.runId}/cancel`, {})
  } catch (err) {
    fail(`关闭会话失败：${err.message}`)
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

// 清单刷新：阶段推进/终态事件触发（无 run 参数，全局镜像）
export async function refreshArtifacts() {
  try {
    const resp = await fetch('/api/artifacts')
    if (!resp.ok) return
    set({ artifacts: await resp.json() })
  } catch {
    // 清单刷新是尽力而为：失败不打断会话观察，下次阶段事件再试
  }
}

// 查看单个产物：内容按需拉取（缓存于全局单槽），产物 tab 渲染。
// relPath 形如 "pi/0.84.2/pi-config"；逐段编码（整段 encode 会把 / 也编码）
export async function openArtifact(relPath) {
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

// 启动即恢复任务列表（含服务重启后经 transcript 重建的历史）与产物清单
// （loadRuns 无历史时提前 return，产物首刷不能依赖它）
loadRuns()
refreshArtifacts()
