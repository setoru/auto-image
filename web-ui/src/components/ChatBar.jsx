// 底部常驻对话输入条：「+ 新建」一步创建空会话；右侧单按钮按状态变形，
// 回车永远等价于点它：
//   执行中 + 无输入   -> ■ 停止（Esc 等效）
//   执行中 + 有输入   -> 发送（服务端 409 turn_in_progress；想改方向先显式停止）
//   等待指令          -> 发送
// 查看终态会话时出现「⑂ 克隆此会话」：一键从当前
// 查看的会话分叉（ENDED 只读是状态机语义——克隆 = 以该会话新建身份，
// 上下文完整）。
import { useState } from 'react'
import * as store from '../store.js'
import { firstPromptPreview, hasPrompt } from '../derive.js'

export default function ChatBar() {
  const run = store.useViewRun()
  const [text, setText] = useState('')

  // 输入只由当前会话状态决定（并行不受其他会话执行影响）
  const canInputHere = !!run && store.isOperable(run.status)
  const noPromptYet = !hasPrompt(run)
  const isTerminal = !!run && !store.isOperable(run.status)

  const send = async () => {
    if (!text.trim()) return
    if (await store.send(text)) setText('')
  }
  // 停止只打断后续动作：已提交的云操作不可撤销（与消息流提示一致）
  const act = () => (run?.status === 'RUNNING' && !text.trim() ? store.stop() : send())

  let btnLabel, btnDisabled
  if (run?.status === 'RUNNING') {
    btnLabel = text.trim() ? '发送 ⏎ 停止' : '■ 停止'
    btnDisabled = false
  } else if (canInputHere) {
    btnLabel = '发送 ⏎'
    btnDisabled = !text.trim()
  } else {
    btnLabel = '会话已结束'
    btnDisabled = true
  }

  const placeholder = run
    ? canInputHere
      ? noPromptYet
        ? '输入第一条部署指令：软件 + 文档链接 + 目标…'
        : '输入指令：继续 / 删除刚创建的 ECS / 跳过验证直接打包…'
      : '会话已结束——点「⑂ 克隆此会话」继续对话'
    : '点「+ 新建」开始一个部署会话'

  return (
    <div className="chat-bar">
      <button
        className="chat-new"
        onClick={() => store.createRun()}
        title="新建空会话（与其他会话执行互不影响）"
      >
        + 新建
      </button>
      {isTerminal && (
        <button
          className="chat-continue"
          onClick={() => store.cloneRun()}
          title={`从『${firstPromptPreview(run)}』的上下文分叉（新建一条克隆会话）`}
        >
          ⑂ 克隆此会话
        </button>
      )}
      <input
        value={text}
        disabled={!canInputHere}
        placeholder={placeholder}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && !btnDisabled && act()}
      />
      <button className="chat-send" onClick={act} disabled={btnDisabled}>
        {btnLabel}
      </button>
    </div>
  )
}
