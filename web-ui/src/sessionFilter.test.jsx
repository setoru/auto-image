import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

// 静态渲染只读取真实 store 快照；订阅与 DOM 交互由隔离浏览器验收。
vi.mock('react', async importOriginal => ({
  ...await importOriginal(),
  useSyncExternalStore: (_subscribe, getSnapshot) => getSnapshot(),
}))

const response = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { 'Content-Type': 'application/json' },
})
const snapshot = (events = []) => new Response(events.map(({ seq, type, payload }) =>
  `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify(payload)}\n\n`,
).join(''), { headers: { 'Content-Type': 'text/event-stream' } })

let store, SidePanel, summaries, identity, history, sources
async function fetchResponse(url) {
  if (url === '/api/auth/me' || url === '/api/auth/login') return response(identity)
  if (url === '/api/runs') return response({ runs: summaries })
  if (url.endsWith('/events')) return snapshot(history[url.split('/')[3]] ?? [])
  return response({})
}
const markup = () => renderToStaticMarkup(<SidePanel />)
const rows = () => [...markup().matchAll(/class="va-sess-name">([^<]*)</g)].map(match => match[1])
function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
function broadcast(runId, seq, type, payload) {
  sources.at(-1).handlers[type]({ data: JSON.stringify({ run_id: runId, seq, type, payload }) })
}

