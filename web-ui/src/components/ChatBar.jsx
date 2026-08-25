// 底部常驻对话输入条：「+ 新建」一步创建空会话；右侧单按钮按状态变形，
// 回车永远等价于点它：
//   执行中 + 无输入   -> ■ 停止（Esc 等效）
//   执行中 + 有输入   -> 发送（服务端先停止再投递）
//   挂起 + 无其他执行 -> 发送
//   挂起 + 另一会话执行中 -> 输入禁用，提示原因
import { useState } from 'react'
import * as store from '../store.js'

export default function ChatBar() {
  const run = store.useViewRun()
  const executing = !!store.executingRunId()
  const [text, setText] = useState('')

  // 挂起会话在另一会话执行中时输入禁用（RUNNING 的会话即执行中的那个，不受此限）
  const canInputHere = !!run && store.isActive(run.status) && (run.status === 'RUNNING' || !executing)
  const noPromptYet = !run?.events.some((e) => e.type === 'user.message')

  const send = async () => {
    if (!text.trim()) return
    if (await store.send(text)) setText('')
  }
  const act = () => (run?.status === 'RUNNING' && !text.trim() ? store.stop() : send())

  let btnLabel, btnDisabled
  if (run?.status === 'RUNNING') {
    btnLabel = text.trim() ? '发送 ⏎ 停止' : '■ 停止'
    btnDisabled = false
  } else if (canInputHere) {
    btnLabel = '发送 ⏎'
    btnDisabled = !text.trim()
  } else {
    btnLabel = run?.status === 'WAITING_INPUT' ? '另一会话执行中' : '会话不可用'
    btnDisabled = true
  }

  const placeholder = run
    ? canInputHere
      ? noPromptYet
        ? '输入第一条部署指令：软件 + 文档链接 + 目标…'
        : run.status === 'RUNNING'
          ? '输入指令将先停止当前回合…'
          : '输入指令：继续 / 删除刚创建的 ECS / 跳过验证直接打包…'
      : run.status === 'WAITING_INPUT'
        ? '另一会话正在执行，先停止它或切换查看'
        : '会话已结束——新建或切换任务'
    : '点「+ 新建」开始一个部署会话'

  return (
    <div className="chat-bar">
      <button
        className="chat-new"
        onClick={() => store.createRun()}
        disabled={executing}
        title={executing ? '有会话在执行，停止后才能新建' : '新建空会话'}
      >
        + 新建
      </button>
      <input
        value={text}
        disabled={!canInputHere}
        placeholder={placeholder}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && !btnDisabled && act()}
      />
      <button className="chat-act" onClick={act} disabled={btnDisabled}>
        {btnLabel}
      </button>
    </div>
  )
}
