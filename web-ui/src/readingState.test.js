import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { activeSeconds } from './derive.js'

const response = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { 'Content-Type': 'application/json' },
})
const snapshot = (events = []) => new Response(events.map(({ seq, type, payload }) =>
  `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify(payload)}\n\n`,
).join(''), { headers: { 'Content-Type': 'text/event-stream' } })
const event = (seq, type = 'agent.message', payload = {}) => ({ seq, type, payload: { ts: seq * 10, ...payload } })
const reading = (overrides = {}) => ({
  follow: false,
  anchor: { seq: 4, offset: -23, toolId: 'tool-a' },
  seenSeq: 8,
  details: { 'tool:tool-a': { open: true, scroll: [{ top: 154, left: 0 }, { top: 312, left: 5 }] } },
  ...overrides,
})
function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

let store, sources, summaries, identity, history
async function fetchResponse(url) {
  if (url === '/api/auth/me' || url === '/api/auth/login') return response(identity)
  if (url === '/api/runs') return response({ runs: summaries })
  if (url.endsWith('/events')) return snapshot(history[url.split('/')[3]] ?? [])
  return response({})
}
function broadcast(runId, ev) {
  sources.at(-1).handlers[ev.type]({ data: JSON.stringify({
    run_id: runId, seq: ev.seq, type: ev.type, ts: ev.payload.ts, payload: ev.payload,
  }) })
}
function save(runId, value) {
  store.saveReading(runId, value, store.getState().identityEpoch)
}

