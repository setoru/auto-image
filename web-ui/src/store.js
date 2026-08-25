// 共享会话状态：多会话并存（挂起可多个、执行至多一个），动作经 HTTP/SSE
// 与服务端交互。每个活跃会话一条常驻 EventSource（断线浏览器自动重连并
// 携带 Last-Event-ID，服务端从 seq+1 补发；已收事件按 seq 去重）。
//
// 停止 / 关闭调用的端点随干预语义接入（见后端 web/），当前后端尚未提供，
// 失败时如实把错误显示在提示条上，端点就绪后此处无需改动。
import { useSyncExternalStore } from 'react'

// 与服务端内部事件协议一致的事件类型全集
export const EVENT_TYPES = [
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
let state = { runs: {}, order: [], viewRunId: null, submitError: null, now: Date.now() }

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
  // 终态后服务端会正常结束流，主动 close 避免 EventSource 无限重连
  if (event.type === 'run.failed' || event.type === 'run.canceled') es.close()
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
function appendTo(runId, event) {
  const run = state.runs[runId]
  if (!run || run.events.some((ev) => ev.seq === event.seq)) return
  const patch = { events: [...run.events, event] }
  if (event.type === 'stage.changed') patch.stage = event.payload.stage
  if (event.type === 'turn.completed') {
    patch.status = WAITING_INPUT // 回合完成 ≠ 会话结束
    patch.result = event.payload.result
  }
  if (event.type === 'turn.stopped') patch.status = WAITING_INPUT
  if (event.type === 'run.failed') {
    patch.status = 'FAILED'
    patch.endedAt = Date.now()
  }
  if (event.type === 'run.canceled') {
    patch.status = 'CANCELED'
    patch.endedAt = Date.now()
  }
  setRun(runId, patch)
}

// ---------- HTTP ----------

// 新建 = 一步创建空会话（WAITING_INPUT），无中间表单；执行中置灰由 UI 保证
export async function createRun() {
  if (executingRunId()) {
    fail(conflictText('deployment_in_progress'))
    return
  }
  try {
    const data = await postJson('/api/runs', {})
    const run = {
      runId: data.run_id,
      status: data.status,
      stage: null,
      events: [],
      result: null,
      connection: 'live',
      startedAt: Date.now(),
      es: null,
    }
    set({
      runs: { ...state.runs, [run.runId]: run },
      order: [...state.order, run.runId],
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
  if (!run || !trimmed || !isActive(run.status)) return false
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
  if (state.runs[runId]) set({ viewRunId: runId })
}
