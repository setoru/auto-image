import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发模式：SSE 与 API 经 proxy 打到本机 FastAPI（python -m web）
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
