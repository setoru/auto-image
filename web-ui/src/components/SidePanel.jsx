// 左侧侧栏：顶部小 tab「会话 | 任务 | 产物 | OBS产物 | ECS实例」切五块面板（默认
// 产物，切过之后 localStorage 记住选择）。会话面板 = 全部会话仪表盘（含
// ENDED 与重启恢复的历史，服务端最后活跃降序平铺），点行开成（或激活既有）
// 会话标签页——历史会话由此第一次可达。产物面板 = 原产物卡内容原样迁入
// （工具行/复选框/zip/单文件下载/默认展开最新组），点文件开成（或激活
// 既有）文件标签页——多槽内容缓存，消息流不再被顶走。OBS产物面板 = 桶内
// 对象在线清单（/api/obs/objects），树形同构，点对象开 OBS 标签页预览。
// ECS实例面板 = 华为云实例清单/状态 + 一键存活检查 + 新建（无标签页，
// 面板内完成）。数据请求：会话列表即摘要轮询已拉的全量，本地产物即清单
// 刷新，OBS 与 ECS 启动拉一次 + 刷新钮（不随流水线事件联动）。
import { useEffect, useRef, useState } from 'react'
import * as store from '../store.js'
import { tabKey } from '../tabState.js'
import EcsPanel from './EcsPanel.jsx'
import ObsPanel from './ObsPanel.jsx'
import TasksPanel from './TasksPanel.jsx'
import StageBadge from './StageBadge.jsx'
import {
  RUN_STATUS_LABEL, STAGE_LABEL, firstPromptText, firstPromptPreview, lastActivityAt, tabDot, fmtAgo,
  fmtSize, artifactTree, subtreeRels, defaultOpenPaths, artifactFileCount,
} from '../derive.js'

// 会话列表行：状态点 + 标题（LLM 标题优先/首条指令截断回退/空会话占位）+
// 右侧相对时间；第二行小字状态中文与当前阶段（ENDED 只显「已结束」，
// 不再显阶段）。当前控制面会话高亮；已开标签页的会话弱标记（标题前小点）。
function SessionRow({ run, on, open }) {
  const title = firstPromptPreview(run, 40)
  const sub =
    run.status === 'ENDED'
      ? RUN_STATUS_LABEL.ENDED
      : [RUN_STATUS_LABEL[run.status], run.stage && (STAGE_LABEL[run.stage] ?? run.stage)]
          .filter(Boolean)
          .join(' · ')
  return (
    <div
      className={`va-sess-row${on ? ' on' : ''}`}
      role="button"
      tabIndex={0}
      onClick={() => store.selectRun(run.runId)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          store.selectRun(run.runId)
        }
      }}
      title={title}
    >
      <span className={`va-tab-dot ${tabDot(run)}`} aria-hidden="true" />
      <div className="va-sess-main">
        <div className="va-sess-title">
          {open && <span className="va-sess-open" title="已开为标签页" />}
          <span className="va-sess-name">{title}</span>
          <span className="va-sess-ago">{fmtAgo(lastActivityAt(run))}</span>
        </div>
        <div className="va-sess-sub">{sub}</div>
      </div>
    </div>
  )
}

function KeywordFilter({ id, label, placeholder, keyword, onChange }) {
  const inputRef = useRef(null)
  return (
    <>
      <label className="va-keyword-filter-label" htmlFor={id}>{label}</label>
      <div className="va-keyword-filter">
        <input
          id={id}
          ref={inputRef}
          type="search"
          value={keyword}
          placeholder={placeholder}
          onChange={event => onChange(event.target.value)}
        />
        <button
          aria-label={`清除${label}`}
          disabled={!keyword}
          onClick={() => {
            onChange('')
            inputRef.current?.focus()
          }}
        >清除</button>
      </div>
    </>
  )
}

