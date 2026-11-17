import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

// 静态展示读取真实状态入口；复选框交互、焦点与菜单由隔离浏览器验收。
vi.mock('react', async importOriginal => ({
  ...await importOriginal(),
  useSyncExternalStore: (_subscribe, getSnapshot) => getSnapshot(),
}))

const response = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { 'Content-Type': 'application/json' },
})
const nginx = 'deploy/nginx/1.25.3/nginx-report.md'
const redis = 'deploy/redis/7.2/redis-report.md'
const file = name => ({ name, size: 12, stage: null })
let store, SidePanel, groups, identity, sources

async function fetchResponse(url) {
  if (url === '/api/auth/me' || url === '/api/auth/login') return response(identity)
  if (url === '/api/runs') return response({ runs: [
    { run_id: 'run-a', title: 'Nginx 会话', status: 'READY' },
    { run_id: 'run-b', title: 'Redis 会话', status: 'READY' },
  ] })
  if (url === '/api/artifacts') return response({ groups })
  if (url.endsWith('/events')) return new Response('', { headers: { 'Content-Type': 'text/event-stream' } })
  if (url.startsWith('/api/artifacts/file/')) {
    const rel = url.slice('/api/artifacts/file/'.length)
    return response({ dir: rel.slice(0, rel.lastIndexOf('/')), name: rel.split('/').at(-1), content: '只在正文出现 BodyOnly' })
  }
  return response({})
}
const markup = () => renderToStaticMarkup(<SidePanel />)
const filePaths = () => [...markup().matchAll(/<div[^>]+class="va-art-item(?: on)?"[^>]+title="([^"]+)"/g)].map(match => match[1])
const checkedFiles = () => [...markup().matchAll(/<div[^>]+class="va-art-item(?: on)?"[^>]+title="([^"]+)"><input([^>]*)/g)]
  .filter(match => match[2].includes('checked=""')).map(match => match[1])
const exportButton = () => markup().match(/<button[^>]+class="va-art-zip"[^>]*>[^<]*<\/button>/)?.[0]
function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
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
    constructor() { sources.push(this) }
    addEventListener() {}
    close() { this.closed = true }
  })
  groups = [
    { dir: 'deploy/nginx/1.25.3', files: [file('nginx-report.md'), file('z-last.log')] },
    { dir: 'deploy/redis/7.2', files: [file('redis-report.md')] },
    { dir: 'deploy/nginx/1.24.0', files: [file('nginx-old-report.md')] },
    { dir: 'rpm/nginx/1.25.3/rpms', files: [{ ...file('Nginx-1.25.3.RPM'), binary: true }] },
  ]
  identity = { username: 'alice' }
  vi.stubGlobal('fetch', vi.fn(fetchResponse))
  store = await import('./store.js')
  SidePanel = (await import('./components/SidePanel.jsx')).default
  await store.initAuth()
  await store.loadRuns()
  await store.refreshArtifacts()
  store.setSidePanel('artifacts')
  fetch.mockClear()
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('完整路径按去首尾空白和忽略大小写匹配，并保留全部祖先与计数', () => {
  store.setArtifactKeyword('  RPM/NGINX/1.25.3/RPMS/NGINX-1.25.3.rpm  ')
  expect(filePaths()).toEqual(['rpm/nginx/1.25.3/rpms/Nginx-1.25.3.RPM'])
  for (const path of ['rpm', 'rpm/nginx', 'rpm/nginx/1.25.3', 'rpm/nginx/1.25.3/rpms']) {
    expect(markup()).toContain(`title="${path}"`)
  }
  expect([...markup().matchAll(/class="va-art-count"[^>]*>(\d+)</g)].map(match => Number(match[1]))).toEqual([1, 1, 1, 1])
  expect(fetch.mock.calls).toEqual([])
})

