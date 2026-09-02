// 底部常驻对话输入条：右侧单按钮按状态变形，回车永远等价于点它：
//   执行中 + 无输入   -> ■ 停止（Esc 等效，只停当前标签页的会话）
//   执行中 + 有输入   -> 发送（服务端 409 turn_in_progress；想改方向先显式停止）
//   等待指令          -> 发送
// 「⑂ 克隆」常驻（替代原「接续此会话」）：READY/ENDED 可点、RUNNING 禁用
// ——克隆基于 transcript resume，执行中的上下文是过时的（服务端 409 同源）。
// 新建会话入口在顶部标签栏的「+」。
import { useState } from 'react'
import * as store from '../store.js'
import { firstPromptPreview } from '../derive.js'

export default function ChatBar() {
  const run = store.useViewRun()
  const [text, setText] = useState('')

  // 输入只由当前会话状态决定（并行不受其他会话执行影响）；
  // 执行中禁用输入（无排队错觉），停止是显式按钮
  const canInputHere = !!run && store.isOperable(run.status) && run.status !== 'RUNNING'
  const isTerminal = !!run && !store.isOperable(run.status)

  const send = async () => {
    if (!text.trim()) return
    if (await store.send(text)) setText('')
  }
  // 停止只打断后续动作：已提交的云操作不可撤销（与消息流提示一致）
  const act = () => (run?.status === 'RUNNING' ? store.stop() : send())

  let btnLabel, btnDisabled
  if (run?.status === 'RUNNING') {
    btnLabel = '■ 停止'
    btnDisabled = false
  } else if (canInputHere) {
    btnLabel = '发送 ⏎'
    btnDisabled = !text.trim()
  } else {
    btnLabel = isTerminal ? '会话已结束' : '无会话'
    btnDisabled = true
  }

  const placeholder = run
    ? isTerminal
      ? '会话已结束——点「⑂ 克隆」分叉后继续对话'
      : '输入部署指令：软件 + 文档链接 + 目标机器…'
    : '点标签栏「+ 新建」开始一个部署会话'

  return (
    <div className="chat-bar">
      <button
        className="chat-continue"
        onClick={() => store.cloneRun()}
        disabled={!run || run.status === 'RUNNING'}
        title={
          run?.status === 'RUNNING'
            ? '回合执行中不能克隆（上下文在变）——回合结束后可点'
            : `从『${firstPromptPreview(run)}』的上下文分叉（新建一条克隆会话）`
        }
      >
        ⑂ 克隆
      </button>
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
