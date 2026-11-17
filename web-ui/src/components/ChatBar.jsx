// 底部常驻对话输入条（控制面之一）：作用于最后激活的会话标签页。左缘
// 常显作用对象胶囊（状态点 + mono 任务名）；激活文件标签页时输入折叠为
// 作用对象行——点它切回该会话标签页再输入（作用对象先确认，就着产物
// 清单也不会把指令误发进另一个挂起会话），执行中回合的「■ 停止」在
// 折叠态仍在手边。右侧单按钮按状态变形，回车永远等价于点它：
//   执行中   -> ■ 停止（stop 自带会话判定，只停控制面会话；输入同步
//               禁用，想改方向先停止——执行中发送被服务端 409
//               turn_in_progress 拒）
//   等待指令 -> 发送
// 「⑂ Fork」常驻：READY/ENDED 可点、RUNNING 禁用——Fork 基于 transcript
// 续接，执行中的上下文是过时的（服务端 409 同源）。新建会话入口在标签栏
// 「+ 新建」。草稿与发送反馈由 store 按会话保存和结算。
import { useRef } from 'react'
import * as store from '../store.js'
import { firstPromptPreview, tabDot } from '../derive.js'
import { tabKey } from '../tabState.js'

// 自动伸缩高度：内容行数 1–5 行（行高 1.5，与正文同档），超出内滚——
// 部署指令是「软件+文档链接+目标机器」多要素长文本，发送前要能回看
// 校对（发送即触发云操作），单行 input 容不下草稿的真实形态
function autoSize(el) {
  if (!el) return
  el.style.height = 'auto'
  const lh = 21
  el.style.height = `${Math.min(el.scrollHeight, lh * 5 + 16)}px`
}

function SubmissionFeedback({ runId, submission, onDismiss }) {
  if (!submission || submission.status === 'accepted') return null
  const { status, message, refresh } = submission
  const label = {
    sending: '正在发送指令…',
    rejected: `未发送：${message}`,
    unknown: '发送结果未确认，指令可能已接收。请查看最新记录，避免重复发送。',
  }[status]
  return (
    <div className={`chat-feedback ${status}`}>
      <p role="status"><span className="chat-feedback-run">会话 {runId}</span> · {label}</p>
      {status === 'unknown' && <>
        <div className="chat-feedback-actions">
          <button
            aria-disabled={refresh === 'loading'}
            onClick={() => refresh !== 'loading' && store.refreshSubmission(runId)}
          >{refresh === 'loading' ? '正在刷新…' : '刷新会话记录'}</button>
          <button onClick={onDismiss} title="只收起本机提醒，不清草稿、不撤销发送">收起提示</button>
          <span>收起仅移除本机提醒，不撤销发送。</span>
        </div>
        {refresh && <p className="chat-refresh-result" role="status">{{
          loading: '正在读取会话记录…',
          success: '记录已刷新，请查看内容核对；本次发送结果仍未确认。',
          error: '记录刷新失败，请稍后再试；本次发送结果仍未确认。',
        }[refresh]}</p>}
        <details className="chat-submission-text">
          <summary>查看本次提交内容</summary>
          <pre>{submission.text}</pre>
        </details>
      </>}
    </div>
  )
}

