import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Vite dev server proxies the FastAPI backend so REST + WS share an origin.
// In production the SPA is mounted by FastAPI at the same host as the API,
// so no proxy is needed there — this is only for `npm run dev`.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/healthz': 'http://localhost:8000',
      '/positions': 'http://localhost:8000',
      '/signals': 'http://localhost:8000',
      '/performance': 'http://localhost:8000',
      '/timeline': 'http://localhost:8000',
      '/config': 'http://localhost:8000',
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
  },
})
