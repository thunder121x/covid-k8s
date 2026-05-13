import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const dashboardApiTarget =
  globalThis.process?.env?.VITE_DASHBOARD_API_TARGET || 'http://127.0.0.1:8088'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: dashboardApiTarget,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