export default function ChatBar() {
  const s = store.useRunState()
  const run = store.useControlRun()
  const runId = run?.runId ?? null
  const text = store.draftOf(runId)
  const submission = store.submissionOf(runId)
  const inputRef = useRef(null)
  const targetRef = useRef(null)
  const activeTab = s.tabs.find((t) => tabKey(t) === s.activeKey) ?? null
  // 激活的就是会话标签页时作用对象显而易见（胶囊窄形态常显左缘）；
  // 激活文件标签页时输入折叠为宽形态胶囊——先确认作用对象再输入
  const targetOn = activeTab?.kind === 'session'

  // 输入只由控制面会话状态决定（并行不受其他会话执行影响）；
  // 执行中禁用输入（无排队错觉），停止是显式按钮
  const canInputHere = !!run && store.isOperable(run.status) && run.status !== 'RUNNING'
  const isTerminal = !!run && !store.isOperable(run.status)

  const send = () => {
    if (!canInputHere || !text.trim() || submission?.status === 'sending') return
    store.send(text)
  }
  // 停止只打断后续动作：已提交的云操作不可撤销（与消息流提示一致）
  const act = () => (run?.status === 'RUNNING' ? store.stop() : send())

  let btnLabel, btnDisabled
  if (run?.status === 'RUNNING') {
    btnLabel = '■ 停止'
    btnDisabled = false
  } else if (canInputHere) {
    btnLabel = submission?.status === 'sending' ? '发送中…' : '发送 ⏎'
    btnDisabled = !text.trim() || submission?.status === 'sending'
  } else {
    btnLabel = isTerminal ? '会话已结束' : '无会话'
    btnDisabled = true
  }

  const placeholder = run
    ? isTerminal
      ? '会话已结束——点「⑂ Fork」基于此上下文创建新分支'
      : run.status === 'RUNNING'
        ? '回合执行中——想改方向点「■ 停止」打断后再输入'
        : '输入部署指令：软件 + 文档链接 + 目标机器…（Enter 发送，Shift+Enter 换行）'
    : '点标签栏「+ 新建」开始一个部署会话'

  // 作用对象胶囊（窄 = 常显标识；宽 = 折叠态的输入位替身），点击切回
  // 该会话标签页
  const target = (wide) => (
    <button
      className={`chat-target${wide ? ' wide' : ''}`}
      ref={targetRef}
      onClick={() => runId && store.activateTab(tabKey({ kind: 'session', runId }))}
      title={`输入发送到此会话 · ${runId}${targetOn ? '' : '（点击切回它的标签页）'}`}
    >
      <span className={`va-tab-dot ${tabDot(run)}`} aria-hidden="true" />
      <span className="chat-target-name">
        {wide ? `输入将作用于「${firstPromptPreview(run, 24)}」——点击切回会话` : firstPromptPreview(run)}
      </span>
    </button>
  )

  const forkBtn = (
    <button
      className="chat-continue"
      onClick={() => store.cloneRun()}
      disabled={!run || run.status === 'RUNNING'}
      title={
        run?.status === 'RUNNING'
          ? '回合执行中不能 Fork（上下文仍在变化）——回合结束后可用'
          : `从『${firstPromptPreview(run)}』的上下文创建独立 Fork`
      }
    >
      ⑂ Fork
    </button>
  )

  const dismiss = () => {
    store.dismissSubmission(runId)
    const focusTarget = targetOn && run?.status !== 'RUNNING' ? inputRef : targetRef
    focusTarget.current?.focus()
  }

  // 文件视图仍保留原控制面和停止入口；反馈也标明同一会话归属。
  const collapsed = !!run && !targetOn
  return (
    <div className="chat-composer">
      <SubmissionFeedback runId={runId} submission={submission} onDismiss={dismiss} />
      {s.draftStorageError && <p className="chat-draft-hint" role="status">
        本机保存失败，刷新恢复不可用；当前仍可输入和发送，草稿与发送提示请在离开前自行保留。
      </p>}
      <div className={`chat-bar${collapsed ? ' collapsed' : ''}`}>
        {run && target(collapsed)}
        {forkBtn}
        {!collapsed && <textarea
          rows={1}
          value={text}
          aria-label={isTerminal ? '已结束会话的草稿（可复制）' : '部署指令'}
          disabled={!run || run.status === 'RUNNING'}
          readOnly={isTerminal}
          placeholder={placeholder}
          title={run?.status === 'RUNNING' ? '回合执行中不能输入——想改方向点「■ 停止」' : isTerminal ? '会话已结束，保留的草稿可选择复制' : undefined}
          onChange={(e) => {
            store.setDraft(runId, e.target.value, s.identityEpoch)
            autoSize(e.target)
          }}
          onKeyDown={(e) => {
            // 发送中仍可编辑，Enter 不再次投递；Shift+Enter 保留换行。
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && !isTerminal) {
              e.preventDefault()
              if (!btnDisabled) act()
            }
          }}
          ref={(el) => { inputRef.current = el; autoSize(el) }}
        />}
        {(!collapsed || run.status === 'RUNNING') && <button className="chat-send" onClick={act} disabled={btnDisabled}>
          {btnLabel}
        </button>}
      </div>
    </div>
  )
}
