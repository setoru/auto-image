// 混合标签栏（只压主区上方，不横跨侧栏）：会话标签页（独立状态点 ● 执行中 /
// ○ 等待指令 / ! 最近回合失败 / ■ 已结束 + 标题）与文件标签页（文件图标 +
// 文件名，hover 显全路径）同栏混排，溢出横向滚动。点击只切激活（不产生
// 服务端动作）；关闭只关视图（closeTab 断 SSE，会话仍在列表；文件内容
// 缓存保留）。最后一枚会话标签页的 × 不渲染（控制面永远有对象）。
// 栏尾「+ 新建」与运行计数、「正在跑」提示（人工规避同软件同版本并行
// 冲突的唯一防线）。
import * as store from '../store.js'
import { firstPromptPreview, resumeMark, tabDot, runningCount, runningOthers } from '../derive.js'
import { tabKey } from '../tabState.js'

export default function Tabs() {
  const s = store.useRunState()
  const sessionCount = s.tabs.filter((t) => t.kind === 'session').length
  const running = runningCount(s.runs)
  const others = runningOthers(s.runs, store.controlRunId())

  return (
    <div className="va-tabsbar">
      {s.tabs.map((t) => {
        const key = tabKey(t)
        const on = key === s.activeKey
        const run = t.kind === 'session' ? s.runs[t.runId] : null
        const closeable = t.kind === 'file' || sessionCount > 1
        return (
          <div
            key={key}
            className={`va-tab${on ? ' on' : ''}`}
            role="button"
            tabIndex={0}
            onClick={() => store.activateTab(key)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                store.activateTab(key)
              }
            }}
            title={
              t.kind === 'file'
                ? t.relPath
                : `${firstPromptPreview(run, 60)} · ${t.runId}${resumeMark(run, s.runs)}`
            }
          >
            {run ? (
              <>
                <span className={`va-tab-dot ${tabDot(run)}`} />
                <span className="va-tab-name">{firstPromptPreview(run)}</span>
              </>
            ) : (
              <>
                <span className="va-tab-ico">{s.artifactCache[t.relPath]?.binary ? '📦' : '📄'}</span>
                <span className="va-tab-name">{t.name}</span>
              </>
            )}
            {closeable && (
              <button
                className="va-tab-close"
                title={t.kind === 'file' ? '关闭文件标签页' : '关闭标签页（不影响会话执行，列表里可重新打开）'}
                aria-label={`关闭 ${t.kind === 'file' ? t.name : firstPromptPreview(run)} 标签页`}
                onClick={(e) => {
                  e.stopPropagation()
                  store.closeTab(key)
                }}
              >
                ×
              </button>
            )}
          </div>
        )
      })}
      <button
        className="va-tab-new"
        onClick={() => store.createRun()}
        title="新建空会话（与其他会话执行互不影响）"
      >
        + 新建
      </button>
      <span className="va-spacer" />
      {running > 0 && (
        <span className="va-run-count" title="当前执行中的回合数（并发上限内的并行负载）">
          运行中 {running}
        </span>
      )}
      {others.length > 0 && (
        <span
          className="va-run-hint"
          title="正在执行的会话——避免同软件同版本并行（产物目录共享，结果会互相覆盖）"
        >
          正在跑：{others.map((r) => firstPromptPreview(r)).join('、')}
        </span>
      )}
    </div>
  )
}