it.each([
  ['nginx-report.md', [nginx]],
  ['  ReDiS  ', [redis]],
  ['1.24.0', ['deploy/nginx/1.24.0/nginx-old-report.md']],
  ['.*', []],
  ['[1.25.3]', []],
  ['nginx redis', []],
])('文件、软件和版本路径只做连续字面子串匹配：%s', (keyword, expected) => {
  store.setArtifactKeyword(keyword)
  expect(filePaths()).toEqual(expected)
  expect(fetch.mock.calls).toEqual([])
})

it('保留目录与文件原排序，计数覆盖每个匹配子树', () => {
  store.setArtifactKeyword('report')
  expect(filePaths()).toEqual([nginx, 'deploy/nginx/1.24.0/nginx-old-report.md', redis])
  expect([...markup().matchAll(/class="va-art-count"[^>]*>(\d+)</g)].map(match => Number(match[1])))
    .toEqual([3, 2, 1, 1, 1, 1])
  store.setArtifactKeyword('deploy/nginx/1.25.3')
  expect(filePaths()).toEqual([nginx, 'deploy/nginx/1.25.3/z-last.log'])
})

it('过滤掉较新的组后，交错出现的软件目录仍保持完整树中的顺序', async () => {
  groups = [
    { dir: 'deploy/nginx/1.26.0', files: [file('new.log')] },
    { dir: 'deploy/redis/7.2', files: [file('redis-report.md')] },
    { dir: 'deploy/nginx/1.25.3', files: [file('nginx-report.md')] },
  ]
  await store.refreshArtifacts()
  for (const path of ['deploy/nginx/1.25.3', 'deploy/redis', 'deploy/redis/7.2']) {
    store.setArtifactDirOpen(path, true)
  }
  const reports = filePaths().filter(path => path.endsWith('report.md'))
  expect(reports).toEqual([nginx, redis])
  store.setArtifactKeyword('report')
  expect(filePaths()).toEqual(reports)
  expect(markup()).not.toContain('title="deploy/nginx/1.26.0"')
})

it('查询自动展开匹配祖先，折叠只影响展示；改词重开，清词恢复普通目录状态', () => {
  store.setArtifactDirOpen('deploy/nginx/1.25.3', false)
  store.setArtifactDirOpen('deploy/redis', true)
  store.setArtifactDirOpen('deploy/redis/7.2', true)
  expect(filePaths()).toEqual([redis])
  store.setArtifactKeyword('report')
  expect(filePaths()).toEqual([nginx, 'deploy/nginx/1.24.0/nginx-old-report.md', redis])
  store.setArtifactDirOpen('deploy/nginx', false)
  expect(filePaths()).toEqual([redis])
  expect([...markup().matchAll(/class="va-art-count"[^>]*>(\d+)</g)].map(match => Number(match[1])))
    .toEqual([3, 2, 1, 1])
  store.setSidePanel('sessions')
  store.setSidePanel('artifacts')
  expect(filePaths()).toEqual([redis])
  store.setArtifactKeyword('report.md')
  expect(filePaths()).toEqual([nginx, 'deploy/nginx/1.24.0/nginx-old-report.md', redis])
  store.setArtifactKeyword('')
  expect(filePaths()).toEqual([redis])
  store.setArtifactKeyword('   ')
  expect(filePaths()).toEqual([redis])
})

it('零匹配与暂无数据分别提示，清除后恢复原清单', async () => {
  store.setArtifactKeyword('missing')
  expect(filePaths()).toEqual([])
  expect(markup()).toContain('未找到相关产物')
  expect(markup()).not.toContain('暂无产物')
  store.setArtifactKeyword('')
  expect(filePaths()).toEqual([nginx, 'deploy/nginx/1.25.3/z-last.log'])
  groups = []
  await store.refreshArtifacts()
  expect(markup()).toContain('暂无产物')
  expect(filePaths()).toEqual([])
  expect(markup()).not.toContain('未找到相关产物')
})