beforeEach(async () => {
  vi.resetModules()
  vi.useFakeTimers()
  const saved = new Map()
  vi.stubGlobal('localStorage', {
    getItem: key => saved.get(key) ?? null,
    setItem: vi.fn((key, value) => saved.set(key, value)),
    removeItem: key => saved.delete(key),
  })
  sources = []
  vi.stubGlobal('EventSource', class {
    constructor() { this.handlers = {}; sources.push(this) }
    addEventListener(type, callback) { this.handlers[type] = callback }
    close() { this.closed = true }
  })
  summaries = ['a', 'b'].map(run_id => ({ run_id, status: 'READY', started_at: 1 }))
  identity = { username: 'alice' }
  history = { a: [event(1), event(4), event(8)] }
  vi.stubGlobal('fetch', vi.fn(fetchResponse))
  store = await import('./store.js')
  await store.initAuth()
  await store.loadRuns()
  await vi.waitFor(() => expect(store.getState().runs.a.events).toHaveLength(3))
  fetch.mockClear()
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('首次打开没有旧阅读记录，已加载历史不算新事件', () => {
  expect(store.readingOf('a')).toBeNull()
  expect(store.pendingEventCount('a')).toBe(0)
  save('a', reading({ follow: true, seenSeq: null }))
  expect(store.pendingEventCount('a')).toBe(0)
  save('a', reading({ follow: true }))
  expect(store.pendingEventCount('a')).toBe(0)
})

it('每条会话的模式、锚点、展开与内部日志位置独立，像素写入不通知工作台或落盘', () => {
  const first = reading(), second = reading({ follow: true, anchor: null, seenSeq: null, details: {} })
  save('a', first)
  save('b', second)
  const notifications = []
  const unsubscribe = store.subscribe(() => notifications.push(store.getState()))
  localStorage.setItem.mockClear()
  const moved = { ...first, anchor: { ...first.anchor, offset: -51 } }
  save('a', moved)
  unsubscribe()
  expect(store.readingOf('a')).toEqual(moved)
  expect(store.readingOf('b')).toEqual(second)
  expect(notifications).toEqual([])
  expect(localStorage.setItem).not.toHaveBeenCalled()
})

it('切会话、打开文件、关标签再重开保留阅读，控制面和单全局流不变', async () => {
  const value = reading()
  save('a', value)
  store.selectRun('b')
  store.selectRun('a')
  await store.openArtifact('deploy/report.rpm', { name: 'report.rpm', binary: true })
  expect(store.controlRunId()).toBe('a')
  expect(store.readingOf('a')).toEqual(value)
  store.closeTab('session:a')
  expect(store.getState().tabs.some(tab => tab.runId === 'a')).toBe(false)
  expect(store.readingOf('a')).toEqual(value)
  store.selectRun('a')
  expect(store.readingOf('a')).toEqual(value)
  expect(store.controlRunId()).toBe('a')
  expect(sources.filter(source => !source.closed)).toHaveLength(1)
  expect(fetch.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
})

it('Fork 新会话不继承源阅读记录，源记录仍保留', async () => {
  const value = reading()
  save('a', value)
  fetch.mockImplementation(url => url === '/api/runs/a/clone'
    ? Promise.resolve(response({ run_id: 'fork-a', status: 'READY', resumed_from: 'a' }))
    : fetchResponse(url))
  await store.cloneRun()
  expect(store.controlRunId()).toBe('fork-a')
  expect(store.readingOf('fork-a')).toBeNull()
  expect(store.pendingEventCount('fork-a')).toBe(0)
  expect(store.readingOf('a')).toEqual(value)
})

it('只有有效完整清单确认删除才剪枝，失败或不完整响应保留阅读', async () => {
  const value = reading()
  save('a', value)
  fetch.mockRejectedValue(new Error('offline'))
  await store.loadRuns()
  expect(store.readingOf('a')).toEqual(value)
  fetch.mockResolvedValue(response({}))
  await store.loadRuns()
  expect(store.readingOf('a')).toEqual(value)
  summaries = summaries.filter(run => run.run_id !== 'a')
  fetch.mockImplementation(fetchResponse)
  await store.loadRuns()
  expect(store.readingOf('a')).toBeNull()
  save('a', value)
  expect(store.readingOf('a')).toBeNull()
})

it('刷新模块仅恢复草稿，不恢复阅读模式、展开或水位', async () => {
  save('a', reading())
  store.setDraft('a', '保留正文\n保留换行')
  const confirming = deferred()
  fetch.mockImplementation(async url => {
    if (url === '/api/auth/me') await confirming.promise
    return fetchResponse(url)
  })
  vi.clearAllTimers()
  vi.resetModules()
  const restored = await import('./store.js')
  expect(restored.readingOf('a')).toBeNull()
  expect(restored.getState().auth).toBe('checking')
  expect(restored.draftOf('a')).toBe('')
  confirming.resolve()
  await restored.initAuth()
  await restored.loadRuns()
  expect(restored.readingOf('a')).toBeNull()
  expect(restored.pendingEventCount('a')).toBe(0)
  expect(restored.draftOf('a')).toBe('保留正文\n保留换行')
})

it('[race] 重复 SSE 和补入旧历史不虚增新事件，水位不替代快照游标或改写执行事实', async () => {
  save('a', reading())
  broadcast('a', event(12, 'user.message', { text: '继续' }))
  broadcast('a', event(15, 'turn.completed', { result: '完成' }))
  broadcast('a', event(15, 'turn.completed', { result: '重复帧' }))
  expect(store.pendingEventCount('a')).toBe(2)
  const oldHistory = deferred()
  fetch.mockImplementation(url => url === '/api/runs/a/events' ? oldHistory.promise : fetchResponse(url))
  sources.at(-1).onopen()
  expect(fetch.mock.calls.filter(([url]) => url === '/api/runs/a/events').at(-1)[1].headers['Last-Event-ID']).toBe('15')
  oldHistory.resolve(snapshot([event(2), event(3), event(4), event(8), event(12, 'user.message', { text: '继续' }), event(15, 'turn.completed', { result: '完成' })]))
  await vi.waitFor(() => expect(store.getState().runs.a.events).toHaveLength(7))
  expect(store.pendingEventCount('a')).toBe(2)
  const before = store.getState().runs.a
  const seconds = activeSeconds(before, 200_000)
  save('a', reading({ follow: true, seenSeq: 15 }))
  expect(store.pendingEventCount('a')).toBe(0)
  expect(store.getState().runs.a).toEqual(before)
  expect(activeSeconds(store.getState().runs.a, 200_000)).toBe(seconds)
  expect(seconds).toBe(30)
  expect(before.status).toBe('READY')
  expect(before.lastEventAt).toBe(150_000)
  history.a = []
  fetch.mockImplementation(fetchResponse)
  sources.at(-1).onopen()
  expect(fetch.mock.calls.filter(([url]) => url === '/api/runs/a/events').at(-1)[1].headers['Last-Event-ID']).toBe('15')
  broadcast('a', event(16))
  expect(store.pendingEventCount('a')).toBe(1)
})

it('[race] 关标签期间通过重开快照补入的新事件按原水位计数', async () => {
  save('a', reading())
  store.selectRun('b')
  store.closeTab('session:a')
  broadcast('a', event(9))
  expect(store.pendingEventCount('a')).toBe(0)
  history.a = [event(9), event(10)]
  store.selectRun('a')
  await vi.waitFor(() => expect(store.pendingEventCount('a')).toBe(2))
  expect(store.readingOf('a')).toEqual(reading())
})

it('[race] 暂无历史时的待恢复记录不被迟到快照覆盖', async () => {
  const pending = deferred()
  fetch.mockImplementation(url => url === '/api/runs/b/events' ? pending.promise : fetchResponse(url))
  store.selectRun('b')
  save('b', reading({ anchor: { seq: 6, offset: -19 }, seenSeq: 7 }))
  expect(store.getState().runs.b.events).toEqual([])
  const retained = store.readingOf('b')
  pending.resolve(snapshot([event(1), event(6), event(7), event(8)]))
  await vi.waitFor(() => expect(store.getState().runs.b.events).toHaveLength(4))
  expect(store.readingOf('b')).toEqual(retained)
  expect(store.pendingEventCount('b')).toBe(1)
})

it.each(['logout', 'change-user', 'password-change', 'expired'])('身份清理 %s 清除阅读，旧视图回调无法写回', async action => {
  const epoch = store.getState().identityEpoch
  const value = reading()
  save('a', value)
  if (action === 'logout') await store.logout()
  if (action === 'change-user') {
    identity = { username: 'bob' }
    await store.login('bob', 'password')
    await store.loadRuns()
  }
  if (action === 'password-change') {
    identity = { username: 'alice', must_change_password: true, user_version: 2 }
    await store.initAuth()
  }
  if (action === 'expired') {
    fetch.mockResolvedValue(response({ detail: 'expired' }, 401))
    await store.loadRuns()
  }
  expect(store.readingOf('a')).toBeNull()
  store.saveReading('a', value, epoch)
  expect(store.readingOf('a')).toBeNull()
})

it('[race] 同名重新登录后旧阅读回调不能覆盖新登录记录', async () => {
  const epoch = store.getState().identityEpoch
  const old = reading()
  save('a', old)
  await store.logout()
  await store.login('alice', 'password')
  await store.loadRuns()
  expect(store.readingOf('a')).toBeNull()
  const current = reading({ follow: true, anchor: null, seenSeq: 8, details: {} })
  save('a', current)
  store.saveReading('a', old, epoch)
  expect(store.readingOf('a')).toEqual(current)
})

it('未认证或会话不存在时不能保存阅读记录', async () => {
  save('missing', reading())
  expect(store.readingOf('missing')).toBeNull()
  await store.logout()
  save('a', reading())
  expect(store.readingOf('a')).toBeNull()
  expect(store.pendingEventCount('a')).toBe(0)
})