// 筛选只派生展示，保留服务端最近活跃顺序；使用完整字段而非行标题预览。
function SessionPanel({ order, runs, controlId, openIds, keyword }) {
  const query = keyword.trim().toLowerCase()
  const sessions = order.map(id => runs[id]).filter(Boolean)
  const matches = query ? sessions.filter(run =>
    [run.title, firstPromptText(run), run.runId].some(text => text?.toLowerCase().includes(query)),
  ) : sessions
  return (
    <div className="va-side-panel">
      <KeywordFilter id="va-session-keyword" label="会话关键词" placeholder="标题、首条指令或 ID"
        keyword={keyword} onChange={store.setSessionKeyword} />
      <div className="va-keyword-filter-count" role="status" aria-live="polite">
        {`匹配 ${matches.length} / 共 ${sessions.length} 条`}
      </div>
      {matches.map(run => (
        <SessionRow
          key={run.runId}
          run={run}
          on={run.runId === controlId}
          open={openIds.has(run.runId)}
        />
      ))}
      {sessions.length === 0 ? (
        <div className="va-side-empty">暂无会话——点标签栏「+ 新建」</div>
      ) : matches.length === 0 && (
        <div className="va-side-empty">没有匹配的会话，试试其他关键词或清除筛选</div>
      )}
    </div>
  )
}

// 目录树节点：目录行（箭头 + 三态勾选 + 目录名 + 子树文件数）+ 本目录文件
// 行 + 子目录递归（缩进 + 竖参考线）。目录行勾选作用于子树全部文件（三态：
// 全选 / 部分半选 / 无）；展开状态由父级 toggles 字典集中管理，未动过的
// 目录落到 defaultOpen（最新一组所在路径自动展开）。文件行「已开标签页」
// 弱标记与高亮从 tabs 派生，不另立状态。
function ArtDir({ node, toggles, filtering, defaultOpen, openFiles, activeRel, depth = 0 }) {
  const s = store.useRunState()
  const open = toggles[node.path] ?? (filtering || defaultOpen.has(node.path))
  const rels = subtreeRels(node)
  const selCount = rels.reduce((n, p) => n + (s.artifactSel[p] ? 1 : 0), 0)
  const allOn = rels.length > 0 && selCount === rels.length
  const some = selCount > 0 && !allOn
  const toggleOpen = () => store.setArtifactDirOpen(node.path, !open)
  return (
    <div className={`va-art-group va-local-art-group${depth >= 3 ? ' va-art-depth-limit' : ''}`}>
      <div
        className="va-art-dir-head"
        role="button"
        tabIndex={0}
        aria-expanded={open}
        onClick={toggleOpen}
        onKeyDown={(e) => {
          if (e.target !== e.currentTarget) return
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            toggleOpen()
          }
        }}
        title={node.path}
      >
        <span className="va-art-dir-arrow">{open ? '▾' : '▸'}</span>
        <input
          type="checkbox"
          className="va-art-check"
          checked={allOn}
          ref={(el) => {
            if (el) el.indeterminate = some
          }}
          onChange={() => store.setArtifactSel(rels, !allOn)}
          onClick={(e) => e.stopPropagation()}
          aria-label={`选择 ${node.path} 下${filtering ? '匹配' : '全部'}文件`}
          title={filtering ? '勾选本目录（含子目录）匹配文件' : '勾选本目录（含子目录）全部文件'}
        />
        <span className="va-art-name">{node.name}</span>
        <span className="va-art-count" title={filtering ? '本目录（含子目录）匹配文件数' : '本目录（含子目录）文件数'}>{node.count}</span>
      </div>
      {open && (
        <div className="va-art-children">
          {node.files.map((f) => {
            const rel = `${node.path}/${f.name}`
            const opened = openFiles.has(rel)
            return (
              <div
                key={f.name}
                role="button"
                tabIndex={0}
                className={`va-art-item${activeRel === rel ? ' on' : ''}`}
                onClick={() => store.openArtifact(rel, f)}
                onKeyDown={(e) => {
                  if (e.target !== e.currentTarget) return
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    store.openArtifact(rel, f)
                  }
                }}
                title={rel}
              >
                <input
                  type="checkbox"
                  className="va-art-check"
                  checked={!!s.artifactSel[rel]}
                  aria-label={`选择 ${rel}`}
                  onChange={() => store.toggleArtifactSel(rel)}
                  onClick={(e) => e.stopPropagation()}
                />
                {f.stage ? <StageBadge stage={f.stage} /> : null}
                <span className="va-art-name">{f.name}</span>
                {opened && <span className="va-art-opened" title="已打开为标签页" />}
                <span className="va-art-size">{fmtSize(f.size)}</span>
                <button
                  className="va-art-dl"
                  title="下载此文件"
                  aria-label={`下载 ${f.name}`}
                  onClick={(e) => {
                    e.stopPropagation()
                    store.downloadArtifact(rel)
                  }}
                >
                  ⤓
                </button>
              </div>
            )
          })}
          {node.dirs.map((d) => (
            <ArtDir key={d.path} node={d} toggles={toggles} filtering={filtering} defaultOpen={defaultOpen} openFiles={openFiles} activeRel={activeRel} depth={depth + 1} />
          ))}
        </div>
      )}
    </div>
  )
}