it('筛选不搜索已开正文、OBS 对象或清单外路径，也不改工作现场', async () => {
  await store.openArtifact(nginx)
  store.setDraft('run-a', '继续核对\n保留草稿')
  const reading = { follow: false, anchor: { seq: 3, offset: -24 }, seenSeq: 8, details: {} }
  store.saveReading('run-a', reading, store.getState().identityEpoch)
  store.setSessionKeyword('Redis')
  store.setArtifactSel([nginx, redis], true)
  fetch.mockImplementation(url => url.startsWith('/api/obs/objects')
    ? response({ objects: [{ key: 'hidden/ObsOnly.md', size: 12 }] }) : fetchResponse(url))
  await store.refreshObs()
  fetch.mockClear()
  const before = store.getState()
  for (const keyword of ['BodyOnly', 'ObsOnly', 'market-doc', 'nginx-report']) {
    store.setArtifactKeyword(keyword)
    expect(filePaths()).toEqual(keyword === 'nginx-report' ? [nginx] : [])
  }
  const after = store.getState()
  for (const field of ['artifacts', 'artifactCache', 'artifactSel', 'runs', 'order', 'tabs', 'activeKey', 'lastSessionKey', 'drafts', 'sessionKeyword']) {
    expect(after[field]).toBe(before[field])
  }
  expect(store.readingOf('run-a')).toEqual(reading)
  expect(store.controlRunId()).toBe('run-a')
  expect(sources.filter(source => !source.closed)).toHaveLength(1)
  expect(fetch.mock.calls).toEqual([])
})

it('预选 nginx 与 redis 后仅显示 nginx，零匹配与折叠均保留全局选择和可用导出', async () => {
  groups = [
    { dir: 'deploy/nginx/1.25.3', files: [file('nginx-report.md')] },
    { dir: 'deploy/redis/7.2', files: [file('redis-report.md')] },
  ]
  await store.refreshArtifacts()
  store.setArtifactSel([nginx, redis], true)
  store.setArtifactKeyword('nginx')
  expect(markup()).toContain('已选 2，其中 1 项不在当前筛选结果')
  expect(checkedFiles()).toEqual([nginx])
  store.setArtifactDirOpen('deploy', false)
  expect(filePaths()).toEqual([])
  expect(markup()).toContain('已选 2，其中 1 项不在当前筛选结果')
  store.setArtifactKeyword('nothing')
  expect(filePaths()).toEqual([])
  expect(markup()).toContain('已选 2，其中 2 项不在当前筛选结果')
  expect(exportButton()).toContain('全部已选 (2)')
  expect(exportButton()).not.toContain('disabled')
  store.setArtifactKeyword('')
  store.setArtifactDirOpen('deploy/redis', true)
  store.setArtifactDirOpen('deploy/redis/7.2', true)
  expect(checkedFiles()).toEqual([nginx, redis])
  store.setArtifactKeyword('nginx')
  store.clearArtifactSel()
  expect(store.getState().artifactSel).toEqual({})
  expect(exportButton()).toContain('disabled')
})

it('批量匹配选择只增加或移除指定文件，单文件操作与全清仍作用全局集合', () => {
  store.setArtifactSel([redis], true)
  store.setArtifactKeyword('nginx-report')
  store.setArtifactSel([nginx], true)
  expect(Object.keys(store.getState().artifactSel)).toEqual([redis, nginx])
  store.setArtifactSel([nginx], false)
  expect(Object.keys(store.getState().artifactSel)).toEqual([redis])
  store.toggleArtifactSel(nginx)
  expect(checkedFiles()).toEqual([nginx])
  store.toggleArtifactSel(nginx)
  expect(Object.keys(store.getState().artifactSel)).toEqual([redis])
  store.clearArtifactSel()
  expect(store.getState().artifactSel).toEqual({})
})

