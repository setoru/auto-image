import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const response = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { 'Content-Type': 'application/json' },
})
const snapshot = (text = '') => new Response(text, { headers: { 'Content-Type': 'text/event-stream' } })
let store, sources
const summary = (runId, status = 'READY') => ({ run_id: runId, status })
function deferred() {
  let resolve, reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
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
    close() {}
  })
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (url === '/api/auth/me') return response({ username: 'alice' })
    if (url === '/api/runs') return response({ runs: [summary('a'), summary('b')] })
    if (url.endsWith('/events')) return snapshot()
    return response({})
  }))
  store = await import('./store.js')
  await store.initAuth()
  await store.loadRuns()
  store.setDraft('a', '部署 nginx\n核对版本')
  fetch.mockClear()
})

it('[race] 同会话在途只提交一次，其他会话可独立提交；关标签不撤销或重开', async () => {
  const pendingA = deferred(), pendingB = deferred()
  fetch.mockImplementation(url => url.includes('/a/') ? pendingA.promise : pendingB.promise)
  const first = store.send(store.draftOf('a'))
  expect(store.submissionOf('a').status).toBe('sending')
  expect(await store.send('重复')).toBe(false)
  store.selectRun('b')
  store.setDraft('b', '部署 redis')
  const second = store.send(store.draftOf('b'))
  store.closeTab('session:a')
  expect(fetch.mock.calls.filter(([url]) => url.endsWith('/messages'))).toHaveLength(2)
  pendingA.resolve(response({ run_id: 'a', status: 'RUNNING' }))
  await first
  expect(store.getState().activeKey).toBe('session:b')
  expect(store.getState().tabs.some(tab => tab.runId === 'a')).toBe(false)
  expect(store.submissionOf('b').status).toBe('sending')
  expect(store.draftOf('b')).toBe('部署 redis')
  pendingB.reject(new Error('connection lost'))
  await second
  expect(store.submissionOf('a').status).toBe('accepted')
  expect(store.submissionOf('b').status).toBe('unknown')
})

it.each(['新编辑', '部署 nginx\n核对版本'])('[race] 后来编辑为 %s，旧成功回执不得清稿', async text => {
  const pending = deferred()
  fetch.mockReturnValue(pending.promise)
  const sending = store.send(store.draftOf('a'))
  store.setDraft('a', '中间修改')
  store.setDraft('a', text)
  pending.resolve(response({ run_id: 'a', status: 'RUNNING' }))
  await sending
  expect(store.draftOf('a')).toBe(text)
  expect(JSON.parse(fetch.mock.calls[0][1].body).text).toBe('部署 nginx\n核对版本')
})

it('[race] 完成事件先到，迟到接收回执不能回退运行状态', async () => {
  const pending = deferred()
  fetch.mockImplementation(url => url.endsWith('/messages') ? pending.promise : Promise.resolve(response({})))
  const sending = store.send(store.draftOf('a'))
  for (const [seq, type] of [[1, 'turn.started'], [2, 'turn.completed']]) {
    sources.at(-1).handlers[type]({ data: JSON.stringify({ run_id: 'a', seq, ts: seq, type, payload: {} }) })
  }
  expect(store.getState().runs.a.status).toBe('READY')
  pending.resolve(response({ run_id: 'a', status: 'RUNNING' }))
  await sending
  expect(store.getState().runs.a.status).toBe('READY')
  expect(store.submissionOf('a').status).toBe('accepted')
})

it('[race] 登出同名重登后，旧回执和旧编辑回调不能写回', async () => {
  const epoch = store.getState().identityEpoch
  const pending = deferred()
  fetch.mockReturnValue(pending.promise)
  const sending = store.send(store.draftOf('a'))
  fetch.mockImplementation(async url => url === '/api/runs'
    ? response({ runs: [summary('a')] }) : response({ username: 'alice' }))
  await store.logout()
  await store.login('alice', 'password')
  await store.loadRuns()
  store.setDraft('a', '新登录的草稿')
  store.setDraft('a', '旧组件回调', epoch)
  pending.resolve(response({ run_id: 'a', status: 'RUNNING' }))
  await sending
  expect(store.draftOf('a')).toBe('新登录的草稿')
  expect(store.submissionOf('a')).toBeNull()
})