beforeEach(async () => {
  vi.resetModules()
  vi.useFakeTimers()
  const saved = new Map()
  vi.stubGlobal('localStorage', {
    getItem: key => saved.get(key) ?? null,
    setItem: (key, value) => saved.set(key, value),
    removeItem: key => saved.delete(key),
  })
  sources = []
  vi.stubGlobal('EventSource', class {
    constructor() { this.handlers = {}; sources.push(this) }
    addEventListener(type, callback) { this.handlers[type] = callback }
    close() { this.closed = true }
  })
  summaries = [
    { run_id: 'run-a', title: `Alpha ${'长标题'.repeat(20)}TitleTail`, first_prompt: `部署 ${'长指令'.repeat(30)}PromptTail`, status: 'READY', started_at: 3 },
    { run_id: 'run-b', title: 'Beta Redis', first_prompt: '复核 Redis [7.2]', status: 'READY', started_at: 2 },
    { run_id: 'Session-C', title: 'Alpha Nginx', first_prompt: null, status: 'READY', started_at: 1 },
  ]
  identity = { username: 'alice' }
  history = {}
  vi.stubGlobal('fetch', vi.fn(fetchResponse))
  store = await import('./store.js')
  SidePanel = (await import('./components/SidePanel.jsx')).default
  await store.initAuth()
  await store.loadRuns()
  store.setSidePanel('sessions')
  fetch.mockClear()
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('标题显示截短后仍能命中完整标题尾部，并显示匹配数与总数', () => {
  store.setSessionKeyword('TitleTail')
  expect(rows()).toEqual([`Alpha ${'长标题'.repeat(11)}长…`])
  expect(markup()).toContain('匹配 1 / 共 3 条')
  expect(fetch.mock.calls).toEqual([])
})

it.each([
  ['  PROMPTtail  ', ['Alpha']],
  ['  sEsSiOn-c  ', ['Alpha Nginx']],
  ['[7.2]', ['Beta Redis']],
  ['.*', []],
  ['Alpha Redis', []],
  [' ', ['Alpha', 'Beta Redis', 'Alpha Nginx']],
])('首条指令、ID 和连续字面匹配：%s', (keyword, expected) => {
  store.setSessionKeyword(keyword)
  expect(rows().map(title => title.startsWith('Alpha 长') ? 'Alpha' : title)).toEqual(expected)
  expect(fetch.mock.calls).toEqual([])
})

it('摘要已有首条指令仍检索它；缺失时取首条用户事件，不搜索后续正文或草稿', async () => {
  history['Session-C'] = [
    { seq: 1, type: 'user.message', payload: { text: '开头 '.repeat(50) + 'EventTail' } },
    { seq: 2, type: 'user.message', payload: { text: 'LaterSecret' } },
    { seq: 3, type: 'agent.message', payload: { text: 'AgentSecret' } },
  ]
  store.selectRun('Session-C')
  await vi.waitFor(() => expect(store.getState().runs['Session-C'].events).toHaveLength(3))
  store.setDraft('run-a', 'DraftSecret')
  store.setSessionKeyword('EventTail')
  expect(rows()).toEqual(['Alpha Nginx'])
  for (const keyword of ['LaterSecret', 'AgentSecret', 'DraftSecret']) {
    store.setSessionKeyword(keyword)
    expect(rows()).toEqual([])
  }
})

it('沿用完整清单的最近活跃顺序，后台标题、指令和新会话更新继续应用当前词', async () => {
  store.setSessionKeyword('alpha')
  expect(rows()).toEqual([`Alpha ${'长标题'.repeat(11)}长…`, 'Alpha Nginx'])
  broadcast('run-a', 1, 'session.title_changed', { title: '改名后的会话' })
  expect(rows()).toEqual(['Alpha Nginx'])
  summaries = [
    { run_id: 'new', title: '新增 Alpha', status: 'READY', started_at: 4 },
    { ...summaries[1], first_prompt: '后台补齐 Alpha' },
    summaries[0], summaries[2],
  ]
  await store.loadRuns()
  expect(rows()).toEqual(['新增 Alpha', 'Beta Redis', 'Alpha Nginx'])
  expect(markup()).toContain('匹配 3 / 共 4 条')
  expect(store.getState().sessionKeyword).toBe('alpha')
})

it('零匹配和暂无数据分别提示，清除后恢复原列表', async () => {
  const original = rows()
  store.setSessionKeyword('missing')
  expect(rows()).toEqual([])
  expect(markup()).toContain('没有匹配的会话')
  expect(markup()).toContain('匹配 0 / 共 3 条')
  expect(markup()).not.toContain('暂无会话')
  store.setSessionKeyword('')
  expect(rows()).toEqual(original)
  summaries = []
  await store.loadRuns()
  expect(markup()).toContain('暂无会话')
  expect(markup()).toContain('匹配 0 / 共 0 条')
  expect(markup()).not.toContain('没有匹配的会话')
})

it('隐藏控制面会话只改变侧栏展示，完整数据、标签、草稿、阅读和勾选保持', () => {
  store.setDraft('run-a', '继续核对\n保留草稿')
  const reading = { follow: false, anchor: { seq: 3, offset: -24 }, seenSeq: 8, details: {} }
  store.saveReading('run-a', reading, store.getState().identityEpoch)
  store.setArtifactSel(['deploy/nginx/1/report.md'], true)
  const before = store.getState()
  store.setSessionKeyword('Redis')
  expect(rows()).toEqual(['Beta Redis'])
  const after = store.getState()
  for (const field of ['runs', 'order', 'tabs', 'activeKey', 'lastSessionKey', 'drafts', 'artifactSel']) {
    expect(after[field]).toBe(before[field])
  }
  expect(store.controlRunId()).toBe('run-a')
  expect(store.readingOf('run-a')).toEqual(reading)
  expect(sources.filter(source => !source.closed)).toHaveLength(1)
  expect(fetch.mock.calls).toEqual([])
})

it('切面板及同一身份核验保留原词，刷新模块只恢复草稿不恢复查询', async () => {
  store.setSessionKeyword('Redis')
  store.setDraft('run-a', '保留正文')
  for (const panel of ['artifacts', 'tasks', 'sessions']) store.setSidePanel(panel)
  await store.initAuth()
  expect(store.getState().sessionKeyword).toBe('Redis')
  expect(rows()).toEqual(['Beta Redis'])
  vi.clearAllTimers()
  vi.resetModules()
  store = await import('./store.js')
  await store.initAuth()
  await store.loadRuns()
  expect(store.getState().sessionKeyword).toBe('')
  expect(store.draftOf('run-a')).toBe('保留正文')
})

it.each(['logout', 'change-user', 'password-change', 'expired', 'business-restricted'])(
  '身份清理 %s 重置查询', async action => {
    store.setSessionKeyword('Redis')
    if (action === 'logout') await store.logout()
    if (action === 'change-user') {
      identity = { username: 'bob' }
      summaries = [{ run_id: 'bob-only', title: 'Bob 会话', status: 'READY' }]
      await store.login('bob', 'password')
      await store.loadRuns()
      expect(rows()).toEqual(['Bob 会话'])
    }
    if (action === 'password-change') {
      identity = { username: 'alice', must_change_password: true, user_version: 2 }
      await store.initAuth()
    }
    if (action === 'expired' || action === 'business-restricted') {
      fetch.mockResolvedValue(action === 'expired'
        ? response({ detail: 'expired' }, 401)
        : response({ detail: 'password_change_required' }, 403))
      await store.loadRuns()
    }
    expect(store.getState().sessionKeyword).toBe('')
  },
)

it('[race] 旧身份的迟到清单不能恢复查询或覆盖新登录的清单与查询', async () => {
  store.setSessionKeyword('Redis')
  const pending = deferred(), oldSummaries = summaries
  fetch.mockImplementation(url => url === '/api/runs' ? pending.promise : fetchResponse(url))
  const loading = store.loadRuns()
  await store.logout()
  expect(store.getState().sessionKeyword).toBe('')
  fetch.mockImplementation(fetchResponse)
  await store.login('alice', 'password')
  summaries = [{ run_id: 'fresh', title: '新的 Alpha', status: 'READY' }]
  await store.loadRuns()
  store.setSessionKeyword('新的')
  pending.resolve(response({ runs: oldSummaries }))
  await loading
  expect(store.getState().sessionKeyword).toBe('新的')
  expect(rows()).toEqual(['新的 Alpha'])
})

it('[race] 筛掉发送中的会话后迟到回执仍归原会话，保留后来草稿及阅读', async () => {
  const pending = deferred()
  fetch.mockImplementation(url => url === '/api/runs/run-a/messages' ? pending.promise : fetchResponse(url))
  store.setDraft('run-a', '原提交')
  const reading = { follow: false, anchor: { seq: 3, offset: -24 }, seenSeq: 8, details: {} }
  store.saveReading('run-a', reading, store.getState().identityEpoch)
  const sending = store.send('原提交')
  store.setDraft('run-a', '后来编辑')
  store.setSessionKeyword('Redis')
  expect(store.controlRunId()).toBe('run-a')
  store.selectRun('run-b')
  store.setDraft('run-b', 'B 草稿')
  pending.resolve(response({ run_id: 'run-a', status: 'RUNNING' }))
  await sending
  expect(store.submissionOf('run-a').status).toBe('accepted')
  expect(store.submissionOf('run-b')).toBeNull()
  expect(store.draftOf('run-a')).toBe('后来编辑')
  expect(store.draftOf('run-b')).toBe('B 草稿')
  expect(store.readingOf('run-a')).toEqual(reading)
  expect(store.controlRunId()).toBe('run-b')
  expect(rows()).toEqual(['Beta Redis'])
  expect(fetch.mock.calls.filter(([url]) => url.endsWith('/messages'))).toHaveLength(1)
})
