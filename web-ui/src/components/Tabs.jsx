// 标签栏（替换任务下拉）：每个打开的标签页一枚，独立状态点（● 执行中 /
// ○ 等待指令 / ! 最近回合失败 / ■ 已结束）、独立关闭 ×；末尾「+ 新建」。
// 点击只切换查看（selectRun 不产生服务端动作）；关闭只关视图（closeTab
// 断 SSE，会话仍在列表）。排序按最后活动降序，新建自动切到最前（store
// 前插 openTabs）。顶部运行计数与 RUNNING 标题提示（人工规避同软件同
// 版本并行冲突的唯一防线）也归本栏。
import * as store from '../store.js'
import { firstPromptPreview, resumeMark, byLastActivity, tabDot, runningCount, runningOthers } from '../derive.js'

export default function Tabs() {
  const s = store.useRunState()
  const tabs = byLastActivity(s.openTabs, s.runs)
  const running = runningCount(s.runs)
  const others = runningOthers(s.runs, s.viewRunId)

  return (
    <div className="va-tabsbar">
      {tabs.map((id) => {
        const run = s.runs[id]
        const on = id === s.viewRunId
        return (
          <div
            key={id}
            className={`va-tab${on ? ' on' : ''}`}
            role="button"
            tabIndex={0}
            onClick={() => store.selectRun(id)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                store.selectRun(id)
              }
            }}
            title={`${firstPromptPreview(run, 60)} · ${id}${resumeMark(run, s.runs)}`}
          >
            <span className={`va-tab-dot ${tabDot(run)}`} />
            <span className="va-tab-name">{firstPromptPreview(run)}</span>
            <button
              className="va-tab-close"
              title="关闭标签页（不影响会话执行，列表里可重新打开）"
              aria-label={`关闭 ${firstPromptPreview(run)} 标签页`}
              onClick={(e) => {
                e.stopPropagation()
                store.closeTab(id)
              }}
            >
              ×
            </button>
          </div>
        )
      })}
      <button
        className="va-tab-new"
        onClick={() => store.createRun()}
        title="新建空会话（与其他会话执行互不影响）"
      >
        +
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
