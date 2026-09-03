// PROTOTYPE — 布局手感验证专用，验证后整体删除，勿并入正式代码。
// 假数据 + 混合 tab 状态机：语义照布局冻结案（session:run_N / file:<relPath>
// 复合 key、文件 tab 插当前激活 tab 右侧、header/输入条只跟最后激活的
// 会话 tab、最后一枚会话 tab 不可关）。store.js 一行不碰。
import { useSyncExternalStore } from 'react'

const RUNNING = 'RUNNING', READY = 'READY', ENDED = 'ENDED'

// 相对时间文本：超过 24h 显示天数
function agoText(ms) {
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000))
  if (s < 60) return '刚刚'
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`
  return `${Math.floor(s / 86400)} 天前`
}

// ---------------------------------------------------------------- 假数据
// runs：标题/状态/阶段/活动时刻/几条消息事件；119 条历史只取十几条代表
// （RUNNING 会跑动、READY 新近、ENDED 沉底），不做 119 条全量（原型要的是
// 密度感不是分页）。
function seed() {
  const now = Date.now()
  const mk = (runId, title, status, stage, minsAgo, lines) => ({
    runId, title, status, stage,
    lastActivity: now - minsAgo * 60000,
    elapsed: status === RUNNING ? 372 : 300,
    // 尾缀 r/f 标记回合收尾类型（f → 失败态点）
    events: [
      ...lines.map(([who, text]) => ({ who, text })),
      ...(status === RUNNING ? [{ who: 'agent', text: '▍（正在执行：安装 docker-ce…）' }] : []),
    ],
    lastTurn: lines[lines.length - 1]?.[1] ?? '',
    failed: false,
  })
  const runs = [
    mk('run_1', 'nginx 1.25.3 部署', RUNNING, 'INSTALL', 0.2, [
      ['user', '部署 nginx 1.25.3 到目标机 ecs-web-01，按官方文档装'],
      ['agent', '好，开始生成部署指南。目标机系统 Ubuntu 22.04，将按 apt 官方源安装。'],
      ['user', '好，继续'],
    ]),
    mk('run_2', 'redis 验证', READY, 'VERIFY', 6, [
      ['user', '验证 redis 是否装好'],
      ['agent', 'redis-cli ping 返回 PONG，版本 7.0.11，服务自启已启用。'],
    ]),
    mk('run_3', '克隆：nginx 调优', READY, 'VERIFY', 15, [
      ['user', '克隆这个会话调 worker_processes'],
      ['agent', '已从源会话分叉。要调到多少？'],
    ]),
    mk('run_4', 'mysql 8 安装', ENDED, 'ARCHIVE', 130, [
      ['user', '装 mysql 8'],
      ['agent', '安装完成，已交付 rpm 包。'],
    ]),
    mk('run_5', '空会话', READY, null, 30, []),
    mk('run_6', '老会话·httpd', ENDED, null, 60 * 26, [
      ['user', '部署 httpd 2.4.57'],
      ['agent', '完成并验证通过。'],
    ]),
    mk('run_7', '老会话·node', ENDED, null, 60 * 50, [
      ['user', '装 node 20'],
      ['agent', '完成。'],
    ]),
  ]
  // run_2 标失败态（尾部 turn.failed 语义）
  runs[1].failed = true
  return { runs }
}

// 产物树（仿 artifactTree 输出；产物全局不属于任何 run）：最新一组默认展开
function seedArtifactTree() {
  return [
    { name: 'deploy', path: 'deploy', count: 4, files: [
      { name: 'nginx-install.md', stage: 'INSTALL', size: 15300, content: '# nginx 安装指南\n\n官方 apt 源安装步骤：\n\n1. `apt-get update`\n2. `apt-get install nginx=1.25.3-1~jammy`\n\n| 项 | 值 |\n|---|---|\n| 版本 | 1.25.3 |\n| 源 | nginx.org 官方 |\n' },
      { name: 'nginx-verify.md', stage: 'VERIFY', size: 9200, content: '# nginx 验证\n\n`nginx -v` 输出 1.25.3；`curl -I localhost` 返回 200。\n' },
      { name: 'app-deploy.md', stage: 'INSTALL', size: 4100, content: '# 应用部署\n\n> 验证契约：服务响应 200 且进程常驻\n\nsystemd 单元已启用。\n' },
    ], dirs: [
      { name: 'httpd', path: 'deploy/httpd', files: [], dirs: [
        { name: '2.4.57', path: 'deploy/httpd/2.4.57', files: [
          { name: 'httpd-install.md', stage: 'INSTALL', size: 12300, content: '# httpd 安装指南\n\n`dnf install httpd-2.4.57`\n' },
        ], dirs: [], count: 1 },
      ], count: 1 },
    ] },
    { name: 'rpm', path: 'rpm', count: 2, files: [], dirs: [
      { name: 'nginx', path: 'rpm/nginx', files: [], dirs: [
        { name: '1.25.3', path: 'rpm/nginx/1.25.3', files: [
          { name: 'nginx-rpm-result.md', stage: 'BUILD', size: 2300, content: '# RPM 构建结果\n\n构建成功，产出 binary/source 两类包。\n' },
          { name: 'nginx-1.25.3.el9.x86_64.rpm', stage: 'BUILD', size: 1284500, binary: true },
        ], dirs: [], count: 2 },
      ], count: 2 },
    ] },
  ]
}

// 产物文件内容缓存（多槽 map）：文件 tab 首开拉取，关 tab 不清（冻结案 3）
const fileCache = new Map()

// ---------------------------------------------------------------- 状态机
// tabs: [{kind:'session', runId} | {kind:'file', relPath, name}]
// activeKey: 'session:run_2' | 'file:deploy/nginx-install.md'
let state = {
  runs: [],
  tree: seedArtifactTree(),
  tabs: [],
  activeKey: null,
  sel: {},          // 产物勾选集
  // 每枚会话 tab 独立 input 文案（切 tab 草稿保留）与滚动位置
  drafts: {},
  scrollPos: {},
  tick: 0,
}

const listeners = new Set()
function set(patch) {
  state = { ...state, ...patch }
  listeners.forEach((l) => l())
}
export const subscribe = (l) => (listeners.add(l), () => listeners.delete(l))
export const getState = () => state
export function useProto() {
  return useSyncExternalStore(subscribe, getState)
}

export const tabKey = (t) => (t.kind === 'session' ? `session:${t.runId}` : `file:${t.relPath}`)
export const sessionTabs = () => state.tabs.filter((t) => t.kind === 'session')
export const activeTab = () => state.tabs.find((t) => tabKey(t) === state.activeKey) ?? null
// header/输入条绑定「最后激活的会话 tab」：激活文件 tab 时仍显示那个会话
export const controlRunId = () => {
  const a = activeTab()
  if (a?.kind === 'session') return a.runId
  return sessionTabs().at(-1)?.runId ?? null
}
export const statusDot = (run) => run.status === RUNNING ? 'running'
  : run.status === ENDED ? 'ended'
  : run.failed ? 'failed' : 'ready'

// 时钟单点：running 活动推进 + 相对时间刷新
setInterval(() => set({ tick: state.tick + 1 }), 1000)

export function boot() {
  if (state.tabs.length) return
  state.runs = seed().runs
  set({
    tabs: [
      { kind: 'session', runId: 'run_1' },
      { kind: 'session', runId: 'run_2' },
      { kind: 'session', runId: 'run_4' },
    ],
    activeKey: 'session:run_1',
  })
}

// ---------------------------------------------------------------- 动作
// 开会话 tab：已有则激活；新则尾插（会话是持久对象，尾插贴旧 Tabs 手感）
export function openSession(runId) {
  const exists = state.tabs.find((t) => t.kind === 'session' && t.runId === runId)
  if (exists) return set({ activeKey: `session:${runId}` })
  set({ tabs: [...state.tabs, { kind: 'session', runId }], activeKey: `session:${runId}` })
}

// 开文件 tab：已有激活；新则插当前激活 tab 右侧（VSCode 次序，冻结案）
export function openFile(relPath, entry) {
  const exists = state.tabs.find((t) => t.kind === 'file' && t.relPath === relPath)
  if (exists) return set({ activeKey: `file:${relPath}` })
  const cut = relPath.lastIndexOf('/')
  const tab = { kind: 'file', relPath, name: entry?.name ?? relPath.slice(cut + 1), entry }
  if (entry) fileCache.set(relPath, entry) // 假数据即清单内容，无网络请求
  const idx = state.tabs.findIndex((t) => tabKey(t) === state.activeKey)
  const tabs = [...state.tabs]
  tabs.splice(idx + 1, 0, tab)
  set({ tabs, activeKey: `file:${relPath}` })
}

// 关 tab：文件 tab 随便关；最后一枚会话 tab 不可关。关激活 tab 时：
// 关文件 → 激活左邻（会话）；关会话 → 激活右邻否则左邻
export function closeTab(key) {
  const idx = state.tabs.findIndex((t) => tabKey(t) === key)
  if (idx === -1) return
  const t = state.tabs[idx]
  if (t.kind === 'session' && sessionTabs().length === 1) return
  const tabs = state.tabs.filter((x) => tabKey(x) !== key)
  let activeKey = state.activeKey
  if (activeKey === key) {
    activeKey = t.kind === 'file'
      ? tabKey(tabs[Math.min(idx - 1, tabs.length - 1)] ?? tabs[0])
      : tabKey(tabs[Math.min(idx, tabs.length - 1)] ?? tabs[0])
    if (activeKey == null) activeKey = tabKey(sessionTabs()[0])
  }
  set({ tabs, activeKey })
}

export function activate(key) { set({ activeKey: key }) }

// 会话 tab 独立输入草稿（切 tab 不丢字）
export function setDrafts(drafts) { set({ drafts }) }

// 新建会话（+ 新建）：假数据只发一条本地 toast（不上后端）
export function createRun() {
  const n = state.runs.length + 1
  const run = {
    runId: `run_new${n}`, title: '新会话', status: READY, stage: null,
    lastActivity: Date.now(), elapsed: 0, events: [], lastTurn: '', failed: false,
  }
  set({ runs: [run, ...state.runs], toast: '（原型）新建会话——真实实现走 POST /api/runs' })
  openSession(run.runId)
  setTimeout(() => set({ toast: null }), 2000)
}

export function toggleSel(relPath) {
  const sel = { ...state.sel }
  if (sel[relPath]) delete sel[relPath]
  else sel[relPath] = true
  set({ sel })
}
export function setSel(relPaths, on) {
  const sel = { ...state.sel }
  for (const p of relPaths) { if (on) sel[p] = true; else delete sel[p] }
  set({ sel })
}
export function subtreeRels(node) {
  return node.files.map((f) => `${node.path}/${f.name}`).concat(...node.dirs.map(subtreeRels))
}

export { agoText, RUNNING, READY, ENDED }
