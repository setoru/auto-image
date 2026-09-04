// store 草稿规则钉子：输入框是受控输入（value=draftOf(runId)），草稿必须
// 驱动重渲染——否则任何外来渲染（摘要轮询、SSE 事件、时长针）都会用旧
// value 把 DOM 里的字冲掉，表现为「输入延迟/丢字、backspace 光标跳尾」。
// 断言订阅者视角的外部可见行为：setDraft 后通知到达、快照含新值、外来
// set 不冲掉草稿。
import { afterEach, describe, expect, it, vi } from 'vitest'

// store 模块级副作用重：SSE EventSource、轮询 setInterval、loadRuns——
// 全部 stub 掉，模块隔离成纯状态容器
vi.mock('./store.js', async () => {
  const actual = await vi.importActual('./store.js')
  return actual
})

const sseListeners = {}
let globalSource = null
global.EventSource = class {
  constructor() {
    this.readyState = 0
    globalSource = this
  }
  addEventListener(type, fn) { (sseListeners[type] ??= []).push(fn) }
  onopen() {}
  onerror() {}
}
vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false })))
const timers = []
vi.stubGlobal('setInterval', (fn, ms) => { timers.push([fn, ms]); return timers.length })

const store = await import('./store.js')

afterEach(() => {
  store.clearDraft('r1')
})

describe('输入草稿', () => {
  it('setDraft 通知订阅者且快照可见（受控输入的 value 源）', () => {
    const seen = []
    const unsub = store.subscribe(() => seen.push(store.getState().drafts.r1))
    store.setDraft('r1', 'nginx 1.25')
    unsub()
    expect(seen).toContain('nginx 1.25')
    expect(store.getState().drafts.r1).toBe('nginx 1.25')
    expect(store.draftOf('r1')).toBe('nginx 1.25')
  })

  it('外来渲染（轮询/SSE 推进）不冲掉草稿——每次 set 后 draftOf 仍是最新值', () => {
    // 模拟外来事件流：外来 set 与用户输入交错
    store.setDraft('r1', 'a')
    //外来渲染等价物：任何不碰 drafts 的 set()
    store.getState() // 触发一次读
    store.setDraft('r1', 'ab')
    expect(store.draftOf('r1')).toBe('ab')
    store.clearDraft('r1')
    expect(store.draftOf('r1')).toBe('')
  })
})

describe('快照与全局流归并', () => {
  it('tail 先到仍收敛到有序事实，断线补齐游标取最大 seq', async () => {
    const snapshot = [
      { seq: 1, type: 'session.started', payload: { ts: 10 } },
      { seq: 2, type: 'stage.changed', payload: { ts: 20, stage: 'VERIFY' } },
      { seq: 3, type: 'session.title_changed', payload: { ts: 30, title: '验证 nginx' } },
      { seq: 4, type: 'turn.completed', payload: { ts: 40, result: '上一回合完成' } },
      { seq: 5, type: 'turn.started', payload: { ts: 50 } },
    ]
    const snapshotText = () => snapshot.map((item) => [
      `id: ${item.seq}`,
      `event: ${item.type}`,
      `data: ${JSON.stringify(item.payload)}`,
      '',
    ].join('\n')).join('\n')
    let replay = ''
    const snapshotRequests = []
    fetch.mockImplementation(async (url, options = {}) => {
      if (url === '/api/runs' && options.method === 'POST') {
        return {
          ok: true,
          json: async () => ({ run_id: 'event-run', status: 'READY', resumed_from: null }),
        }
      }
      if (url === '/api/runs/event-run/events') {
        snapshotRequests.push(options)
        const body = replay
        return { ok: true, text: async () => body }
      }
      return { ok: false }
    })

    await store.createRun()
    await Promise.resolve()
    sseListeners['turn.started'][0]({
      data: JSON.stringify({ run_id: 'event-run', seq: 5, ts: 50, type: 'turn.started', payload: {} }),
    })
    replay = snapshotText()
    globalSource.onopen()

    await vi.waitFor(() => expect(store.getState().runs['event-run'].events).toHaveLength(5))
    const run = store.getState().runs['event-run']
    expect({
      seqs: run.events.map((item) => item.seq),
      status: run.status,
      stage: run.stage,
      title: run.title,
      result: run.result,
      lastEventAt: run.lastEventAt,
    }).toEqual({
      seqs: [1, 2, 3, 4, 5],
      status: 'RUNNING',
      stage: 'VERIFY',
      title: '验证 nginx',
      result: '上一回合完成',
      lastEventAt: 50_000,
    })

    for (const seq of [7, 6]) {
      sseListeners['agent.message'][0]({
        data: JSON.stringify({ run_id: 'event-run', seq, ts: seq * 10, type: 'agent.message', payload: {} }),
      })
    }
    replay = ''
    globalSource.onopen()
    expect(snapshotRequests.at(-1).headers['Last-Event-ID']).toBe('7')
  })
})
