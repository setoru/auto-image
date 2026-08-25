// 从会话状态派生展示数据（布局自理），与服务端 first_prompt 语义对齐

// 任务名 = 首条指令截断（与 SDK list_sessions 的 first_prompt 对齐）
export function firstPromptPreview(run, max = 18) {
  if (!run) return ''
  const first = run.events.find((e) => e.type === 'user.message')
  if (!first) return '(空会话)'
  const t = first.payload.text.trim().replace(/\s+/g, ' ')
  return t.length > max ? t.slice(0, max) + '…' : t
}

export function fmtElapsed(startedAt, now) {
  if (!startedAt) return '--:--'
  const s = Math.max(0, Math.floor(((now || Date.now()) - startedAt) / 1000))
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

// 产物大小：B / KB（清单 size 字段的展示形态）
export function fmtSize(bytes) {
  if (bytes == null) return ''
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`
}