it.each([
  ['downloadArtifactZip', '/api/artifacts/zip'],
  ['archiveToObs', '/api/obs/archive'],
  ['archiveZipToObs', '/api/obs/archive-zip'],
])('%s 的实际请求在零匹配时仍包含全部已选，并保留既有在途约束', async (action, endpoint) => {
  const pending = deferred()
  vi.stubGlobal('document', { createElement: () => ({ click() {}, remove() {} }), body: { appendChild() {} } })
  vi.stubGlobal('URL', { createObjectURL: () => 'blob:artifacts', revokeObjectURL() {} })
  vi.stubGlobal('window', { prompt: () => 'selected-artifacts' })
  fetch.mockImplementation(url => url === endpoint ? pending.promise : fetchResponse(url))
  store.setArtifactSel([nginx, redis], true)
  store.setArtifactKeyword('nothing')
  const exporting = store[action]()
  expect(exportButton()).toContain('disabled')
  await store[action]()
  const calls = fetch.mock.calls.filter(([url]) => url === endpoint)
  expect(calls).toHaveLength(1)
  expect(calls[0][1].method).toBe('POST')
  expect(JSON.parse(calls[0][1].body)).toEqual(action === 'archiveZipToObs'
    ? { paths: [nginx, redis], name: 'selected-artifacts' }
    : { paths: [nginx, redis] })
  pending.resolve(action === 'downloadArtifactZip'
    ? new Response('zip content', { headers: { 'Content-Disposition': 'attachment; filename="artifacts.zip"' } })
    : response({ count: 2, zipped: 2, key: 'zip/selected-artifacts.zip', size: 24 }))
  await exporting
  expect(store.getState().submitError).toBeNull()
  expect(exportButton()).not.toContain('disabled')
  expect(Object.keys(store.getState().artifactSel)).toEqual([nginx, redis])
})

it('后台完整清单刷新继续应用原词，新增匹配文件自动出现并保留选择', async () => {
  store.setArtifactSel([nginx, redis], true)
  store.setArtifactKeyword('nginx')
  groups = [{ dir: 'deploy/nginx/1.26.0', files: [file('nginx-new-report.md')] }, ...groups]
  await store.refreshArtifacts()
  expect(store.getState().artifactKeyword).toBe('nginx')
  expect(filePaths()).toEqual([
    'deploy/nginx/1.26.0/nginx-new-report.md', nginx, 'deploy/nginx/1.25.3/z-last.log',
    'deploy/nginx/1.24.0/nginx-old-report.md', 'rpm/nginx/1.25.3/rpms/Nginx-1.25.3.RPM',
  ])
  expect(Object.keys(store.getState().artifactSel)).toEqual([nginx, redis])
})

it('读取失败不剪枝，只有完整清单确认删除才移除筛选外选择', async () => {
  store.setArtifactSel([nginx, redis], true)
  store.setArtifactKeyword('nginx-report')
  fetch.mockResolvedValueOnce(response({ detail: 'temporarily unavailable' }, 503))
  await store.refreshArtifacts()
  expect(Object.keys(store.getState().artifactSel)).toEqual([nginx, redis])
  groups = groups.filter(group => !group.dir.includes('redis'))
  await store.refreshArtifacts()
  expect(Object.keys(store.getState().artifactSel)).toEqual([nginx])
  expect(filePaths()).toEqual([nginx])
  expect(markup()).not.toContain('其中 1 项不在当前筛选结果')
  store.setArtifactKeyword('redis')
  expect(filePaths()).toEqual([])
  expect(Object.keys(store.getState().artifactSel)).toEqual([nginx])
})

