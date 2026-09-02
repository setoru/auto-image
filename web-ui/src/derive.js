// 从会话状态派生展示数据（布局自理），与服务端 first_prompt / title 语义对齐

// 首条指令原文：服务端摘要的 firstPrompt 优先（重启找回的历史在事件回放前就有名字），
// 否则取事件流首条 user.message。空会话（含尚未回放的历史）返回 null。
export function firstPromptText(run) {
  if (!run) return null
  return run.firstPrompt ?? run.events.find((e) => e.type === 'user.message')?.payload.text ?? null
}

// 会话是否已有首条指令（输入条占位文案判定）
export function hasPrompt(run) {
  return firstPromptText(run) != null
}

// 任务名 = LLM 标题（服务端 run.title / 事件流 run.title_changed）优先，
// 回退首条指令截断（生成中/失败/老会话）
export function firstPromptPreview(run, max = 18) {
  if (run?.title) return run.title.length > max ? run.title.slice(0, max) + '…' : run.title
  const first = firstPromptText(run)
  if (first == null) return '(空会话)'
  const t = String(first).trim().replace(/\s+/g, ' ')
  return t.length > max ? t.slice(0, max) + '…' : t
}

// 接续标记：「↩ 接续『任务名』」；来源会话不在（列表缺它）时不标
export function resumeMark(run, runs) {
  if (!run?.resumedFrom || !runs?.[run.resumedFrom]) return ''
  return ` ↩ 接续『${firstPromptPreview(runs[run.resumedFrom])}』`
}

// 累计执行时长：各回合（user.message → 回合收尾）求和，扣除等待输入的
// 空档；执行中的回合以 now 收口。终态会话直接用 endedAt - startedAt 兜底
// 定格（方案：终态以服务端 endedAt 定格总时长）——跨重载时事件 ts 是
// 服务端时刻，不会随 Date.now() 无限增长。无 endedAt 的终态（异常边界）
// 退回事件求和，未闭合的回合不计。
export function activeSeconds(run, now) {
  if (run.endedAt != null && run.startedAt != null) {
    return Math.max(0, Math.floor((run.endedAt - run.startedAt) / 1000))
  }
  let total = 0
  let turnStart = null
  for (const ev of run.events) {
    if (ev.type === 'user.message') turnStart = (ev.payload.ts ?? run.startedAt / 1000)
    if ((ev.type === 'turn.completed' || ev.type === 'turn.stopped' || ev.type === 'run.failed') && turnStart != null) {
      total += Math.max(0, (ev.payload.ts ?? turnStart) - turnStart)
      turnStart = null
    }
  }
  if (turnStart != null && run.status !== 'CANCELED' && run.status !== 'FAILED' && run.status !== 'ENDED') {
    total += Math.max(0, (now || Date.now()) / 1000 - turnStart)
  }
  return Math.floor(total)
}

export function fmtActive(run, now) {
  if (!run.startedAt) return '--:--'
  const s = activeSeconds(run, now)
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

// 更新时间（具体日期+时间）：最后一条 user.message 的 ts——会话随用户
// 发消息推进，agent 回复是它的响应；无事件时回退会话创建时刻
export function lastUserMessageAt(run) {
  for (let i = run.events.length - 1; i >= 0; i--) {
    if (run.events[i].type === 'user.message') return (run.events[i].payload.ts ?? 0) * 1000
  }
  return null
}

export function fmtLastActivity(run) {
  const ms = lastUserMessageAt(run) ?? run.startedAt
  if (!ms) return '—'
  const d = new Date(ms)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

// 产物大小：B / KB（清单 size 字段的展示形态）
export function fmtSize(bytes) {
  if (bytes == null) return ''
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`
}
