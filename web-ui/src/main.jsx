import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import PrototypePage from './proto/PrototypePage.jsx'

// PROTOTYPE — 布局手感验证（?variant=A/B/C，默认 A），验证后连同 proto/ 删除
const Page = new URLSearchParams(location.search).has('variant') ? PrototypePage : App

ReactDOM.createRoot(document.getElementById('root')).render(<Page />)