// 产物面板：deploy/ + rpm/ 全量镜像，目录树形态（服务端平铺分组派生成嵌套
// 树，子目录按最新落盘在前），点击文件开成（或激活既有）文件标签页。约定
// 命名的带阶段徽标，非约定的（.v1 备份、杂项）无徽标平铺。每行可勾选
// （目录行/面板头可整棵子树全选），单文件 ⤓ 下载、勾选集一键打包 zip 下载。
// 行与目录头是 div 而非 button：内部还嵌复选框与下载按钮，交互件不嵌套。
function ArtifactPanel({ openFiles, activeRel }) {
  const s = store.useRunState()
  const groups = s.artifacts.groups
  const query = s.artifactKeyword.trim().toLowerCase()
  const toggles = query ? s.artifactQueryToggles : s.artifactDirToggles
  const [exportOpen, setExportOpen] = useState(false)
  const anchorRef = useRef(null)
  const closeExport = (run) => {
    setExportOpen(false)
    run()
  }
  // 展开期间点外部收起（mousedown 而非 click：事件先于按钮 onClick 落定，
  // 点菜单项时仍属锚点内部不误收；点空白/其他面板元素即关）
  useEffect(() => {
    if (!exportOpen) return
    const onDown = (e) => {
      if (anchorRef.current && !anchorRef.current.contains(e.target)) setExportOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [exportOpen])
  const roots = artifactTree(groups, query)
  const matchRels = roots.flatMap(subtreeRels)
  const matchPaths = new Set(matchRels)
  const defaultOpen = defaultOpenPaths(groups)
  const fileCount = artifactFileCount(groups)
  const selCount = Object.keys(s.artifactSel).length
  const outsideCount = Object.keys(s.artifactSel).filter(path => !matchPaths.has(path)).length
  return (
    <div className="va-side-panel va-art-panel">
      <KeywordFilter id="va-artifact-keyword" label="产物关键词" placeholder="文件名、软件或版本路径"
        keyword={s.artifactKeyword} onChange={store.setArtifactKeyword} />
      <div className="va-keyword-filter-count" role="status" aria-live="polite">
        {`匹配 ${matchRels.length} / 共 ${fileCount} 个文件`}
      </div>
      <div className="va-art-selection" role="status" aria-live="polite">
        {outsideCount > 0 ? `已选 ${selCount}，其中 ${outsideCount} 项不在当前筛选结果` : `已选 ${selCount}`}
      </div>
      {(fileCount > 0 || selCount > 0) && (
        <div className="va-art-tools">
          <button onClick={() => store.setArtifactSel(matchRels, true)} disabled={matchRels.length === 0}>
            全选匹配项
          </button>
          <button onClick={() => store.clearArtifactSel()} disabled={selCount === 0}>
            清空全部已选
          </button>
          {/* 三种导出沿用全局勾选集；目录折叠与关键词只改变展示。 */}
          <span className="va-art-export-anchor" ref={anchorRef}>
            <button
              className="va-art-zip"
              onClick={() => setExportOpen((v) => !v)}
              disabled={selCount === 0 || s.artifactZipping || s.obsArchiving || s.obsZipArchiving}
              aria-expanded={exportOpen}
              title="导出全部已选，包含筛选外文件（ZIP 下载 / OBS 散件 / OBS 单包）"
            >
              导出全部已选 ({selCount})
            </button>
            {exportOpen && (
              <div className="va-art-export" role="menu" aria-label="导出方式">
                <button
                  role="menuitem"
                  onClick={() => closeExport(() => store.downloadArtifactZip())}
                  disabled={s.artifactZipping}
                  title="全部已选打包成一个 ZIP 下载到本机（文件名自动带时间戳）"
                >
                  {s.artifactZipping ? '打包中…' : '下载 ZIP'} · 全部已选 ({selCount})
                </button>
                <button
                  role="menuitem"
                  onClick={() => closeExport(() => store.archiveToObs())}
                  disabled={s.obsArchiving}
                  title="全部已选逐个上传到 OBS 桶（对象名 = 产物路径，同名覆盖）"
                >
                  {s.obsArchiving ? '归档中…' : '上传 OBS · 散件'} · 全部已选 ({selCount})
                </button>
                <button
                  role="menuitem"
                  onClick={() => closeExport(() => store.archiveZipToObs())}
                  disabled={s.obsZipArchiving}
                  title="全部已选打成一个 ZIP（自定义包名）上传到 OBS 的 zip/ 目录"
                >
                  {s.obsZipArchiving ? '打包中…' : '上传 OBS · 单包'} · 全部已选 ({selCount})
                </button>
              </div>
            )}
          </span>
        </div>
      )}
      {fileCount === 0 ? (
        <div className="va-side-empty">deploy/ · rpm/ 下暂无产物</div>
      ) : roots.length === 0 && (
        <div className="va-side-empty">没有匹配的产物，试试其他关键词或清除筛选</div>
      )}
      {roots.map((r) => (
        <ArtDir key={r.path} node={r} toggles={toggles} filtering={!!query} defaultOpen={defaultOpen} openFiles={openFiles} activeRel={activeRel} />
      ))}
    </div>
  )
}

// 侧栏本体：pin 开合钮在 Workbench 内（骑缝移动）。
// 面板选择在 store（localStorage 持久化——刷新后仍是切过的面板，首次默认
// 产物）：会话标签的任务 pill 要跨面板跳到任务面板，本地 state 不够用。
// 各面板的「当前对象」标记从 tabs 派生：会话面板高亮控制面会话，产物面板
// 高亮激活的文件标签页（弱标记则覆盖全部已开文件）。
export default function SidePanel() {
  const s = store.useRunState()
  const panel = s.sidePanel === 'users' ? 'artifacts' : s.sidePanel
  const openIds = new Set(s.tabs.filter((t) => t.kind === 'session').map((t) => t.runId))
  const openFiles = new Set(s.tabs.filter((t) => t.kind === 'file').map((t) => t.relPath))
  const openObsKeys = new Set(s.tabs.filter((t) => t.kind === 'obs').map((t) => t.key))
  const activeTab = s.tabs.find((t) => tabKey(t) === s.activeKey)
  const switchPanel = (p) => store.setSidePanel(p)
  return (
    <aside className="va-side" id="task-side">
      <div className="va-side-tabs" role="tablist" aria-label="侧栏面板">
        {[
          ['sessions', '会话'], ['tasks', '任务'], ['artifacts', '产物'],
          ['obs', 'OBS产物'], ['ecs', 'ECS实例'],
        ].map(([key, label]) => (
          <button key={key} role="tab" aria-label={label} aria-selected={panel === key}
            aria-controls="va-side-panel" tabIndex={panel === key ? 0 : -1}
            className={panel === key ? 'on' : ''} onClick={() => switchPanel(key)}
            onKeyDown={(event) => {
              const buttons = Array.from(event.currentTarget.parentElement.querySelectorAll('[role="tab"]'))
              const index = buttons.indexOf(event.currentTarget)
              const next = { ArrowRight: (index + 1) % buttons.length,
                ArrowLeft: (index - 1 + buttons.length) % buttons.length,
                Home: 0, End: buttons.length - 1 }[event.key]
              if (next === undefined) return
              event.preventDefault()
              buttons[next].focus()
              buttons[next].click()
            }}
          >{label}</button>
        ))}
      </div>
      <div className="va-side-panel-wrap" id="va-side-panel" role="tabpanel">
        {panel === 'sessions' ? (
          <SessionPanel order={s.order} runs={s.runs} controlId={store.controlRunId()} openIds={openIds} keyword={s.sessionKeyword} />
        ) : panel === 'tasks' ? (
          <TasksPanel />
        ) : panel === 'obs' ? (
          <ObsPanel openKeys={openObsKeys} activeKey={activeTab?.kind === 'obs' ? activeTab.key : null} />
        ) : panel === 'ecs' ? (
          <EcsPanel />
        ) : (
          <ArtifactPanel openFiles={openFiles} activeRel={activeTab?.kind === 'file' ? activeTab.relPath : null} />
        )}
      </div>
    </aside>
  )
}