it('两种关键词在本次登录内独立保留，刷新只恢复草稿', async () => {
  store.setArtifactKeyword('nginx')
  store.setSessionKeyword('Redis')
  store.setArtifactDirOpen('deploy', false)
  store.setDraft('run-a', '刷新仍保留正文')
  for (const panel of ['sessions', 'tasks', 'artifacts']) store.setSidePanel(panel)
  await store.initAuth()
  expect(store.getState().artifactKeyword).toBe('nginx')
  expect(store.getState().sessionKeyword).toBe('Redis')
  expect(filePaths()).toEqual(['rpm/nginx/1.25.3/rpms/Nginx-1.25.3.RPM'])
  vi.clearAllTimers()
  vi.resetModules()
  store = await import('./store.js')
  SidePanel = (await import('./components/SidePanel.jsx')).default
  await store.initAuth()
  await store.loadRuns()
  await store.refreshArtifacts()
  expect(store.getState().artifactKeyword).toBe('')
  expect(store.getState().sessionKeyword).toBe('')
  expect(store.draftOf('run-a')).toBe('刷新仍保留正文')
  expect(filePaths()).toEqual([nginx, 'deploy/nginx/1.25.3/z-last.log'])
})

it.each(['logout', 'change-user', 'password-change', 'expired', 'business-restricted'])(
  '身份清理 %s 同时清空关键词、目录现场与选择', async action => {
    store.setArtifactDirOpen('deploy', false)
    store.setArtifactKeyword('nginx')
    store.setArtifactDirOpen('rpm', false)
    store.setSessionKeyword('Redis')
    store.setArtifactSel([nginx, redis], true)
    if (action === 'logout') await store.logout()
    if (action === 'change-user') {
      identity = { username: 'bob' }
      await store.login('bob', 'password')
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
    expect(store.getState().artifactKeyword).toBe('')
    expect(store.getState().sessionKeyword).toBe('')
    expect(store.getState().artifactSel).toEqual({})
    fetch.mockImplementation(fetchResponse)
    identity = { username: 'alice' }
    await store.login('alice', 'password')
    await store.refreshArtifacts()
    expect(filePaths()).toEqual([nginx, 'deploy/nginx/1.25.3/z-last.log'])
    store.setArtifactKeyword('rpm')
    expect(filePaths()).toEqual(['rpm/nginx/1.25.3/rpms/Nginx-1.25.3.RPM'])
  },
)

it('[race] 旧身份迟到的产物清单不能恢复旧关键词、选择或覆盖新清单', async () => {
  store.setArtifactKeyword('nginx')
  store.setArtifactSel([nginx], true)
  const pending = deferred(), oldGroups = groups
  fetch.mockImplementation(url => url === '/api/artifacts' ? pending.promise : fetchResponse(url))
  const loading = store.refreshArtifacts()
  await store.logout()
  groups = [{ dir: 'deploy/redis/7.2', files: [file('redis-report.md')] }]
  fetch.mockImplementation(fetchResponse)
  await store.login('alice', 'password')
  await store.refreshArtifacts()
  store.setArtifactKeyword('redis')
  store.setArtifactSel([redis], true)
  pending.resolve(response({ groups: oldGroups }))
  await loading
  expect(store.getState().artifactKeyword).toBe('redis')
  expect(filePaths()).toEqual([redis])
  expect(store.getState().artifacts.groups).toEqual(groups)
  expect(Object.keys(store.getState().artifactSel)).toEqual([redis])
})

it('[race] 较早清单晚到时不恢复已删除的选择，当前查询仍对最新清单生效', async () => {
  store.setArtifactKeyword('report')
  store.setArtifactSel([nginx, redis], true)
  const pending = deferred(), oldGroups = groups
  fetch.mockImplementationOnce(() => pending.promise)
  const loading = store.refreshArtifacts()
  groups = groups.filter(group => !group.dir.includes('redis'))
  await store.refreshArtifacts()
  store.setArtifactKeyword('nginx-report')
  pending.resolve(response({ groups: oldGroups }))
  await loading
  expect(store.getState().artifactKeyword).toBe('nginx-report')
  expect(store.getState().artifacts.groups).toEqual(groups)
  expect(Object.keys(store.getState().artifactSel)).toEqual([nginx])
  expect(filePaths()).toEqual([nginx])
})
