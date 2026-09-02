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

// ---------- 产物目录树（清单平铺分组 → 嵌套树） ----------

// 服务端清单组键（"rpm/httpd/2.4.57" 形）按 / 拆段逐级挂载：节点.path 即
// 组键（勾选/下载的寻址前缀），files 为该目录自身组内文件，count 为子树
// 文件总数。子目录次序取首见次序——组间已按最新落盘降序，各级天然「最新
// 在前」；返回根层节点（deploy、rpm……），空清单返回 []。
export function artifactTree(groups) {
  const roots = []
  const byPath = new Map()
  for (const g of groups) {
    let node = null
    let path = ''
    for (const seg of g.dir.split('/')) {
      path = path ? `${path}/${seg}` : seg
      let child = byPath.get(path)
      if (!child) {
        child = { name: seg, path, dirs: [], files: [] }
        byPath.set(path, child)
        const holder = node ?? { dirs: roots }
        holder.dirs.push(child)
      }
      node = child
    }
    if (node) node.files = g.files
  }
  const tally = (n) => n.files.length + n.dirs.reduce((sum, d) => sum + tally(d), 0)
  for (const r of roots) r.count = tally(r)
  return roots
}

// 节点子树全部文件的根前缀相对路径（目录行三态勾选 / 卡头全选的勾选集）
export function subtreeRels(node) {
  const out = node.files.map((f) => `${node.path}/${f.name}`)
  for (const d of node.dirs) out.push(...subtreeRels(d))
  return out
}

// 默认展开路径集：最新一组（groups[0]，服务端按落盘时间降序）及其各级
// 祖先——沿用旧平铺形态「默认展开最新一组」的行为，未手动动过的目录才生效
export function defaultOpenPaths(groups) {
  const open = new Set()
  const first = groups[0]?.dir
  if (!first) return open
  let path = ''
  for (const seg of first.split('/')) {
    path = path ? `${path}/${seg}` : seg
    open.add(path)
  }
  return open
}
