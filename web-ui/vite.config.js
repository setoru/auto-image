import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发模式：SSE 与 API 经 proxy 打到本机 FastAPI（python -m web）。
// 端口单一事实来源是 FastAPI 的 WEB_PORT（默认 8123，同 web/__main__.py），
// 服务端换端口时经 env 覆盖：WEB_PORT=8765 npm run dev
const port = process.env.WEB_PORT || '8123'
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': `http://127.0.0.1:${port}`,
    },
  },
})
