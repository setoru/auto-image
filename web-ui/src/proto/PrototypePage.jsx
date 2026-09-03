// PROTOTYPE — 布局手感验证专用，验证后整体删除，勿并入正式代码。
// 三变体（A/B/C，见 Variants.jsx）挂在同一路由，?variant= 切换；底部
// 浮动切换条 + ←/→ 键盘循环（输入框聚焦时不劫持）。三变体共用 protoState，
// 切换变体时 tab 状态保留（比较的是同一状态在不同壳里的手感）。
import { useEffect, useState } from 'react'
import { VARIANTS } from './Variants.jsx'
import * as ps from './protoState.js'

function useVariantKey() {
  const [key, setKey] = useState(() => {
    const cur = new URLSearchParams(location.search).get('variant')
    return VARIANTS.some((v) => v.key === cur) ? cur : 'A'
  })
  const go = (k) => {
    const url = new URL(location.href)
    url.searchParams.set('variant', k)
    history.replaceState(null, '', url)
    setKey(k)
  }
  const cycle = (dir) => {
    const i = VARIANTS.findIndex((v) => v.key === key)
    go(VARIANTS[(i + dir + VARIANTS.length) % VARIANTS.length].key)
  }
  return { key, go, cycle }
}

export default function PrototypePage() {
  ps.boot()
  const { key, cycle } = useVariantKey()
  const cur = VARIANTS.find((v) => v.key === key) ?? VARIANTS[0]

  useEffect(() => {
    const onKey = (e) => {
      const t = e.target
      if (t instanceof HTMLElement && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      if (e.key === 'ArrowLeft') cycle(-1)
      if (e.key === 'ArrowRight') cycle(1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  const Cur = cur.Comp
  return (
    <>
      <Cur />
      <div className="proto-toast" hidden={!ps.getState().toast}>{ps.getState().toast}</div>
      <div className="proto-switcher">
        <button onClick={() => cycle(-1)} title="上一变体（←）">◀</button>
        <span>{cur.key} — {cur.name}</span>
        <button onClick={() => cycle(1)} title="下一变体（→）">▶</button>
      </div>
    </>
  )
}
