import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Vite dev server proxies the FastAPI backend so REST + WS share an origin.
// In production the dashboard is built to /dist and served statically (or
// hosted on Vercel) while the API is on a separate host; the proxy is only
// for `npm run dev` local development.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/healthz': 'http://localhost:8000',
      '/positions': 'http://localhost:8000',
      '/signals': 'http://localhost:8000',
      '/performance': 'http://localhost:8000',
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