it('核对只读，读取到同文及完成事件也保留未确认；收起不清稿', async () => {
  fetch.mockRejectedValue(new Error('response lost'))
  await store.send(store.draftOf('a'))
  const pending = deferred()
  fetch.mockImplementation(url => url === '/api/runs'
    ? Promise.resolve(response({ runs: [summary('a'), summary('b')] })) : pending.promise)
  fetch.mockClear()
  const refreshing = store.refreshSubmission('a')
  expect(store.submissionOf('a').refresh).toBe('loading')
  pending.resolve(snapshot('id: 1\nevent: user.message\ndata: {"text":"部署 nginx\\n核对版本","ts":1}\n\nid: 2\nevent: turn.completed\ndata: {"ts":2}\n\n'))
  await refreshing
  expect(store.submissionOf('a')).toMatchObject({ status: 'unknown', refresh: 'success' })
  expect(store.getState().runs.a.events).toHaveLength(2)
  expect(store.draftOf('a')).toBe('部署 nginx\n核对版本')
  expect(fetch.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
  expect(sources).toHaveLength(1)
  store.dismissSubmission('a')
  expect(store.submissionOf('a')).toBeNull()
  expect(store.draftOf('a')).toBe('部署 nginx\n核对版本')
})

it('核对失败保留提示，用户随后主动发送为新请求', async () => {
  fetch.mockRejectedValue(new Error('offline'))
  await store.send(store.draftOf('a'))
  await store.refreshSubmission('a')
  expect(store.submissionOf('a')).toMatchObject({ status: 'unknown', refresh: 'error' })
  store.setDraft('a', '改后的指令')
  fetch.mockResolvedValue(response({ run_id: 'a', status: 'RUNNING' }))
  await store.send(store.draftOf('a'))
  expect(store.submissionOf('a')).toMatchObject({ status: 'accepted', text: '改后的指令' })
})

it('[race] 核对读取迟到不能改写替换后的新提交', async () => {
  fetch.mockRejectedValue(new Error('offline'))
  await store.send(store.draftOf('a'))
  const pending = deferred()
  fetch.mockReturnValue(pending.promise)
  const checking = store.refreshSubmission('a')
  fetch.mockResolvedValue(response({ run_id: 'a', status: 'RUNNING' }))
  await store.send('第二次主动提交')
  pending.reject(new Error('read failed'))
  await checking
  expect(store.submissionOf('a')).toMatchObject({ status: 'accepted', text: '第二次主动提交' })
  expect(store.submissionOf('a').refresh).toBeUndefined()
})

it('刚编辑后重载模块，认证前不恢复，认证后保留换行与未确认原提交', async () => {
  fetch.mockRejectedValue(new Error('lost response'))
  await store.send(store.draftOf('a'))
  store.setDraft('a', '后来编辑\n第二行')
  const identity = deferred()
  fetch.mockImplementation(async url => {
    if (url === '/api/auth/me') { await identity.promise; return response({ username: 'alice' }) }
    if (url === '/api/runs') return response({ runs: [summary('a'), summary('b')] })
    return new Response('')
  })
  vi.clearAllTimers()
  vi.resetModules()
  const restored = await import('./store.js')
  expect(restored.getState().auth).toBe('checking')
  expect(restored.draftOf('a')).toBe('')
  identity.resolve()
  await restored.initAuth()
  expect(restored.draftOf('a')).toBe('后来编辑\n第二行')
  expect(restored.submissionOf('a')).toMatchObject({ status: 'unknown', text: '部署 nginx\n核对版本' })
  restored.setDraft('a', '恢复后的新输入')
  await restored.initAuth()
  expect(restored.draftOf('a')).toBe('恢复后的新输入')
})

it('刷新时残留发送中恢复成未确认，恢复不自动提交', async () => {
  const pending = deferred()
  fetch.mockReturnValue(pending.promise)
  const sending = store.send(store.draftOf('a'))
  fetch.mockImplementation(async url => url === '/api/auth/me' ? response({ username: 'alice' })
    : url === '/api/runs' ? response({ runs: [summary('a')] }) : new Response(''))
  fetch.mockClear()
  vi.clearAllTimers()
  vi.resetModules()
  const restored = await import('./store.js')
  await restored.initAuth()
  expect(restored.submissionOf('a')).toMatchObject({ status: 'unknown', text: '部署 nginx\n核对版本' })
  expect(fetch.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
  pending.reject(new Error('page gone'))
  await sending
})

it('清空正文仍恢复未确认信息，收起后空记录消失', async () => {
  fetch.mockRejectedValue(new Error('lost'))
  await store.send(store.draftOf('a'))
  store.clearDraft('a')
  fetch.mockImplementation(async url => url === '/api/auth/me' ? response({ username: 'alice' })
    : url === '/api/runs' ? response({ runs: [summary('a')] }) : new Response(''))
  vi.clearAllTimers()
  vi.resetModules()
  let restored = await import('./store.js')
  await restored.initAuth()
  expect(restored.draftOf('a')).toBe('')
  expect(restored.submissionOf('a').status).toBe('unknown')
  restored.dismissSubmission('a')
  vi.clearAllTimers()
  vi.resetModules()
  restored = await import('./store.js')
  await restored.initAuth()
  expect(restored.draftOf('a')).toBe('')
  expect(restored.submissionOf('a')).toBeNull()
})

it('可信接收后刷新不会复活已清草稿或接收提示', async () => {
  fetch.mockResolvedValue(response({ run_id: 'a', status: 'RUNNING' }))
  await store.send(store.draftOf('a'))
  fetch.mockImplementation(async url => url === '/api/auth/me' ? response({ username: 'alice' })
    : url === '/api/runs' ? response({ runs: [summary('a')] }) : new Response(''))
  vi.clearAllTimers()
  vi.resetModules()
  const restored = await import('./store.js')
  await restored.initAuth()
  expect(restored.draftOf('a')).toBe('')
  expect(restored.submissionOf('a')).toBeNull()
})

it.each(['logout', 'switch', '401', 'password-change'])('身份清理 %s 后重新登录不能恢复旧记录', async action => {
  fetch.mockImplementation(async url => url === '/api/auth/me'
    ? response(action === 'switch' ? { username: 'bob' }
      : action === 'password-change' ? { username: 'alice', must_change_password: true } : { username: 'alice' }, action === '401' ? 401 : 200)
    : url === '/api/runs' ? response({ runs: [summary('a')] }) : response({}))
  if (action === 'logout') await store.logout()
  else await store.initAuth()
  expect(store.draftOf('a')).toBe('')
  fetch.mockImplementation(async url => url === '/api/auth/me' ? response({ username: 'alice' })
    : url === '/api/runs' ? response({ runs: [summary('a')] }) : response({}))
  vi.clearAllTimers()
  vi.resetModules()
  const restored = await import('./store.js')
  await restored.initAuth()
  expect(restored.draftOf('a')).toBe('')
  expect(restored.submissionOf('a')).toBeNull()
})

it('存储失败仍能编辑和发送，并公布恢复不可用', async () => {
  localStorage.setItem = () => { throw new Error('quota') }
  store.setDraft('a', '仍可发送')
  expect(store.draftOf('a')).toBe('仍可发送')
  expect(store.getState().draftStorageError).toBe(true)
  fetch.mockResolvedValue(response({ run_id: 'a', status: 'RUNNING' }))
  expect(await store.send(store.draftOf('a'))).toBe(true)
  expect(store.draftOf('a')).toBe('')
})

it('[race] 完整清单确认删除才剪枝，已删除会话的旧回执不复活数据', async () => {
  const pending = deferred()
  fetch.mockReturnValue(pending.promise)
  const sending = store.send(store.draftOf('a'))
  fetch.mockRejectedValue(new Error('read failed'))
  await store.loadRuns()
  expect(store.draftOf('a')).toBe('部署 nginx\n核对版本')
  fetch.mockResolvedValue(response({}))
  await store.loadRuns()
  expect(store.draftOf('a')).toBe('部署 nginx\n核对版本')
  fetch.mockResolvedValue(response({ runs: [] }))
  await store.loadRuns()
  expect(store.draftOf('a')).toBe('')
  expect(store.submissionOf('a')).toBeNull()
  expect(store.getState().runs.a).toBeUndefined()
  pending.resolve(response({ run_id: 'a', status: 'RUNNING' }))
  await sending
  expect(store.draftOf('a')).toBe('')
  expect(store.submissionOf('a')).toBeNull()
})

it('结束后仍保留草稿，Fork 从空草稿和无提交提示开始', async () => {
  fetch.mockResolvedValue(response({ runs: [summary('a', 'ENDED'), summary('b')] }))
  await store.loadRuns()
  expect(store.draftOf('a')).toBe('部署 nginx\n核对版本')
  fetch.mockImplementation(async url => url.endsWith('/clone')
    ? response({ run_id: 'fork', status: 'READY' }) : new Response(''))
  await store.cloneRun()
  expect(store.draftOf('fork')).toBe('')
  expect(store.submissionOf('fork')).toBeNull()
  expect(store.draftOf('a')).toBe('部署 nginx\n核对版本')
})

it('[race] 创建前发出的旧清单不能删除新会话或其草稿', async () => {
  const pending = deferred()
  fetch.mockReturnValue(pending.promise)
  const listing = store.loadRuns()
  fetch.mockImplementation(async url => url === '/api/runs'
    ? response({ run_id: 'new', status: 'READY' }) : new Response(''))
  await store.createRun()
  store.setDraft('new', '新会话草稿')
  pending.resolve(response({ runs: [summary('a'), summary('b')] }))
  await listing
  expect(store.getState().runs.new.status).toBe('READY')
  expect(store.draftOf('new')).toBe('新会话草稿')
  expect(store.controlRunId()).toBe('new')
})

it.each([401, 403])('发送响应表明访问资格失效 %s 时清理草稿与提示', async status => {
  fetch.mockResolvedValue(response({ detail: status === 401 ? 'session expired' : 'password_change_required' }, status))
  expect(await store.send(store.draftOf('a'))).toBe(false)
  expect(store.getState().auth).toBe('anonymous')
  expect(store.draftOf('a')).toBe('')
  expect(store.submissionOf('a')).toBeNull()
})

it('模块重载后另一身份不能恢复先前用户的本机记录', async () => {
  fetch.mockImplementation(async url => url === '/api/auth/me' ? response({ username: 'bob' })
    : url === '/api/runs' ? response({ runs: [summary('a')] }) : new Response(''))
  vi.clearAllTimers()
  vi.resetModules()
  const restored = await import('./store.js')
  await restored.initAuth()
  expect(restored.draftOf('a')).toBe('')
  expect(restored.submissionOf('a')).toBeNull()
})

it('本机读取失败也进入可用工作台，并公布无法恢复', async () => {
  localStorage.getItem = () => { throw new Error('storage disabled') }
  fetch.mockImplementation(async url => url === '/api/auth/me' ? response({ username: 'alice' })
    : url === '/api/runs' ? response({ runs: [summary('a')] }) : new Response(''))
  vi.clearAllTimers()
  vi.resetModules()
  const restored = await import('./store.js')
  await restored.initAuth()
  expect(restored.getState().auth).toBe('user')
  expect(restored.getState().draftStorageError).toBe(true)
  expect(restored.draftOf('a')).toBe('')
})

it('后台完整清单删除当前会话后，控制面回到仍存在的会话', async () => {
  fetch.mockImplementation(async url => url === '/api/runs' ? response({ runs: [summary('b')] })
    : url.endsWith('/events') ? snapshot() : response({}))
  await vi.advanceTimersByTimeAsync(5000)
  expect(store.getState().runs.a).toBeUndefined()
  expect(store.getState().tabs).toEqual([{ kind: 'session', runId: 'b' }])
  expect(store.controlRunId()).toBe('b')
})

it('快照返回 HTTP 200 HTML 时核对失败，不能假报记录已刷新', async () => {
  fetch.mockRejectedValue(new Error('response lost'))
  await store.send(store.draftOf('a'))
  fetch.mockImplementation(async url => url === '/api/runs' ? response({ runs: [summary('a')] })
    : new Response('<html>proxy login</html>', { headers: { 'Content-Type': 'text/html' } }))
  await store.refreshSubmission('a')
  expect(store.submissionOf('a')).toMatchObject({ status: 'unknown', refresh: 'error' })
  expect(store.draftOf('a')).toBe('部署 nginx\n核对版本')
})

it.each([401, 403])('核对读取确认访问失效 %s 时统一清理身份、草稿和本机记录', async status => {
  fetch.mockRejectedValue(new Error('response lost'))
  await store.send(store.draftOf('a'))
  fetch.mockImplementation(async () => response({ detail: status === 401 ? 'session expired' : 'password_change_required' }, status))
  await store.refreshSubmission('a')
  expect(store.getState().auth).toBe('anonymous')
  expect(store.draftOf('a')).toBe('')
  expect(store.submissionOf('a')).toBeNull()
  expect(localStorage.getItem('va-session-drafts')).toBeNull()
})

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

it('收到受理回执后清除本次草稿，接收反馈不冒充回合运行状态', async () => {
  fetch.mockResolvedValue(response({ run_id: 'a', status: 'RUNNING' }))
  expect(await store.send(store.draftOf('a'))).toBe(true)
  expect(store.submissionOf('a').status).toBe('accepted')
  expect(store.draftOf('a')).toBe('')
  expect(store.getState().runs.a.status).toBe('READY')
  expect(fetch.mock.calls.map(([url, init]) => [url, JSON.parse(init.body)])).toEqual([
    ['/api/runs/a/messages', { text: '部署 nginx\n核对版本' }],
  ])
})

it.each([
  [403, { detail: 'origin forbidden' }, 'origin forbidden'],
  [404, { detail: 'run not found' }, 'run not found'],
  [409, { detail: 'parallel_limit_reached' }, '执行中回合已达并发上限'],
  [422, { detail: 'text required' }, 'text required'],
  [422, { detail: [{ loc: ['body', 'text'], msg: 'Field required', type: 'missing' }] }, 'Field required'],
  ...['audit unavailable', 'state persist unavailable', 'state unavailable (restricted recovery)']
    .map(detail => [503, { detail }, detail]),
])('前置拒绝 %s/%j 保留草稿并在本会话说明原因', async (status, data, hint) => {
  fetch.mockResolvedValue(response(data, status))
  expect(await store.send(store.draftOf('a'))).toBe(false)
  expect(store.submissionOf('a')).toMatchObject({ status: 'rejected', message: expect.stringContaining(hint) })
  expect(store.draftOf('a')).toBe('部署 nginx\n核对版本')
  expect(store.getState().submitError).toBeNull()
})

it.each([
  [200, '{}'], [200, 'null'], [200, '[]'], [200, ''], [200, '{broken'],
  [200, '<html>proxy</html>'], [200, '{"run_id":"b","status":"RUNNING"}'],
  [200, '{"run_id":"a","status":"READY"}'], [201, '{"run_id":"a","status":"RUNNING"}'],
  [401, '<html>proxy</html>'], [403, '{}'], [422, '{"detail":{}}'],
  [500, '{"detail":"internal failure"}'], [503, '{"detail":"other failure"}'],
])('无法识别的响应 %s/%s 保留未确认，不能猜成未执行', async (status, body) => {
  fetch.mockResolvedValue(new Response(body, { status }))
  expect(await store.send(store.draftOf('a'))).toBe(false)
  expect(store.submissionOf('a').status).toBe('unknown')
  expect(store.draftOf('a')).toBe('部署 nginx\n核对版本')
  expect(store.getState().auth).toBe('user')
})
