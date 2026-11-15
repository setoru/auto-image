import { useLayoutEffect, useRef, useState } from 'react'
import * as store from './store.js'

const detailKey = element => element.dataset.toolId
  ? `tool:${element.dataset.toolId}` : `event:${element.dataset.eventSeq}`

function visibleRows(element) {
  return [...element.querySelectorAll(':scope > [data-event-seq]')]
    .filter(row => row.getBoundingClientRect().height > 0)
}

function captureAnchor(element) {
  const viewport = element.getBoundingClientRect()
  const row = visibleRows(element).find(item => {
    const rect = item.getBoundingClientRect()
    return rect.bottom > viewport.top && rect.top < viewport.bottom
  })
  return row ? {
    seq: Number(row.dataset.eventSeq),
    offset: row.getBoundingClientRect().top - viewport.top,
    ...(row.dataset.toolId ? { toolId: row.dataset.toolId } : {}),
  } : null
}

function restoreAnchor(element, anchor) {
  if (!anchor) return
  const rows = visibleRows(element)
  const row = (anchor.toolId && rows.find(item => item.dataset.toolId === anchor.toolId))
    || rows.find(item => Number(item.dataset.eventSeq) === anchor.seq)
    || rows.reduce((nearest, item) => !nearest
      || Math.abs(Number(item.dataset.eventSeq) - anchor.seq) < Math.abs(Number(nearest.dataset.eventSeq) - anchor.seq)
      ? item : nearest, null)
  if (row) element.scrollTop += row.getBoundingClientRect().top - element.getBoundingClientRect().top - anchor.offset
}

function captureDetails(element, previous) {
  const details = { ...previous }
  for (const item of element.querySelectorAll(':scope > details[data-event-seq]')) {
    details[detailKey(item)] = {
      open: item.open,
      scroll: [...item.querySelectorAll('.va-tool-io-text')].map(log => ({ top: log.scrollTop, left: log.scrollLeft })),
    }
  }
  return details
}

function restoreDetails(element, details) {
  for (const item of element.querySelectorAll(':scope > details[data-event-seq]')) {
    const saved = details[detailKey(item)]
    if (!saved) continue
    item.open = saved.open
    for (const [index, log] of [...item.querySelectorAll('.va-tool-io-text')].entries()) {
      const position = saved.scroll[index]
      if (position) {
        log.scrollTop = position.top
        log.scrollLeft = position.left
      }
    }
  }
}

// 只管理当前挂载的事件流。阅读缓存独立于订阅快照，滚动不会重渲染工作台。
export function useSessionReading(run, visible, epoch) {
  const scrollRef = useRef(null)
  const reading = useRef(store.readingOf(run.runId) ?? { follow: true, anchor: null, seenSeq: null, details: {} })
  const [follow, setFollow] = useState(reading.current.follow)
  const current = useRef(null)
  const actions = useRef(null)
  current.current = { run, visible }

  useLayoutEffect(() => {
    const element = scrollRef.current
    let programmaticTop = null
    const canRead = () => current.current.visible && element.clientHeight > 0
    const save = patch => {
      reading.current = { ...reading.current, ...patch }
      store.saveReading(run.runId, reading.current, epoch)
    }
    const capture = () => {
      if (!canRead()) return
      const anchor = captureAnchor(element)
      // 空历史/隐藏行不覆盖等待快照的锚点和展开记录。
      if (anchor) save({ anchor, details: captureDetails(element, reading.current.details) })
    }
    const markSeen = () => save({ seenSeq: Math.max(reading.current.seenSeq ?? 0, current.current.run.maxSeq) })
    const restore = () => {
      if (!canRead()) return
      restoreDetails(element, reading.current.details)
      if (reading.current.follow) {
        element.scrollTop = element.scrollHeight
        markSeen()
      } else restoreAnchor(element, reading.current.anchor)
      programmaticTop = element.scrollTop
      capture()
    }
    const onScroll = event => {
      if (!canRead()) return
      if (event.target !== element) {
        capture()
        return
      }
      // 浏览器延后派发的恢复滚动不能变成用户阅读意图（含底部钳位）。
      if (programmaticTop !== null && Math.abs(element.scrollTop - programmaticTop) < 1) return
      programmaticTop = null
      const nextFollow = element.scrollHeight - element.scrollTop - element.clientHeight < 40
      save({ follow: nextFollow })
      if (nextFollow) {
        element.scrollTop = element.scrollHeight
        programmaticTop = element.scrollTop
        markSeen()
      }
      capture()
      setFollow(nextFollow)
    }
    const jump = () => {
      if (!canRead()) return
      save({ follow: true })
      setFollow(true)
      restore()
      element.focus({ preventScroll: true })
    }
    actions.current = { restore, jump }
    element.addEventListener('scroll', onScroll, true)
    element.addEventListener('toggle', capture, true)
    // 仅观察事件视口尺寸：侧栏与管理页显隐后的布局恢复，不追踪正文布局。
    const observer = new ResizeObserver(restore)
    observer.observe(element)
    return () => {
      capture()
      observer.disconnect()
      element.removeEventListener('scroll', onScroll, true)
      element.removeEventListener('toggle', capture, true)
    }
  }, [run.runId, epoch])

  useLayoutEffect(() => { actions.current.restore() }, [run.events, visible])

  return { scrollRef, follow, pending: store.pendingEventCount(run.runId), jump: () => actions.current.jump() }
}
