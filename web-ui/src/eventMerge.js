const RUNNING = 'RUNNING'
const READY = 'READY'
const ENDED = 'ENDED'

const READY_EVENTS = new Set([
  'turn.completed',
  'turn.stopped',
  'turn.failed',
  'turn.interrupted',
])

function orderedUniqueEvents(existing, incoming) {
  const bySeq = new Map()
  for (const event of existing) {
    if (!bySeq.has(event.seq)) bySeq.set(event.seq, event)
  }
  for (const event of incoming) {
    // seq 对应不可变事件；两路冲突时保留已经落地的事实。
    if (!bySeq.has(event.seq)) bySeq.set(event.seq, event)
  }
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq)
}

/**
 * 合并单个会话的事件事实，并从完整有序事件统一派生展示状态。
 * 摘要字段只在尚未见到对应事件时作为初值。
 */
export function mergeRunEvents(run, incoming = []) {
  const events = orderedUniqueEvents(run.events ?? [], incoming)
  let status = run.status
  let stage = run.stage
  let title = run.title
  let result = run.result
  let endedAt = run.endedAt
  let lastEventAt = run.lastEventAt

  for (const event of events) {
    const { type, payload = {} } = event
    if (type === 'stage.changed') stage = payload.stage
    if (type === 'session.title_changed') title = payload.title
    if (type === 'turn.completed') result = payload.result
    if (type === 'turn.started') status = RUNNING
    if (READY_EVENTS.has(type)) status = READY
    if (type === 'session.ended') {
      status = ENDED
      if (payload.ts != null) endedAt = payload.ts * 1000
    }
    if (payload.ts != null) lastEventAt = payload.ts * 1000
  }

  // 墓碑会话的历史可能没有 session.ended；摘要的 ENDED 必须保持单向。
  if (run.status === ENDED) status = ENDED

  return {
    ...run,
    events,
    status,
    stage,
    title,
    result,
    endedAt,
    lastEventAt,
    maxSeq: events.reduce((max, event) => Math.max(max, event.seq), 0),
  }
}
