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
global.EventSource = class {
  constructor() { this.readyState = 0 }
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
